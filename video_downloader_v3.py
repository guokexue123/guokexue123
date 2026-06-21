#!/usr/bin/env python3
"""
supjav.com 视频下载器 v3 — 自动绕过 CF，无需手动 Cookie

CF 绕过三层递进策略：
  A. curl-cffi     — TLS/HTTP2 指纹伪装，纯 HTTP 层，最快（无需浏览器）
  B. stealth 浏览器 — playwright-stealth v2，等待 JS Challenge 自动完成
  C. 显示模式       — HEADLESS=False，用户手动通过（最终保底）

四层工作流程：
  CfBuster  →  PageNavigator  →  M3u8Catcher  →  VideoDownloader

依赖安装：
  pip install playwright playwright-stealth curl-cffi httpx
  playwright install chromium    # 或设置 CHROMIUM_BIN
  apt install ffmpeg             # 或 winget install ffmpeg
"""

import asyncio
import json
import re
import shutil
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

# ═══════════════════════════════════════════════════════════
#  用户配置
# ═══════════════════════════════════════════════════════════

TARGET_URL   = "https://supjav.com/132824.html"
VIDEO_SERVER = "DS"            # DS / TV / JPA / ST
DOWNLOAD_DIR = Path("downloads")
OUTPUT_FILE  = DOWNLOAD_DIR / "video_5s.mp4"

# 只下载前 N 秒（0 = 全部）
DOWNLOAD_SECONDS = 5

# 调试开关：True 显示浏览器窗口（CF 验证失败时手动介入）
HEADLESS = True

# CF 超时：headless=True 时 30 秒，headless=False 时 300 秒
CF_TIMEOUT_HEADLESS = 30
CF_TIMEOUT_VISIBLE  = 300

# 等待 m3u8 的最长秒数（DS iframe 加载可能需要 20-30 秒）
M3U8_WAIT_SEC = 60

# Chromium 路径（留空自动查找）
CHROMIUM_BIN = ""

# User-Agent（与 curl-cffi impersonate 对应）
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# ═══════════════════════════════════════════════════════════

DOMAIN = urlparse(TARGET_URL).netloc

_PLAYER_KW  = ("/e/", "/embed", "/player/", "/hls/", "stream.", "playmogo.", "lk1.")
_AD_KW      = ("googlesyndication.", "doubleclick.", "adnxs.", "amazon-adsystem.",
               "advertising.", "tracker.", "tracking.", "analytics.")


# ───────────────────────────────────────────────────────────
#  Layer 0 — CF Buster（三档自动绕过）
# ───────────────────────────────────────────────────────────

