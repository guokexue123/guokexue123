#!/usr/bin/env python3
"""
supjav.com 视频下载器 — 专家重构版 v2

架构（四层）：
    BrowserManager      — 浏览器生命周期 / stealth / Chromium 路径
    CloudflareDetector  — 统一检测 CF 四种拦截模式
    FrameAnalyzer       — Frame 树注册表（event-driven，避免重复扫描）
    MediaAnalyzer       — 播放器定位 → video.currentSrc → m3u8
    Downloader          — ffmpeg 下载 / 纯 Python 备用

重构要点（整合专家建议）：
  1. 优先找播放器（video.currentSrc），而非直接搜 m3u8
  2. context.route() 比 page.on("request/response") 更早拦截，无需等响应
  3. 实时维护 Frame 注册表（frameattached / framenavigated），不重复遍历
  4. CF 检测覆盖四种模式：JS Challenge / Turnstile / Managed / Interstitial
  5. dump_frame_tree() 可视化 iframe 嵌套层级，快速定位播放器位置
  6. 先找 deepest player iframe，再分析媒体源

依赖：
  pip install playwright playwright-stealth httpx
  playwright install chromium   # 或指定 CHROMIUM_BIN 路径
  # ffmpeg: apt install ffmpeg  /  winget install ffmpeg
"""

import asyncio
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, urljoin

import httpx
from playwright.async_api import (
    async_playwright, Browser, BrowserContext, Page, Frame, Route
)

# ╔════════════════════════════════════════════════════════╗
# ║                     用户配置                            ║
# ╚════════════════════════════════════════════════════════╝

TARGET_URL   = "https://supjav.com/132824.html"
DOWNLOAD_DIR = Path("downloads")
VIDEO_SERVER = "DS"          # DS / TV / JPA / ST / "" (默认)
PLAYER_URL   = ""            # 直接填播放器 embed URL 跳过主页流程

# Cookie 文件（按顺序扫描，首个含 cf_clearance 的生效）
COOKIE_FILES = ["cf_cookies.json", "cookie_cache.json", "cookies.txt"]
COOKIE_CACHE = Path("cookie_cache.json")

# Chromium 可执行文件路径（留空自动查找）
# Linux 示例: "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
# Windows:    r"C:\Program Files\Google\Chrome\Application\chrome.exe"
CHROMIUM_BIN = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"

HEADLESS         = True    # False = 显示浏览器（调试 / 手动过 CF）
CF_WAIT_SEC      = 25      # 等待主站 CF 自动通过最大秒数
PLAYER_CF_WAIT   = 30      # 等待播放器 iframe CF 最大秒数
M3U8_ROUTE_WAIT  = 60      # context.route 捕获 m3u8 最大秒数

# FlareSolverr (方案 B)
FLARESOLVERR_URL     = "http://localhost:8191/v1"
FLARESOLVERR_TIMEOUT = 60

# CapSolver (方案 C, 付费)
CAPSOLVER_API_KEY = ""

# ffmpeg 路径（留空自动查找）
FFMPEG_PATH = ""

# User-Agent
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# ╚════════════════════════════════════════════════════════╝

DOMAIN = urlparse(TARGET_URL).netloc

_AD_KW = (
    "mayzaent.", "googlesyndication.", "doubleclick.", "adnxs.",
    "amazon-adsystem.", "adtech.", "advertising.", "tracker.",
    "tracking.", "analytics.", "pixel.", "campaign", "banner",
)

_PLAYER_PATH_KW = ("/e/", "/embed", "/player/", "/hls/", "stream.")


# ══════════════════════════════════════════════════════════
#  层 0: stealth 兼容加载
# ══════════════════════════════════════════════════════════

def _load_stealth():
    """返回 stealth 异步应用函数（兼容 playwright-stealth v1/v2），否则 None。"""
    try:
        from playwright_stealth import stealth_async
        return stealth_async
    except ImportError:
        pass
    try:
        from playwright_stealth import Stealth
        s = Stealth()
        for m in ["apply_stealth_async", "use_async", "async_stealth", "__call__"]:
            fn = getattr(s, m, None)
            if callable(fn):
                return fn
    except ImportError:
        pass
    return None


# ══════════════════════════════════════════════════════════
#  层 1: BrowserManager
# ══════════════════════════════════════════════════════════