class CfBuster:
    """
    自动绕过 Cloudflare 并返回可注入浏览器的 Cookie 列表。

    策略 A — curl-cffi（推荐）：
      通过 TLS/HTTP2 指纹伪装直接发送 HTTP 请求。
      Chrome 的 JA3/JA4 TLS 指纹 + HTTP2 SETTINGS 帧完全匹配，
      Cloudflare 认为是真实 Chrome，通常直接返回 200 + cf_clearance。
      适用：JS Challenge、Browser Integrity Check。
      不适用：Managed Challenge（含 CAPTCHA）。

    策略 B — stealth 浏览器：
      playwright-stealth 修补 navigator.webdriver 等 20+ 检测点，
      让 Chromium 看起来像真实 Chrome。
      CF JS Challenge 通常在 3-10 秒内自动完成。
      适用：JS Challenge、Turnstile 自动模式。
      不适用：Turnstile 需人工点击（极少见于 supjav.com）。

    策略 C — 显示模式：
      HEADLESS=False，用户手动点击 CF 验证。
    """

    @staticmethod
    async def get_cookies(url: str, headless: bool) -> list[dict]:
        """按 A→B→C 顺序获取 CF Cookie，返回 playwright 格式列表。"""
        # ── 策略 A: curl-cffi ────────────────────────────────────────────
        print("  [A] curl-cffi TLS 指纹伪装...")
        result = await CfBuster._curl_cffi(url)
        if result:
            print(f"  ✓ [A] curl-cffi 成功，获得 {len(result)} 个 Cookie")
            return result

        # ── 策略 B: stealth 浏览器 ───────────────────────────────────────
        print("  [B] stealth 浏览器（playwright-stealth v2）...")
        cf_timeout = CF_TIMEOUT_VISIBLE if not headless else CF_TIMEOUT_HEADLESS
        result = await CfBuster._stealth_browser(url, headless=True, timeout=cf_timeout)
        if result:
            print(f"  ✓ [B] stealth 浏览器成功，获得 {len(result)} 个 Cookie")
            return result

        # ── 策略 C: 显示模式 ─────────────────────────────────────────────
        if headless:
            print("  [C] 无头模式失败，切换显示模式（弹出浏览器窗口）...")
        else:
            print("  [C] 显示模式：请在浏览器中手动通过 CF 验证...")
        result = await CfBuster._stealth_browser(url, headless=False,
                                                  timeout=CF_TIMEOUT_VISIBLE)
        if result:
            print(f"  ✓ [C] 显示模式成功，获得 {len(result)} 个 Cookie")
            return result

        print("\n  ✗ 三种 CF 绕过方法均失败")
        print("  可能原因：")
        print("    1. IP 被 Cloudflare 封禁（数据中心 IP 常见）")
        print("    2. supjav.com 启用了 Managed Challenge（需人工 CAPTCHA）")
        print("    3. 网络连接失败")
        sys.exit(1)

    # ── 策略 A 实现 ──────────────────────────────────────────────────────

    @staticmethod
    async def _curl_cffi(url: str) -> Optional[list[dict]]:
        """curl-cffi 伪装 Chrome TLS 指纹绕过 CF JS Challenge。"""
        try:
            from curl_cffi.requests import AsyncSession
        except ImportError:
            print("  ⚠ curl-cffi 未安装 (pip install curl-cffi)")
            return None

        try:
            headers = {
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Accept-Encoding": "gzip, deflate, br",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
                "Upgrade-Insecure-Requests": "1",
            }
            async with AsyncSession(impersonate="chrome124") as sess:
                resp = await sess.get(url, headers=headers, timeout=20,
                                      allow_redirects=True)

            if resp.status_code in (403, 503) and "cf-mitigated" in str(resp.headers):
                print(f"  ⚠ [A] CF 仍在拦截 (status={resp.status_code})，curl-cffi 不够")
                return None

            # 提取 cookies
            raw = dict(resp.cookies)
            if not raw.get("cf_clearance"):
                if resp.status_code == 200:
                    # 某些情况下没有 cf_clearance 但页面已正常返回
                    print(f"  ℹ [A] 状态 200，无 cf_clearance（站点可能不需要）")
                    # 仍然继续，注入现有 cookies
                else:
                    print(f"  ⚠ [A] 未获得 cf_clearance (status={resp.status_code})")
                    return None

            cookies = CfBuster._raw_to_playwright(raw, DOMAIN)
            return cookies if cookies else None

        except Exception as e:
            print(f"  ⚠ [A] curl-cffi 异常: {e}")
            return None

    # ── 策略 B 实现 ──────────────────────────────────────────────────────

    @staticmethod
    async def _stealth_browser(url: str, headless: bool,
                                timeout: int) -> Optional[list[dict]]:
        """启动 stealth 浏览器，等待 CF JS Challenge 自动完成。"""
        import random

        exe = _find_chromium()
        if not exe:
            print("  ✗ 未找到 Chromium")
            return None

        async with async_playwright() as pw:
            launch_args = [
                "--no-sandbox", "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-web-security",
                "--window-size=1920,1080",
            ]
            if headless:
                launch_args.append("--headless=new")

            browser = await pw.chromium.launch(
                headless=headless,
                executable_path=exe,
                args=launch_args,
            )
            ctx = await browser.new_context(
                user_agent=USER_AGENT,
                viewport={"width": 1920, "height": 1080},
                ignore_https_errors=True,
                locale="zh-CN",
            )

            page = await ctx.new_page()
            await _apply_stealth(page)
            if not headless:
                print(f"  ℹ 浏览器已弹出，请手动通过 CF 验证（最多 {timeout} 秒）")

            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                print(f"  ⚠ 页面加载超时（继续等待）: {e}")

            # 等待 cf_clearance
            for i in range(timeout):
                cookies_raw = await ctx.cookies()
                has_cf = any(c.get("name") == "cf_clearance" for c in cookies_raw)
                has_content = await _page_has_real_content(page)

                if has_cf or has_content:
                    cookies = CfBuster._normalize_cookies(cookies_raw)
                    await browser.close()
                    return cookies

                # 模拟人类鼠标
                if i % 5 == 4:
                    try:
                        await page.mouse.move(
                            random.randint(200, 1400), random.randint(100, 600),
                            steps=random.randint(5, 15)
                        )
                    except Exception:
                        pass

                await asyncio.sleep(1)
                if (i + 1) % 10 == 0:
                    print(f"  ⏳ CF 等待 {i+1}/{timeout} 秒...")

            await browser.close()
            print(f"  ✗ stealth 浏览器 {timeout} 秒内未通过")
            return None

    # ── Cookie 格式转换 ──────────────────────────────────────────────────

    @staticmethod
    def _raw_to_playwright(raw: dict, domain: str) -> list[dict]:
        """将 curl-cffi cookies dict 转为 playwright add_cookies 格式。"""
        return [{
            "name": k, "value": v,
            "domain": domain, "path": "/",
            "httpOnly": False, "secure": True, "sameSite": "None",
        } for k, v in raw.items()]

    @staticmethod
    def _normalize_cookies(raw: list[dict]) -> list[dict]:
        """playwright ctx.cookies() 列表标准化。"""
        return [{
            "name":     c.get("name", ""),
            "value":    c.get("value", ""),
            "domain":   c.get("domain", DOMAIN).lstrip("."),
            "path":     c.get("path", "/"),
            "httpOnly": c.get("httpOnly", False),
            "secure":   c.get("secure", True),
            "sameSite": c.get("sameSite", "None"),
        } for c in raw]


# ───────────────────────────────────────────────────────────
#  Layer 1 — PageNavigator（加载页面 + 点击 DS 按钮）
# ───────────────────────────────────────────────────────────

class PageNavigator:
    """
    使用 CF Cookie 加载目标页，点击服务器选择按钮。
    内置重试、networkidle 等待、多选择器策略。
    """

    def __init__(self, ctx: BrowserContext, page: Page):
        self.ctx  = ctx
        self.page = page

    async def load(self) -> bool:
        """加载目标页面，等待 CF 通过。"""
        print(f"  ▶ 加载: {TARGET_URL}")
        try:
            await self.page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            print(f"  ⚠ 加载超时（继续）: {e}")

        # 等待 CF 通过（此时 cookies 已注入，通常立刻通过）
        cf_timeout = CF_TIMEOUT_VISIBLE if not HEADLESS else CF_TIMEOUT_HEADLESS
        ok = await self._wait_cf(cf_timeout)
        if not ok:
            shot = DOWNLOAD_DIR / "debug_cf.png"
            DOWNLOAD_DIR.mkdir(exist_ok=True)
            await self.page.screenshot(path=str(shot), full_page=True)
            print(f"  ✗ CF 验证失败，截图: {shot}")
            return False
        return True

    async def click_server(self, server: str) -> bool:
        """点击服务器按钮（networkidle + 多策略）。"""
        if not server:
            return False

        print(f"  ▶ 切换到 {server} 服务器...")

        # 等待页面 JS 完全渲染
        try:
            await self.page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass

        # 等待服务器按钮出现（任意一个匹配即可）
        for sel in (f":text('{server}')", "[class*='server']",
                    "[class*='source']", "[class*='sorc']", "li"):
            try:
                await self.page.wait_for_selector(sel, timeout=3000)
                break
            except Exception:
                continue

        # 微滚动触发懒加载（不超过 300px 避免滚过按钮行）
        try:
            await self.page.evaluate(
                "window.scrollTo(0, Math.min(300, document.body.scrollHeight * 0.1))"
            )
            await asyncio.sleep(0.3)
        except Exception:
            pass

        # 多选择器尝试点击
        for sel in (
            f":text-is('{server}')",
            f":text('{server}')",
            f"li:has-text('{server}')",
            f"button:has-text('{server}')",
            f"a:has-text('{server}')",
            f"span:has-text('{server}')",
            f"[class*='{server.lower()}']",
            f"[class*='server']:has-text('{server}')",
            f"[class*='source']:has-text('{server}')",
        ):
            try:
                el = self.page.locator(sel).first
                if await el.count() > 0:
                    try:
                        await el.scroll_into_view_if_needed()
                    except Exception:
                        pass
                    await el.click(timeout=3000)
                    print(f"  ✓ 点击 {server}（{sel}）")
                    await asyncio.sleep(2)  # 等 iframe 开始加载
                    return True
            except Exception:
                continue

        # JS 兜底：遍历所有叶节点精确匹配文本
        try:
            hit = await self.page.evaluate(f"""(s) => {{
                for (const el of document.querySelectorAll('*')) {{
                    if (el.childElementCount === 0 &&
                        el.textContent.trim() === s) {{
                        el.click();
                        return el.tagName + '.' + el.className;
                    }}
                }}
                return null;
            }}""", server)
            if hit:
                print(f"  ✓ JS 兜底点击 {server}（{hit}）")
                await asyncio.sleep(2)
                return True
        except Exception:
            pass

        # 诊断输出
        try:
            leaves = await self.page.evaluate("""() =>
                [...document.querySelectorAll('*')]
                .filter(e => e.childElementCount === 0 && e.textContent.trim())
                .map(e => {
                    const c = [...e.classList].join('.');
                    return e.tagName.toLowerCase() +
                           (c ? '.' + c : '') + ': "' +
                           e.textContent.trim().slice(0, 40) + '"';
                }).slice(0, 30)
            """)
            print(f"  ⚠ 未找到 {server} 按钮，当前叶节点（前30条）:")
            for l in (leaves or []):
                print(f"    {l}")
        except Exception:
            pass

        # 无论是否找到按钮，截图保留现场
        try:
            shot = DOWNLOAD_DIR / "debug_server.png"
            await self.page.screenshot(path=str(shot), full_page=True)
            print(f"  截图: {shot}")
        except Exception:
            pass

        print(f"  ⚠ {server} 按钮未找到，继续尝试默认播放器...")
        return False

    async def _wait_cf(self, timeout: int) -> bool:
        """等待 CF 通过（DOM 结构检测，无误报）。"""
        import random
        for i in range(timeout):
            # CF challenge iframe 是最可靠的拦截信号
            if any("challenges.cloudflare.com" in (f.url or "")
                   for f in self.page.frames):
                # 有 CF iframe → 还在验证
                if i % 5 == 4:
                    print(f"  ⏳ CF 验证中 {i+1}/{timeout}s...")
                await asyncio.sleep(1)
                continue

            # 无 CF iframe → 检查页面是否有真实内容
            if await _page_has_real_content(self.page):
                if i > 0:
                    print(f"  ✓ CF 验证通过（{i+1}s）")
                return True

            # 检查 CF challenge form（无 iframe 时的备用）
            has_challenge = await self.page.evaluate("""() => {
                const sels = ['form#challenge-form','#cf-challenge-running',
                              '.cf-browser-verification','[id^=cf-chl-widget]'];
                return sels.some(s => document.querySelector(s));
            }""")
            if has_challenge:
                if i % 5 == 4:
                    print(f"  ⏳ CF form 等待 {i+1}/{timeout}s...")
                # 模拟鼠标
                try:
                    await self.page.mouse.move(
                        random.randint(300, 1600), random.randint(100, 700),
                        steps=random.randint(8, 20)
                    )
                except Exception:
                    pass
                await asyncio.sleep(1)
                continue

            # 无 CF 信号 → 通过（避免误报）
            if i > 0:
                print(f"  ✓ 页面就绪（{i+1}s）")
            return True

        return False