class BrowserManager:
    """
    负责启动/关闭 Chromium，应用 stealth，提供 context/page。
    自动查找 Chromium 可执行文件路径。
    """

    def __init__(self, headless: bool = HEADLESS):
        self.headless = headless
        self._pw = None
        self.browser: Optional[Browser] = None

    @staticmethod
    def _find_chromium() -> Optional[str]:
        import shutil
        # 1. 用户手动配置
        if CHROMIUM_BIN and Path(CHROMIUM_BIN).exists():
            return CHROMIUM_BIN
        # 2. PATH 查找
        for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable"):
            found = shutil.which(name)
            if found:
                return found
        # 3. 常见 Linux 路径
        for p in [
            "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/usr/bin/google-chrome",
        ]:
            if Path(p).exists():
                return p
        # 4. Windows 常见路径
        for p in [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ]:
            if Path(p).exists():
                return p
        return None

    async def start(self, pw):
        self._pw = pw
        exe = self._find_chromium()
        launch_kw = dict(
            headless=self.headless,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--headless=new" if self.headless else "",
                "--disable-web-security",          # 允许跨域 iframe 访问
                "--disable-features=IsolateOrigins,site-per-process",
                "--window-size=1920,1080",
            ],
        )
        launch_kw["args"] = [a for a in launch_kw["args"] if a]
        if exe:
            launch_kw["executable_path"] = exe
            print(f"  ✓ Chromium: {exe}")
        else:
            print("  ℹ 使用 playwright 默认 Chromium")

        self.browser = await pw.chromium.launch(**launch_kw)
        return self

    async def new_context(self, cookies: list[dict] = None) -> BrowserContext:
        ctx = await self.browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1920, "height": 1080},
            ignore_https_errors=True,
            locale="zh-CN",
        )
        if cookies:
            await ctx.add_cookies(cookies)
        return ctx

    async def new_stealth_page(self, ctx: BrowserContext) -> Page:
        page = await ctx.new_page()
        stealth = _load_stealth()
        if stealth:
            try:
                await stealth(page)
                print("  ✓ stealth 已应用")
            except Exception as e:
                print(f"  ⚠ stealth 失败（继续）: {e}")
        return page

    async def close(self):
        if self.browser:
            await self.browser.close()


# ══════════════════════════════════════════════════════════
#  层 2: CloudflareDetector
# ══════════════════════════════════════════════════════════

class CloudflareDetector:
    """
    统一检测 Cloudflare 四种拦截模式：
      1. JS Challenge     — "Just a moment..."
      2. Turnstile        — cf-turnstile widget
      3. Managed Challenge— cf-challenge-*
      4. Interstitial     — "Checking your browser"

    比只检测 challenges.cloudflare.com iframe 更可靠。
    """

    # 所有 CF 特征关键词
    _CF_KEYS = [
        "cf-turnstile",
        "just a moment",
        "checking your browser",
        "cf-browser-verification",
        "challenge-platform",
        "cf-challenge",
        "enable javascript and cookies",
        "performing security verification",
        "__cf$cv$params",
    ]

    @classmethod
    async def is_blocked(cls, page: Page) -> bool:
        """检测页面是否被 CF 拦截。"""
        try:
            html = (await page.content()).lower()
            title = (await page.title()).lower()
        except Exception:
            return False
        combined = html[:3000] + title
        return any(k in combined for k in cls._CF_KEYS)

    @classmethod
    def has_cf_iframe(cls, page: Page) -> bool:
        """检测是否存在 CF challenge iframe（补充检测）。"""
        return any(
            "challenges.cloudflare.com" in (f.url or "")
            for f in page.frames
        )

    @classmethod
    async def wait_pass(
        cls, page: Page, timeout: int = CF_WAIT_SEC, label: str = "页面"
    ) -> bool:
        """
        等待 CF 验证自动通过（stealth 模式下 JS Challenge 通常数秒内完成）。
        返回 True 表示通过，False 表示超时。
        """
        import random
        for i in range(timeout):
            blocked = await cls.is_blocked(page)
            has_cf  = cls.has_cf_iframe(page)
            if not blocked and not has_cf:
                if i > 0:
                    print(f"  ✓ {label} CF 验证通过（等待 {i+1} 秒）")
                return True

            # 模拟人类随机鼠标移动
            if i % 4 == 3:
                try:
                    await page.mouse.move(
                        random.randint(200, 1600),
                        random.randint(100, 700),
                        steps=random.randint(5, 12),
                    )
                except Exception:
                    pass

            await asyncio.sleep(1)
            if (i + 1) % 5 == 0:
                print(f"  ⏳ {label} CF 等待 {i+1}/{timeout} 秒...")

        print(f"  ✗ {label} CF 验证 {timeout} 秒内未通过")
        return False


# ══════════════════════════════════════════════════════════
#  层 3: FrameAnalyzer
# ══════════════════════════════════════════════════════════

class FrameAnalyzer:
    """
    实时维护 Frame 注册表（event-driven）。
    避免在 extract_m3u8_fallback / try_click_play 等处反复遍历 page.frames。

    注册表结构：
      _registry: dict[frame_id, {"frame": Frame, "url": str, "role": str}]
      role: "main" | "player" | "ad" | "cf" | "unknown"
    """

    def __init__(self):
        self._registry: dict[str, dict] = {}

    def attach(self, page: Page):
        """绑定到页面，监听 frameattached / framenavigated。"""
        page.on("frameattached",   self._on_attached)
        page.on("framenavigated",  self._on_navigated)
        # 注册当前已有的 frames
        for f in page.frames:
            self._register(f)

    def _register(self, frame: Frame):
        fid = str(id(frame))
        url = frame.url or ""
        role = self._classify(url)
        self._registry[fid] = {"frame": frame, "url": url, "role": role}

    def _classify(self, url: str) -> str:
        if not url or url == "about:blank":
            return "blank"
        if "challenges.cloudflare.com" in url:
            return "cf"
        host = urlparse(url).netloc if url.startswith("http") else ""
        if host == DOMAIN or host.endswith("." + DOMAIN):
            return "main"
        if any(kw in url.lower() for kw in _AD_KW):
            return "ad"
        if any(kw in url.lower() for kw in _PLAYER_PATH_KW):
            return "player"
        return "unknown"

    def _on_attached(self, frame: Frame):
        self._register(frame)

    def _on_navigated(self, frame: Frame):
        fid = str(id(frame))
        url = frame.url or ""
        role = self._classify(url)
        self._registry[fid] = {"frame": frame, "url": url, "role": role}

    @property
    def player_frames(self) -> list[Frame]:
        """返回所有识别为播放器的 frame（按注册顺序）。"""
        return [v["frame"] for v in self._registry.values() if v["role"] == "player"]

    @property
    def media_frames(self) -> list[Frame]:
        """返回播放器 + unknown 的 frame（排除 main / ad / cf）。"""
        return [
            v["frame"] for v in self._registry.values()
            if v["role"] in ("player", "unknown")
        ]

    @property
    def all_non_ad_frames(self) -> list[Frame]:
        return [
            v["frame"] for v in self._registry.values()
            if v["role"] not in ("ad", "cf", "blank")
        ]

    def dump_tree(self, page: Page):
        """以树形结构打印 frame 嵌套层级，一眼定位播放器。"""
        def _dump(frame: Frame, depth: int = 0):
            url  = frame.url or "about:blank"
            fid  = str(id(frame))
            role = self._registry.get(fid, {}).get("role", "?")
            mark = {"player": "🎬", "cf": "🔒", "ad": "📢", "main": "🏠"}.get(role, "  ")
            print(f"{'  ' * depth}{mark} [{role}] {url[:90]}")
            for child in frame.child_frames:
                _dump(child, depth + 1)

        print("  ── Frame 树 ──────────────────────────────────────")
        _dump(page.main_frame)
        print("  ────────────────────────────────────────────────")

    async def wait_for_player(self, page: Page, timeout: int = 15) -> Optional[Frame]:
        """
        等待最深层播放器 iframe 出现（最多 timeout 秒）。
        优先返回 role=player 的 frame。
        """
        for i in range(timeout):
            players = self.player_frames
            if players:
                if i > 0:
                    print(f"  ✓ 播放器 iframe 出现（等待 {i+1} 秒）")
                return players[-1]   # 最后注册的通常是最深层
            await asyncio.sleep(1)

        # 兜底：返回第一个 unknown frame
        unknowns = [v["frame"] for v in self._registry.values() if v["role"] == "unknown"]
        if unknowns:
            print(f"  ⚠ 未找到 player iframe，使用兜底 unknown frame")
            return unknowns[0]
        return None


# ══════════════════════════════════════════════════════════
#  层 4: MediaAnalyzer
# ══════════════════════════════════════════════════════════