# ───────────────────────────────────────────────────────────
#  Layer 2 — M3u8Catcher（拦截 + 播放器 API + DOM 搜索）
# ───────────────────────────────────────────────────────────

class M3u8Catcher:
    """
    三路并发捕获 m3u8：
      1. context.route() 拦截所有 frame 的请求（最早，无需等响应）
      2. page.on("response") 检测响应 body（无扩展名 HLS）
      3. 主动查询 video.currentSrc / jwplayer / videojs API
    """

    _M3U8_RE = re.compile(r'https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*', re.IGNORECASE)

    def __init__(self):
        self._url:   Optional[str] = None
        self._event  = asyncio.Event()

    async def install(self, ctx: BrowserContext, page: Page):
        """安装 context 级路由拦截 + response 监听。"""
        await ctx.route("**/*", self._route_handler)
        page.on("response", self._response_handler)

    async def _route_handler(self, route: Route):
        url = route.request.url
        if self._is_m3u8(url) and not self._url:
            print(f"\n  ✓ [route] 捕获 m3u8: {url[:90]}")
            self._set(url)
        try:
            await route.continue_()
        except Exception:
            pass

    async def _response_handler(self, resp):
        if self._url:
            return
        url = resp.url
        if self._is_m3u8(url):
            self._set(url)
            return
        ct = ""
        try:
            ct = resp.headers.get("content-type", "")
        except Exception:
            return
        if "mpegurl" in ct.lower() or "x-mpegurl" in ct.lower():
            self._set(url)
            return
        # 检测无扩展名 HLS（body 以 #EXTM3U 开头）
        url_l = url.lower().split("?")[0]
        skip_ext = ('.js', '.css', '.html', '.json', '.png', '.jpg',
                    '.gif', '.svg', '.woff', '.woff2', '.ico')
        if any(url_l.endswith(e) for e in skip_ext):
            return
        try:
            body = await resp.text()
            if body.strip().startswith("#EXTM3U"):
                local = self._cache_local(url, body)
                print(f"\n  ✓ [response] 无扩展名 HLS，已缓存: {local}")
                self._set(local)
        except Exception:
            pass

    def _set(self, url: str):
        if not self._url:
            self._url = url
            self._event.set()

    async def wait(self, timeout: int = M3U8_WAIT_SEC) -> Optional[str]:
        try:
            await asyncio.wait_for(self._event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        return self._url

    async def query_player_api(self, page: Page) -> Optional[str]:
        """在所有非广告 frame 中查询 video.currentSrc / jwplayer / videojs。"""
        scripts = [
            "(() => { const v=document.querySelector('video'); return v?(v.currentSrc||v.src||null):null; })()",
            "(() => { try{return jwplayer().getPlaylistItem().file||null;}catch(e){return null;} })()",
            "(() => { try{return videojs(document.querySelector('.video-js')).currentSrc()||null;}catch(e){return null;} })()",
        ]
        for frame in page.frames:
            if any(kw in (frame.url or "") for kw in _AD_KW):
                continue
            for script in scripts:
                try:
                    r = await frame.evaluate(script)
                    if r and isinstance(r, str) and r.startswith("http"):
                        return r
                except Exception:
                    pass
        return None

    async def search_dom(self, page: Page) -> Optional[str]:
        """在所有 frame 的 DOM 中正则搜索 m3u8 URL。"""
        for frame in page.frames:
            if any(kw in (frame.url or "") for kw in _AD_KW):
                continue
            try:
                html = await frame.content()
                m = self._M3U8_RE.search(html)
                if m:
                    return m.group(0)
            except Exception:
                pass
        return None

    @staticmethod
    def _is_m3u8(url: str) -> bool:
        return ".m3u8" in url.lower()

    @staticmethod
    def _cache_local(url: str, body: str) -> str:
        base = url.rsplit("/", 1)[0] + "/"
        lines = [
            (urljoin(base, l) if l.strip() and not l.startswith("#")
             and not l.startswith("http") else l)
            for l in body.splitlines()
        ]
        p = DOWNLOAD_DIR / "playlist_cache.m3u8"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("\n".join(lines), encoding="utf-8")
        return str(p)


# ───────────────────────────────────────────────────────────
#  Layer 3 — VideoDownloader（ffmpeg + Python 备用）
# ───────────────────────────────────────────────────────────

class VideoDownloader:
    """
    优先使用 ffmpeg（支持 AES 加密、自动拼接），
    其次使用纯 Python 分片拼接（不支持 AES 加密流）。
    """

    @staticmethod
    def _find_ffmpeg() -> Optional[str]:
        if found := shutil.which("ffmpeg"):
            return found
        for p in (r"C:\ffmpeg\bin\ffmpeg.exe",
                  r"C:\Program Files\ffmpeg\bin\ffmpeg.exe"):
            if Path(p).exists():
                return p
        return None

    @classmethod
    async def download(cls, m3u8: str, out: Path,
                       seconds: int = 0) -> bool:
        out.parent.mkdir(parents=True, exist_ok=True)
        ffmpeg = cls._find_ffmpeg()
        if ffmpeg:
            return cls._ffmpeg(m3u8, out, ffmpeg, seconds)
        print("  ⚠ 未找到 ffmpeg，使用内置下载器（不支持 AES 加密流）")
        return await cls._python(m3u8, out, seconds)

    @staticmethod
    def _ffmpeg(m3u8: str, out: Path, ffbin: str, seconds: int) -> bool:
        is_local = not m3u8.startswith("http") and Path(m3u8).exists()
        cmd = [
            ffbin, "-y",
            "-allowed_extensions", "ALL",
            "-headers", f"Referer: {TARGET_URL}\r\nUser-Agent: {USER_AGENT}\r\n",
            "-i", m3u8,
        ]
        if seconds > 0:
            cmd += ["-t", str(seconds)]
        cmd += ["-c", "copy", "-bsf:a", "aac_adtstoasc", str(out)]
        print(f"  ffmpeg {'(前' + str(seconds) + '秒)' if seconds else '(完整)'}: {ffbin}")
        ret = subprocess.run(cmd, capture_output=True)
        if ret.returncode != 0:
            err = ret.stderr.decode(errors="replace")[-300:]
            print(f"  ✗ ffmpeg 退出码 {ret.returncode}:\n{err}")
        return ret.returncode == 0

    @staticmethod
    async def _python(m3u8: str, out: Path, seconds: int) -> bool:
        import urllib.parse
        headers = {"Referer": TARGET_URL, "User-Agent": USER_AGENT}
        is_local = not m3u8.startswith("http") and Path(m3u8).exists()
        base = (Path(m3u8).parent.as_uri() + "/") if is_local \
               else (m3u8.rsplit("/", 1)[0] + "/")

        async with httpx.AsyncClient(headers=headers, timeout=30,
                                     follow_redirects=True) as c:
            playlist = (Path(m3u8).read_text() if is_local
                        else (await c.get(m3u8)).text)
            if "#EXT-X-KEY" in playlist:
                print("  ✗ AES 加密流，Python 下载器不支持，请安装 ffmpeg")
                return False

            segs, elapsed = [], 0.0
            for line in playlist.splitlines():
                s = line.strip()
                if s.startswith("#EXTINF:"):
                    try:
                        elapsed += float(s[8:].split(",")[0])
                    except Exception:
                        pass
                elif s and not s.startswith("#"):
                    segs.append(s if s.startswith("http")
                                else urllib.parse.urljoin(base, s))
                    if seconds > 0 and elapsed >= seconds:
                        break

            if not segs:
                print("  ✗ 无分片")
                return False

            print(f"  下载 {len(segs)} 个分片...")
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
                    if i % 5 == 0 or i == len(segs):
                        print(f"  ▶ {i}/{len(segs)}", end="\r")

            print(f"\n  ✓ 保存: {ts_out}")
            print(f'  转换: ffmpeg -i "{ts_out}" -c copy "{out}"')
            return True


# ───────────────────────────────────────────────────────────
#  Frame 树可视化（调试用）
# ───────────────────────────────────────────────────────────

def dump_frames(page: Page):
    def _dump(frame: Frame, depth: int = 0):
        url  = frame.url or "about:blank"
        role = "🎬" if any(kw in url for kw in _PLAYER_KW) else \
               "🔒" if "challenges.cloudflare.com" in url else \
               "🏠" if DOMAIN in url else "  "
        print(f"{'  ' * depth}{role} {url[:100]}")
        for ch in frame.child_frames:
            _dump(ch, depth + 1)

    print("  ── Frame 树 ─────────────────────────────────")
    _dump(page.main_frame)
    print("  ─────────────────────────────────────────────")


# ───────────────────────────────────────────────────────────
#  辅助函数
# ───────────────────────────────────────────────────────────

def _find_chromium() -> Optional[str]:
    if CHROMIUM_BIN and Path(CHROMIUM_BIN).exists():
        return CHROMIUM_BIN
    for name in ("chromium", "chromium-browser", "google-chrome",
                 "google-chrome-stable"):
        if f := shutil.which(name):
            return f
    for p in ("/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
              "/usr/bin/chromium", "/usr/bin/google-chrome",
              r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"):
        if Path(p).exists():
            return p
    return None


async def _apply_stealth(page: Page):
    """
    应用 playwright-stealth，兼容 v1/v2 两种 API：
      v2: Stealth().apply_stealth_async(page) — 直接 awaitable
      v2: Stealth().use_async(page)           — async context manager（不能 await）
      v1: stealth_async(page)                  — 直接 awaitable
    """
    try:
        from playwright_stealth import Stealth
        s = Stealth()
        # v2: apply_stealth_async 是普通协程
        if callable(getattr(s, "apply_stealth_async", None)):
            await s.apply_stealth_async(page)
            print("  ✓ stealth v2 已应用")
            return
        # v2: use_async 是 async context manager
        if callable(getattr(s, "use_async", None)):
            async with s.use_async(page):
                pass  # 进入即完成注入，退出时无副作用
            print("  ✓ stealth v2 (use_async) 已应用")
            return
    except ImportError:
        pass
    except Exception as e:
        print(f"  ⚠ stealth v2: {e}")

    try:
        from playwright_stealth import stealth_async
        await stealth_async(page)
        print("  ✓ stealth v1 已应用")
    except ImportError:
        print("  ⚠ playwright-stealth 未安装，跳过")


async def _page_has_real_content(page: Page) -> bool:
    """检测页面是否有真实内容（用于 CF bypass 判定）。"""
    try:
        return await page.evaluate("""() => {
            const sels = ['nav','footer','video','article','.entry-content',
                          '#content','main','.post-content','#player'];
            return sels.some(s => document.querySelector(s)) ||
                   document.querySelectorAll('a[href]').length > 8;
        }""")
    except Exception:
        return False


# ───────────────────────────────────────────────────────────
#  主流程
# ───────────────────────────────────────────────────────────

async def main():
    print("supjav 视频下载器 v3 — 自动 CF 绕过")
    print("=" * 60)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    # ── Step 1: CF 自动绕过，获取 cookies ──────────────────────────────
    print("\n[1/3] 自动绕过 Cloudflare...")
    cookies = await CfBuster.get_cookies(TARGET_URL, HEADLESS)
    cf_val = next((c["value"][:30] for c in cookies
                   if c.get("name") == "cf_clearance"), None)
    print(f"  cf_clearance: {cf_val}..." if cf_val else
          f"  ℹ 无 cf_clearance（共 {len(cookies)} 个 cookie）")

    # ── Step 2: 浏览器加载页面，拦截 m3u8 ──────────────────────────────
    print(f"\n[2/3] 加载页面，捕获视频流...")

    m3u8: Optional[str] = None
    exe = _find_chromium()
    print(f"  Chromium: {exe or '(playwright 默认)'}")

    async with async_playwright() as pw:
        launch_args = [
            "--no-sandbox", "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            "--disable-web-security",
            "--disable-features=IsolateOrigins,site-per-process",
            "--window-size=1920,1080",
        ]
        if HEADLESS:
            launch_args.append("--headless=new")

        browser = await pw.chromium.launch(
            headless=HEADLESS,
            executable_path=exe,
            args=launch_args,
        )
        ctx = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1920, "height": 1080},
            ignore_https_errors=True,
            locale="zh-CN",
        )

        # 注入 cookies
        if cookies:
            await ctx.add_cookies(cookies)

        page = await ctx.new_page()
        await _apply_stealth(page)

        # 安装 m3u8 拦截器
        catcher = M3u8Catcher()
        await catcher.install(ctx, page)

        # 加载页面
        nav = PageNavigator(ctx, page)
        if not await nav.load():
            await browser.close()
            sys.exit(1)

        # 点击 DS 服务器按钮（在 route 已就绪的情况下）
        if VIDEO_SERVER:
            await nav.click_server(VIDEO_SERVER)

        # 打印 frame 树
        dump_frames(page)

        # 等待 m3u8 （DS 播放器 iframe 完整加载 + VideoJS 初始化）
        print(f"  ⏳ 等待 m3u8 URL（最多 {M3U8_WAIT_SEC}s）...")
        m3u8 = await catcher.wait(M3U8_WAIT_SEC)

        if not m3u8:
            print("  ▶ 未从 route 捕获，尝试触发播放...")
            await _trigger_play(page)
            await asyncio.sleep(3)
            m3u8 = await catcher.wait(10)

        if not m3u8:
            print("  ▶ 查询播放器 API (video.currentSrc)...")
            m3u8 = await catcher.query_player_api(page)

        if not m3u8:
            print("  ▶ DOM 正则搜索 m3u8...")
            m3u8 = await catcher.search_dom(page)

        if not m3u8:
            shot = DOWNLOAD_DIR / "debug_final.png"
            await page.screenshot(path=str(shot), full_page=True)
            print(f"  截图: {shot}")

        await browser.close()

    if not m3u8:
        print("\n  ✗ 未找到视频流")
        print("  诊断步骤：")
        print("    1. 设置 HEADLESS=False 再运行，观察浏览器行为")
        print("    2. 查看 downloads/ 目录中的截图")
        print("    3. 确认 DS 服务器按钮文字与 VIDEO_SERVER 设置一致")
        sys.exit(1)

    print(f"\n  ✓ 视频流: {m3u8[:80]}")

    # ── Step 3: 下载前 5 秒 ────────────────────────────────────────────
    print(f"\n[3/3] 下载前 {DOWNLOAD_SECONDS} 秒视频...")
    print(f"  输出: {OUTPUT_FILE}")
    ok = await VideoDownloader.download(m3u8, OUTPUT_FILE, DOWNLOAD_SECONDS)

    if ok:
        size = OUTPUT_FILE.stat().st_size if OUTPUT_FILE.exists() else 0
        print(f"\n  ✓ 完成: {OUTPUT_FILE} ({size/1024:.1f} KB)")
        print(f"  播放: ffplay \"{OUTPUT_FILE}\"")
    else:
        print(f"\n  ✗ 下载失败，手动命令:")
        t_arg = f"-t {DOWNLOAD_SECONDS} " if DOWNLOAD_SECONDS else ""
        print(f'  ffmpeg -allowed_extensions ALL {t_arg}-i "{m3u8}" '
              f'-c copy downloads/video_5s.mp4')
        sys.exit(1)


async def _trigger_play(page: Page):
    """在主页 + 所有 iframe 中触发视频播放。"""
    sels = ["video", ".vjs-big-play-button", ".jw-display-icon-container",
            ".play-btn", "[class*='play']", "[aria-label*='play' i]"]
    for frame in page.frames:
        for sel in sels:
            try:
                el = frame.locator(sel).first
                if await el.count() > 0:
                    await el.click(timeout=2000)
                    print(f"  ✓ 点击播放: {sel}")
                    return
            except Exception:
                pass

    for frame in page.frames:
        try:
            n = await frame.evaluate(
                "() => { const vs=document.querySelectorAll('video'); "
                "vs.forEach(v=>{try{v.play()}catch(e){}}); return vs.length; }"
            )
            if n:
                print(f"  ✓ video.play() × {n}")
                return
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