class MediaAnalyzer:
    """
    视频源定位，按以下优先级：
      1. context.route() 拦截网络请求（最早拦截，无需等响应）
      2. video.currentSrc（播放器 API，最直接）
      3. jwplayer / videojs API
      4. DOM / script 标签正则搜索 m3u8
      5. 响应体检测 #EXTM3U（无扩展名 CDN）
    """

    _M3U8_RE = re.compile(r'https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*', re.IGNORECASE)

    _SKIP_CT  = ("image/", "font/", "text/javascript", "text/css",
                 "text/html", "application/javascript", "application/json")
    _SKIP_EXT = ('.js','.css','.html','.htm','.json','.png','.jpg',
                 '.jpeg','.gif','.svg','.woff','.woff2','.ttf','.eot','.ico')

    def __init__(self, frame_analyzer: FrameAnalyzer):
        self._fa = frame_analyzer
        self._found: Optional[str] = None
        self._found_event = asyncio.Event()

    # ── route 拦截（最高优先级）──────────────────────────────────────────────

    async def install_route(self, ctx: BrowserContext):
        """
        在 context 级别安装路由拦截。
        比 page.on("request") 更早（发出前可见），且覆盖所有 frame 的请求。
        """
        async def _handler(route: Route):
            url = route.request.url
            ct  = route.request.headers.get("accept", "")

            if self._is_m3u8_url(url):
                print(f"\n  ✓ [route] 捕获 m3u8 URL: {url[:80]}")
                self._set_found(url)

            # 必须调用 continue_，否则请求被阻断
            try:
                await route.continue_()
            except Exception:
                pass

        await ctx.route("**/*", _handler)

    # ── 响应 body 检测（无扩展名 HLS）────────────────────────────────────────

    def install_response_listener(self, page: Page):
        """监听响应 body，检测无扩展名 HLS（#EXTM3U 开头）。"""
        async def _on_response(resp):
            if self._found:
                return
            url = resp.url
            if self._is_m3u8_url(url):
                self._set_found(url)
                return
            ct = ""
            try:
                ct = resp.headers.get("content-type", "")
            except Exception:
                return
            if self._is_media_candidate(url, ct):
                try:
                    body = await resp.text()
                    if body.strip().startswith("#EXTM3U"):
                        local = self._cache_playlist(url, body)
                        print(f"\n  ✓ [response] 无扩展名 HLS，已缓存: {local}")
                        self._set_found(local)
                except Exception:
                    pass

        page.on("response", _on_response)

    def _set_found(self, url: str):
        if not self._found:
            self._found = url
            self._found_event.set()

    async def wait_for_m3u8(self, timeout: int = M3U8_ROUTE_WAIT) -> Optional[str]:
        try:
            await asyncio.wait_for(self._found_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        return self._found

    # ── 播放器 API（video.currentSrc — 最直接）──────────────────────────────

    async def query_player_api(self) -> Optional[str]:
        """
        在所有非广告 frame 中查询 video.currentSrc / jwplayer / videojs。
        这是「先找播放器再找媒体源」的核心。
        """
        scripts = [
            # HTML5 video（最通用）
            "(() => { const v = document.querySelector('video'); return v ? (v.currentSrc || v.src || null) : null; })()",
            # JWPlayer
            "(() => { try { return jwplayer().getPlaylistItem().file || null; } catch(e) { return null; } })()",
            # VideoJS
            "(() => { try { return videojs(document.querySelector('.video-js')).currentSrc() || null; } catch(e) { return null; } })()",
            # Plyr
            "(() => { try { return document.querySelector('.plyr video')?.currentSrc || null; } catch(e) { return null; } })()",
        ]
        for frame in self._fa.all_non_ad_frames:
            for script in scripts:
                try:
                    r = await frame.evaluate(script)
                    if r and isinstance(r, str) and r.startswith("http"):
                        print(f"  ✓ 播放器 API: {r[:80]}")
                        return r
                except Exception:
                    pass
        return None

    # ── DOM / HTML 正则搜索 ────────────────────────────────────────────────

    async def search_dom(self) -> Optional[str]:
        """在所有非广告 frame 的 DOM 中正则搜索 m3u8 URL。"""
        for frame in self._fa.all_non_ad_frames:
            try:
                html = await frame.content()
                m = self._M3U8_RE.search(html)
                if m:
                    return m.group(0)
            except Exception:
                pass

        # script 标签内容
        try:
            # 只在 page main_frame 里做（避免找不到 page 引用）
            for frame in self._fa.all_non_ad_frames:
                try:
                    r = await frame.evaluate("""() => {
                        for (const s of document.querySelectorAll('script')) {
                            const m = s.textContent.match(/https?:\\/\\/[^\\s"'<>]+\\.m3u8/i);
                            if (m) return m[0];
                        }
                        return null;
                    }""")
                    if r:
                        return r
                except Exception:
                    pass
        except Exception:
            pass
        return None

    # ── 全量提取（fallback）────────────────────────────────────────────────

    async def extract_all(self) -> Optional[str]:
        """按优先级尝试所有提取方式。"""
        # 1. 如果 route 已捕获
        if self._found:
            return self._found

        # 2. 播放器 API（video.currentSrc）
        r = await self.query_player_api()
        if r:
            return r

        # 3. DOM 正则
        r = await self.search_dom()
        if r:
            return r

        return None

    # ── 触发播放 ───────────────────────────────────────────────────────────

    async def trigger_play(self, page: Page):
        """在主页面和播放器 frame 中触发视频播放。"""
        # 滚动到视频
        try:
            await page.evaluate(
                "document.querySelector('video')?.scrollIntoView({behavior:'instant',block:'center'})"
            )
        except Exception:
            pass

        dom_selectors = [
            "video", ".vjs-big-play-button", ".jw-display-icon-container",
            ".jw-icon-display", ".play-btn", ".btn-play",
            "[class*='play']", "[aria-label*='play' i]", "#player",
        ]
        # 在所有媒体 frame 中尝试点击
        for frame in [page.main_frame] + self._fa.media_frames:
            for sel in dom_selectors:
                try:
                    el = frame.locator(sel).first
                    if await el.count() > 0:
                        try:
                            await el.scroll_into_view_if_needed()
                        except Exception:
                            pass
                        await el.click(timeout=2500)
                        print(f"  ✓ 点击播放: {sel} ({(frame.url or '')[:60]})")
                        return
                except Exception:
                    pass

        # JS 强制播放
        print("  ▶ DOM 点击未命中，JS 强制播放...")
        for frame in [page.main_frame] + self._fa.media_frames:
            try:
                n = await frame.evaluate("""() => {
                    const vs = document.querySelectorAll('video');
                    vs.forEach(v => { try { v.play(); } catch(e) {} });
                    return vs.length;
                }""")
                if n:
                    print(f"  ✓ JS video.play() 触发 {n} 个视频")
                    return
            except Exception:
                pass

        # 播放器 API
        for js, lbl in [
            ("try{jwplayer().play();return true}catch(e){return false}", "jwplayer()"),
            ("try{videojs(document.querySelector('.video-js')).play();return true}catch(e){return false}",
             "videojs()"),
        ]:
            try:
                if await page.evaluate(f"(()=>{{ {js} }})()"):
                    print(f"  ✓ {lbl}.play()")
                    return
            except Exception:
                pass

        print("  ⚠ 未找到可触发的播放元素")

    # ── 内部工具 ──────────────────────────────────────────────────────────

    @staticmethod
    def _is_m3u8_url(url: str, ct: str = "") -> bool:
        if ".m3u8" in url.lower():
            return True
        ct = ct.lower()
        return "mpegurl" in ct or "x-mpegurl" in ct

    @staticmethod
    def _is_media_candidate(url: str, ct: str) -> bool:
        ct_l = ct.lower()
        if any(ct_l.startswith(t) for t in MediaAnalyzer._SKIP_CT):
            return False
        url_l = url.lower().split("?")[0]
        return not any(url_l.endswith(e) for e in MediaAnalyzer._SKIP_EXT)

    @staticmethod
    def _cache_playlist(url: str, body: str) -> str:
        """保存 m3u8 到本地（相对 URL 转绝对），规避 CDN token 短暂有效问题。"""
        base = url.rsplit("/", 1)[0] + "/"
        lines = []
        for line in body.splitlines():
            s = line.strip()
            if s and not s.startswith("#") and not s.startswith("http"):
                line = urljoin(base, s)
            lines.append(line)
        local = DOWNLOAD_DIR / "playlist_cache.m3u8"
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_text("\n".join(lines), encoding="utf-8")
        return str(local)


# ══════════════════════════════════════════════════════════
#  Cookie 工具
# ══════════════════════════════════════════════════════════

def _parse_cookie_str(s: str, domain: str) -> list[dict]:
    result = []
    for part in s.split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        name, _, value = part.partition("=")
        result.append({
            "name": name.strip(), "value": value.strip(),
            "domain": domain, "path": "/",
            "httpOnly": False, "secure": True, "sameSite": "None",
        })
    return result


def _normalize_cookies(raw: list[dict]) -> list[dict]:
    return [{
        "name":     c.get("name", ""),
        "value":    c.get("value", ""),
        "domain":   c.get("domain", DOMAIN).lstrip("."),
        "path":     c.get("path", "/"),
        "httpOnly": c.get("httpOnly", False),
        "secure":   c.get("secure", True),
        "sameSite": c.get("sameSite", "None"),
    } for c in raw]


def has_cf_clearance(cookies: list[dict]) -> bool:
    return any(c.get("name") == "cf_clearance" for c in cookies)


def load_cookies_from_file(path) -> Optional[list[dict]]:
    p = Path(path)
    if not p.exists():
        return None
    content = p.read_text(encoding="utf-8").strip()
    if not content:
        return None
    try:
        data = json.loads(content)
        if isinstance(data, str):
            content = data            # 外层引号包裹，剥开继续
        elif isinstance(data, dict) and "cookies" in data:
            remaining = 3000 - (time.time() - data.get("saved_at", 0))
            if remaining <= 0:
                print(f"  ⚠ {p.name} Cookie 已过期")
                return None
            c = data["cookies"]
            print(f"  ✓ {p.name}: {len(c)} 个 Cookie（剩余 {int(remaining/60)} 分钟）")
            return c
        elif isinstance(data, list):
            c = _normalize_cookies(data)
            print(f"  ✓ {p.name}: {len(c)} 个 Cookie（JSON 数组）")
            return c
    except json.JSONDecodeError:
        pass
    if "=" in content:
        c = _parse_cookie_str(content, DOMAIN)
        if c:
            print(f"  ✓ {p.name}: {len(c)} 个 Cookie（纯文本）")
            return c
    print(f"  ⚠ {p.name} 格式无法识别")
    return None


def save_cookies(cookies: list[dict]):
    COOKIE_CACHE.write_text(
        json.dumps({"saved_at": time.time(), "cookies": cookies}, indent=2, ensure_ascii=False)
    )
    print(f"  ✓ Cookie 已缓存: {COOKIE_CACHE}")


# ══════════════════════════════════════════════════════════
#  CF Cookie 获取方案
# ══════════════════════════════════════════════════════════

async def _stealth_get_cookies(url: str) -> Optional[list[dict]]:
    """方案 A：playwright-stealth 自动获取 cf_clearance。"""
    import random
    stealth = _load_stealth()
    if not stealth:
        print("  ✗ playwright-stealth 未安装，跳过")
        return None

    bm = BrowserManager(headless=True)
    async with async_playwright() as pw:
        await bm.start(pw)
        ctx = await bm.new_context()
        page = await bm.new_stealth_page(ctx)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception:
            pass

        print(f"  ▶ 等待 CF 验证（最多 {CF_WAIT_SEC} 秒）...")
        for i in range(CF_WAIT_SEC):
            await asyncio.sleep(1)
            raw = await ctx.cookies()
            if any(c.get("name") == "cf_clearance" for c in raw):
                print(f"  ✓ 第 {i+1} 秒获得 cf_clearance")
                cookies = _normalize_cookies(raw)
                await bm.close()
                return cookies
            if i % 4 == 3:
                try:
                    await page.mouse.move(
                        random.randint(200, 1600), random.randint(100, 700),
                        steps=random.randint(5, 12),
                    )
                except Exception:
                    pass
        await bm.close()
    print("  ✗ stealth 方案未获得 cf_clearance")
    return None


async def _flaresolverr_get_cookies(url: str) -> Optional[list[dict]]:
    """方案 B：FlareSolverr 获取 cf_clearance。"""
    print(f"  ▶ FlareSolverr ({FLARESOLVERR_URL})...")
    try:
        async with httpx.AsyncClient(timeout=FLARESOLVERR_TIMEOUT + 10) as c:
            resp = await c.post(FLARESOLVERR_URL, json={
                "cmd": "request.get", "url": url,
                "maxTimeout": FLARESOLVERR_TIMEOUT * 1000,
            })
        if not resp.text.strip():
            print("  ✗ FlareSolverr 返回空响应")
            return None
        data = resp.json()
    except httpx.ConnectError:
        print("  ✗ FlareSolverr 未运行（启动: docker run -d -p 8191:8191 ghcr.io/flaresolverr/flaresolverr:latest）")
        return None
    except Exception as e:
        print(f"  ✗ FlareSolverr 异常: {e}")
        return None
    if data.get("status") != "ok":
        print(f"  ✗ FlareSolverr: {data.get('message')}")
        return None
    raw = data.get("solution", {}).get("cookies", [])
    cookies = _normalize_cookies(raw)
    if has_cf_clearance(cookies):
        print("  ✓ FlareSolverr 获取成功")
        return cookies
    print("  ✗ FlareSolverr 未获得 cf_clearance")
    return None


async def acquire_cookies() -> list[dict]:
    """按优先级获取 CF Cookie：文件 → stealth → FlareSolverr → CapSolver。"""
    # 0. 文件扫描
    for fname in COOKIE_FILES:
        c = load_cookies_from_file(fname)
        if c and has_cf_clearance(c):
            print(f"  ✓ 使用 {fname}")
            return c
        elif c:
            print(f"  ⚠ {fname} 无 cf_clearance")

    print("\n  ℹ 开始自动获取 Cookie...")

    # A. stealth
    c = await _stealth_get_cookies(TARGET_URL)
    if c:
        save_cookies(c)
        return c

    # B. FlareSolverr
    c = await _flaresolverr_get_cookies(TARGET_URL)
    if c:
        save_cookies(c)
        return c

    print("\n  ✗ 自动获取失败")
    print("  手动获取: 浏览器访问目标页 → F12 → 复制 Cookie → 存入 cf_cookies.json")
    sys.exit(1)


# ══════════════════════════════════════════════════════════
#  服务器切换
# ══════════════════════════════════════════════════════════

async def click_server(page: Page, server: str) -> bool:
    """点击视频线路按钮（DS/TV/ST/JPA 等）。"""
    if not server:
        return False
    selectors = [
        f"button:text-is('{server}')", f"a:text-is('{server}')",
        f"li:text-is('{server}')",     f"span:text-is('{server}')",
        f"div:text-is('{server}')",    f"[class*='server']:text-is('{server}')",
        f"[class*='source']:text-is('{server}')", f":text('{server}')",
    ]
    for sel in selectors:
        try:
            el = page.locator(sel).first
            if await el.count() > 0:
                await el.click(timeout=3000)
                print(f"  ✓ 切换到 {server} 线路")
                await asyncio.sleep(5)   # 等待播放器 iframe 重新加载
                return True
        except Exception:
            continue

    # 调试输出
    try:
        texts = await page.evaluate("""() => {
            return [...document.querySelectorAll('button,a,li,[class*="server"],[class*="source"]')]
                   .map(e => e.textContent.trim()).filter(t => t && t.length < 30);
        }""")
        print(f"  ⚠ 未找到 '{server}' 按钮，检测到: {list(dict.fromkeys(texts))[:15]}")
    except Exception:
        print(f"  ⚠ 未找到 '{server}' 按钮")
    return False


# ══════════════════════════════════════════════════════════
#  Downloader
# ══════════════════════════════════════════════════════════

class Downloader:
    """ffmpeg 下载 + 纯 Python 备用。"""

    @staticmethod
    def _find_ffmpeg() -> Optional[str]:
        import shutil
        if FFMPEG_PATH and Path(FFMPEG_PATH).exists():
            return FFMPEG_PATH
        for name in ("ffmpeg", "ffmpeg.exe"):
            found = shutil.which(name)
            if found:
                return found
        for p in [r"C:\ffmpeg\bin\ffmpeg.exe",
                  r"C:\Program Files\ffmpeg\bin\ffmpeg.exe"]:
            if Path(p).exists():
                return p
        return None

    @staticmethod
    def _ffmpeg_dl(m3u8: str, out: Path, ffbin: str) -> bool:
        is_local = not m3u8.startswith("http") and Path(m3u8).exists()
        if is_local:
            print(f"  使用本地缓存播放列表: {m3u8}")
        cmd = [
            ffbin, "-y",
            "-allowed_extensions", "ALL",
            "-headers", f"Referer: {TARGET_URL}\r\nUser-Agent: {USER_AGENT}\r\n",
            "-i", m3u8,
            "-c", "copy",
            "-bsf:a", "aac_adtstoasc",
            str(out),
        ]
        print(f"  ffmpeg: {ffbin}")
        return subprocess.run(cmd).returncode == 0

    @staticmethod
    async def _python_dl(m3u8: str, out: Path) -> bool:
        """纯 Python 分片下载（不支持 AES 加密流）。"""
        import urllib.parse
        headers = {"Referer": TARGET_URL, "User-Agent": USER_AGENT}
        is_local = not m3u8.startswith("http") and Path(m3u8).exists()
        base = (Path(m3u8).parent.as_uri() + "/") if is_local else (m3u8.rsplit("/", 1)[0] + "/")

        print("  使用内置 Python 下载器")
        async with httpx.AsyncClient(headers=headers, timeout=30, follow_redirects=True) as c:
            playlist = (Path(m3u8).read_text() if is_local else (await c.get(m3u8)).text)
            if "#EXT-X-KEY" in playlist:
                print("  ⚠ AES 加密流，Python 下载器不支持，请安装 ffmpeg")
                return False
            segs = []
            for line in playlist.splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    segs.append(line if line.startswith("http") else urllib.parse.urljoin(base, line))
            if not segs:
                print("  ✗ 无分片")
                return False

            print(f"  ✓ {len(segs)} 个分片，开始下载...")
            out.parent.mkdir(parents=True, exist_ok=True)
            ts_out = out.with_suffix(".ts")
            with open(ts_out, "wb") as f:
                for i, seg in enumerate(segs, 1):
                    for attempt in range(3):
                        try:
                            f.write((await c.get(seg, timeout=20)).content)
                            break
                        except Exception as e:
                            if attempt == 2:
                                print(f"\n  ✗ 分片 {i} 失败: {e}")
                                return False
                            await asyncio.sleep(1)
                    if i % 10 == 0 or i == len(segs):
                        print(f"  ▶ {i}/{len(segs)} ({i*100//len(segs)}%)", end="\r")
            print(f"\n  ✓ 完成: {ts_out}")
            print(f'  提示: ffmpeg -i "{ts_out}" -c copy "{out}"')
            return True

    @classmethod
    async def download(cls, m3u8: str, out: Path) -> bool:
        out.parent.mkdir(parents=True, exist_ok=True)
        ffmpeg = cls._find_ffmpeg()
        if ffmpeg:
            return cls._ffmpeg_dl(m3u8, out, ffmpeg)
        print("  ⚠ 未找到 ffmpeg，使用内置下载器")
        return await cls._python_dl(m3u8, out)


# ══════════════════════════════════════════════════════════
#  播放器直接提取（PLAYER_URL 快捷路径）
# ══════════════════════════════════════════════════════════

async def extract_from_player_url(player_url: str) -> Optional[str]:
    """直接加载播放器 embed 页面（跳过主页 / Cookie）。"""
    print(f"  ▶ 直接播放器模式: {player_url[:80]}")
    bm = BrowserManager()
    fa = FrameAnalyzer()

    async with async_playwright() as pw:
        await bm.start(pw)
        ctx = await bm.new_context()

        ma = MediaAnalyzer(fa)
        await ma.install_route(ctx)

        page = await bm.new_stealth_page(ctx)
        fa.attach(page)
        ma.install_response_listener(page)

        try:
            await page.goto(player_url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            print(f"  ⚠ 加载超时（继续）: {e}")

        await CloudflareDetector.wait_pass(page, PLAYER_CF_WAIT, "播放器")
        await asyncio.sleep(2)

        # 打印 frame 树
        fa.dump_tree(page)

        m3u8 = await ma.wait_for_m3u8(timeout=5)
        if not m3u8:
            await ma.trigger_play(page)
            await asyncio.sleep(3)
            m3u8 = await ma.wait_for_m3u8(timeout=10)

        if not m3u8:
            m3u8 = await ma.extract_all()

        if not m3u8:
            shot = DOWNLOAD_DIR / "debug_player.png"
            await page.screenshot(path=str(shot), full_page=True)
            print(f"  截图: {shot}")

        await bm.close()
    return m3u8


# ══════════════════════════════════════════════════════════
#  主流程
# ══════════════════════════════════════════════════════════

async def main():
    print("supjav.com 视频下载器 — 专家重构版 v2")
    print("=" * 64)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    # ── PLAYER_URL 快捷路径 ───────────────────────────────────────────────
    if PLAYER_URL:
        m3u8 = await extract_from_player_url(PLAYER_URL)
        if not m3u8:
            print("\n  ✗ 无法从播放器 URL 提取视频流")
            sys.exit(1)
        print(f"\n  ✓ 视频流: {m3u8}")
        out = DOWNLOAD_DIR / "video.mp4"
        if not await Downloader.download(m3u8, out):
            print(f'\n  手动: ffmpeg -i "{m3u8}" -c copy downloads/video.mp4')
            sys.exit(1)
        print(f"\n  ✓ 下载完成: {out}")
        return

    # ── 第一步：Cookie ────────────────────────────────────────────────────
    print("\n[1/3] 获取 Cloudflare Cookie...")
    cookies = await acquire_cookies()
    cf = next((c for c in cookies if c["name"] == "cf_clearance"), None)
    print(f"  ✓ cf_clearance: {cf['value'][:40]}..." if cf else "  ⚠ 无 cf_clearance")

    # ── 第二步：加载页面，提取视频流 ──────────────────────────────────────
    print(f"\n[2/3] 加载页面，定位视频流...")
    m3u8: Optional[str] = None
    fallback_player: Optional[str] = None

    bm = BrowserManager()
    fa = FrameAnalyzer()

    async with async_playwright() as pw:
        await bm.start(pw)
        ctx = await bm.new_context(cookies=cookies)

        # 安装 route 拦截（在 context 级别，覆盖所有 frame）
        ma = MediaAnalyzer(fa)
        await ma.install_route(ctx)

        page = await bm.new_stealth_page(ctx)
        fa.attach(page)
        ma.install_response_listener(page)

        print(f"  ▶ 加载页面: {TARGET_URL}")
        try:
            await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            print(f"  ⚠ 加载超时（继续）: {e}")

        # 等待主站 CF
        await CloudflareDetector.wait_pass(page, CF_WAIT_SEC, "主站")

        # CF 拦截检测（cf_clearance 失效时终止并指导用户）
        if await CloudflareDetector.is_blocked(page):
            shot = DOWNLOAD_DIR / "debug_cf.png"
            await page.screenshot(path=str(shot))
            await bm.close()
            print(f"\n  ✗ Cloudflare 仍在拦截（截图: {shot}）")
            print("  cf_clearance 与 IP 绑定，约1小时失效。")
            print("  解决：浏览器访问 → F12 → 复制 Cookie → 存入 cf_cookies.json")
            if COOKIE_CACHE.exists():
                COOKIE_CACHE.unlink()
            sys.exit(1)

        # 切换视频服务器（DS/TV/ST 等），在点击前 route 已就绪
        if VIDEO_SERVER:
            print(f"\n  ▶ 切换到 {VIDEO_SERVER} 线路...")
            await click_server(page, VIDEO_SERVER)
            # click_server 内部已等待5秒（DS 播放器 iframe 加载期间）
            # route 会自动捕获 VideoJS 预加载请求

        # 等待播放器 CF
        await CloudflareDetector.wait_pass(page, PLAYER_CF_WAIT, "播放器")

        # 打印 frame 树（可视化 iframe 嵌套结构）
        fa.dump_tree(page)

        # 检查 route 是否已捕获
        m3u8 = await ma.wait_for_m3u8(timeout=3)

        if not m3u8:
            print("\n  ▶ 触发视频播放...")
            await ma.trigger_play(page)
            await asyncio.sleep(3)
            m3u8 = await ma.wait_for_m3u8(timeout=15)

        # fallback：全量搜索
        if not m3u8:
            print("  ▶ route 未捕获，尝试全量搜索...")
            m3u8 = await ma.extract_all()

        # 若仍未找到，记录播放器 iframe URL 作为备用
        if not m3u8:
            player_frame = await fa.wait_for_player(page, timeout=5)
            if player_frame:
                fallback_player = player_frame.url
                print(f"  ▶ 记录播放器 URL 备用: {fallback_player[:80]}")

        if m3u8:
            print(f"\n  ✓ 视频流: {m3u8}")
        await bm.close()

    # 若常规流程失败且有备用播放器 URL
    if not m3u8 and fallback_player:
        print(f"\n  ▶ 直接加载播放器重试: {fallback_player[:80]}")
        m3u8 = await extract_from_player_url(fallback_player)
        if m3u8:
            print(f"\n  ✓ 视频流: {m3u8}")

    if not m3u8:
        print("\n  ✗ 未找到视频流")
        print("  提示：将 HEADLESS=False 后重运行，观察浏览器行为")
        print("  或将 PLAYER_URL 设为 F12 中找到的播放器 embed URL")
        sys.exit(1)

    # ── 第三步：下载 ──────────────────────────────────────────────────────
    print(f"\n[3/3] 开始下载...")
    out = DOWNLOAD_DIR / "video.mp4"
    print(f"  输出: {out}")
    ok = await Downloader.download(m3u8, out)
    if ok:
        print(f"\n  ✓ 下载完成: {out}")
    else:
        print(f'\n  ✗ 下载失败，手动执行:')
        print(f'  ffmpeg -allowed_extensions ALL -i "{m3u8}" -c copy downloads/video.mp4')
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
