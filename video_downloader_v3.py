#!/usr/bin/env python3
"""
supjav.com 视频下载器 v3 — 单会话自动 CF 绕过，无需手动 Cookie

【设计原则】CF 绕过与页面操作必须在同一浏览器会话内完成。
原因：cf_clearance 绑定 (IP + TLS 指纹 + 会话)，跨会话注入会触发
Cloudflare Turnstile 重新验证。

工作流程（单会话）：
  ┌── 浏览器启动（stealth 修补 navigator.webdriver 等）
  │
  ├── [可选] curl-cffi 预热：纯 HTTP TLS 伪装获取 cf_clearance
  │     成功 → 注入 cookie 到浏览器（可跳过 CF 等待）
  │     失败 → 继续（浏览器自行处理 CF）
  │
  ├── goto(supjav.com/132824.html)
  │
  ├── wait_for_cf_pass()  —— 精确检测，避免 CF 过渡页误判
  │     · CF challenge iframe  → 等待
  │     · CF 特有标题/class    → 等待
  │     · 真实内容（server按钮/article/video）→ 通过
  │     · headless 超时 → 重启为显示模式让用户手动过
  │
  ├── click_server(DS)   ← 与 CF 绕过同一 session
  │
  ├── context.route() 拦截 m3u8
  │
  └── ffmpeg 下载前 N 秒

依赖：
  pip install playwright playwright-stealth curl-cffi httpx
  playwright install chromium   # 或设置 CHROMIUM_BIN
  # Windows: winget install ffmpeg
  # Linux:   apt install ffmpeg
"""

import asyncio
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, urljoin

import httpx
from playwright.async_api import (
    async_playwright, BrowserContext, Page, Frame, Route
)

# ═══════════════════════════════════════════════════════════
#  用户配置
# ═══════════════════════════════════════════════════════════

TARGET_URL      = "https://supjav.com/132824.html"
VIDEO_SERVER    = "DS"              # DS / TV / JPA / ST
DOWNLOAD_DIR    = Path("downloads")
OUTPUT_FILE     = DOWNLOAD_DIR / "video_5s.mp4"
DOWNLOAD_SECONDS = 5                # 0 = 下载全部

# headless=True 时 CF 自动等待上限（秒）
# headless=False 时固定 300 秒（供用户手动操作）
CF_AUTO_TIMEOUT  = 30
CF_MANUAL_TIMEOUT = 300

# m3u8 等待上限（DS iframe 初始化可能需要 30-40 秒）
M3U8_WAIT_SEC = 60

# Chromium 路径（留空 = 自动查找）
CHROMIUM_BIN = ""

# CDP 连接端口（策略 A0：用户手动启动 Chrome 后脚本连接）
CDP_PORT = 9222

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# ═══════════════════════════════════════════════════════════

DOMAIN     = urlparse(TARGET_URL).netloc
_PLAYER_KW = ("/e/", "/embed", "/player/", "/hls/", "playmogo.", "lk1.", "supremejav.")
_AD_KW     = ("googlesyndication.", "doubleclick.", "adnxs.", "analytics.",
               "tracking.", "tracker.", "advertising.")

# CF 挑战页独有的精确标题（小写）
_CF_TITLES = frozenset({
    "just a moment...", "checking your browser...",
    "attention required! | cloudflare", "请稍候…", "请稍候",
    "access denied", "one more step",
})

# CF 挑战页独有的 DOM 选择器
_CF_DOM_SELS = (
    "form#challenge-form",
    "#cf-challenge-running",
    ".cf-browser-verification",
    "#challenge-error-title",
    "[id^='cf-chl-widget']",
    "#cf-wrapper",           # Turnstile 外层包裹
    "#cf-error-details",
)

# 真实页面才有的 DOM 选择器（CF 挑战页绝对没有）
_REAL_CONTENT_SELS = (
    "video",
    "article",
    ".entry-content",
    "#content",
    ".post-content",
    "#player",
    "[class*='server']",   # supjav 服务器按钮容器
    "[class*='source']",
    "[class*='sorc']",
    ".post-title",
    ".entry-title",
    "h1.title",
)


# ───────────────────────────────────────────────────────────
#  CF 状态检测（精确，无误判）
# ───────────────────────────────────────────────────────────

async def cf_is_blocked(page: Page) -> bool:
    """
    精确判断页面是否被 CF 拦截。
    检测顺序（优先快速）：
      1. challenges.cloudflare.com iframe → 拦截
      2. 页面标题精确匹配 CF 标题 → 拦截
      3. CF 特有 DOM 元素 → 拦截
      4. 页面有真实内容（server按钮/video/article）→ 未拦截
      5. 链接数 > 8 → 未拦截（CF页通常只有2个链接）
      6. 默认 → 未拦截（宁漏报勿误报）
    """
    # 1. CF challenge iframe（最强信号）
    if any("challenges.cloudflare.com" in (f.url or "")
           for f in page.frames):
        return True

    try:
        result = await page.evaluate(f"""() => {{
            // 2. 标题精确匹配
            const t = document.title.trim().toLowerCase();
            const cfTitles = {list(_CF_TITLES)};
            if (cfTitles.includes(t)) return 'title';

            // 3. CF 特有 DOM
            const cfSels = {list(_CF_DOM_SELS)};
            if (cfSels.some(s => document.querySelector(s))) return 'dom';

            // 4. 真实内容（CF 页绝对没有这些）
            const realSels = {list(_REAL_CONTENT_SELS)};
            if (realSels.some(s => document.querySelector(s))) return 'real';

            // 5. 链接数（CF 页 ≤ 3 个链接）
            const links = document.querySelectorAll('a[href]').length;
            if (links > 8) return 'links';

            // 6. 默认未拦截
            return 'unknown';
        }}""")
        return result in ("title", "dom")
    except Exception:
        return False


async def wait_for_cf_pass(page: Page, timeout: int, label: str = "页面") -> bool:
    """
    等待 CF 验证通过，且真实页面内容已加载。
    两个条件都满足才返回 True：
      1. cf_is_blocked() → False（CF 拦截消失）
      2. 页面有真实内容（服务器按钮 / article / .entry-content 等）
    这样可以避免在 "验证成功。正在等待..." 过渡态误判通过。
    """
    import random

    await asyncio.sleep(1.5)

    for i in range(timeout):
        blocked = await cf_is_blocked(page)
        if not blocked:
            # CF 消失后，还需确认真实内容已渲染（过渡页没有这些元素）
            try:
                has_content = await page.evaluate("""() => {
                    const sels = [
                        "[class*='server']", "button.srv-btn", ".server-bar",
                        "article", ".entry-content", "#content", ".post-content",
                        "#player", "h1.title", ".post-title", ".entry-title",
                        ".wp-content", ".video-wrap", "#player-container"
                    ];
                    return sels.some(s => document.querySelector(s));
                }""")
            except Exception:
                has_content = False

            if has_content:
                if i > 0:
                    print(f"  ✓ {label} CF 验证通过（{i + 2}s）")
                return True
            # CF 已消失但页面还在过渡态，继续等待

        # 模拟人类鼠标（有助于行为检测）
        if i % 5 == 4:
            try:
                await page.mouse.move(
                    random.randint(300, 1600), random.randint(100, 700),
                    steps=random.randint(8, 20),
                )
            except Exception:
                pass

        await asyncio.sleep(1)
        if (i + 1) % 10 == 0:
            print(f"  ⏳ {label} CF 等待 {i + 2}s/{timeout}s...")

    print(f"  ✗ {label} CF {timeout}s 内未通过")
    return False


# ───────────────────────────────────────────────────────────
#  curl-cffi 预热（可选，不影响主流程）
# ───────────────────────────────────────────────────────────

async def curl_cffi_prefetch(url: str) -> list[dict]:
    """
    用 curl-cffi 的 Chrome TLS 指纹尝试绕过 CF JS Challenge。
    成功时返回 cookies 注入浏览器（可缩短 CF 等待时间）。
    失败（Turnstile / 未安装）时返回空列表，浏览器自行处理。
    """
    try:
        from curl_cffi.requests import AsyncSession
    except ImportError:
        print("  ⚠ curl-cffi 未安装（pip install curl-cffi），跳过预热")
        return []

    try:
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
        }
        async with AsyncSession(impersonate="chrome124") as sess:
            resp = await sess.get(url, headers=headers, timeout=15, allow_redirects=True)

        if resp.status_code not in (200, 301, 302):
            print(f"  ⚠ curl-cffi: HTTP {resp.status_code}，跳过预热")
            return []

        raw = dict(resp.cookies)
        if not raw:
            return []

        cookies = [{
            "name": k, "value": v,
            "domain": DOMAIN, "path": "/",
            "httpOnly": False, "secure": True, "sameSite": "None",
        } for k, v in raw.items()]

        cf_val = raw.get("cf_clearance", "")
        if cf_val:
            print(f"  ✓ curl-cffi 获得 cf_clearance: {cf_val[:30]}...")
        else:
            print(f"  ℹ curl-cffi 获得 {len(cookies)} 个 cookie（无 cf_clearance）")
        return cookies

    except Exception as e:
        print(f"  ⚠ curl-cffi 异常: {e}")
        return []


# ───────────────────────────────────────────────────────────
#  DS 服务器按钮点击
# ───────────────────────────────────────────────────────────

async def click_server(page: Page, server: str) -> bool:
    """点击服务器选择按钮（多策略 + JS 兜底）。"""
    if not server:
        return False

    print(f"  ▶ 切换到 {server} 服务器...")

    # 等待服务器按钮出现（真实内容优先，超时后再尝试）
    # 不用 networkidle：networkidle 可能在 CF 过渡页就触发，此时按钮不存在
    found_content = False
    for sel in (
        f":text('{server}')",
        "button.srv-btn", "[class*='server']",
        "[class*='source']", "[class*='sorc']", "li", "button",
    ):
        try:
            await page.wait_for_selector(sel, timeout=3000)
            found_content = True
            break
        except Exception:
            continue

    if not found_content:
        # 最后兜底：等 networkidle（可能在过渡页）
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass

    # 小幅滚动（触发懒加载，不超过 300px 避免滚过按钮行）
    try:
        await page.evaluate(
            "window.scrollTo(0, Math.min(300, document.body.scrollHeight * 0.1))"
        )
        await asyncio.sleep(0.3)
    except Exception:
        pass

    # 多选择器尝试
    for sel in (
        f":text-is('{server}')",
        f":text('{server}')",
        f"li:has-text('{server}')",
        f"button:has-text('{server}')",
        f"a:has-text('{server}')",
        f"span:has-text('{server}')",
        f"div:has-text('{server}')",
        f"[class*='{server.lower()}']",
        f"[class*='server']:has-text('{server}')",
        f"[class*='source']:has-text('{server}')",
        f"[class*='sorc']:has-text('{server}')",
    ):
        try:
            el = page.locator(sel).first
            if await el.count() > 0:
                try:
                    await el.scroll_into_view_if_needed()
                except Exception:
                    pass
                await el.click(timeout=3000)
                print(f"  ✓ 点击 {server}（{sel}）")
                await asyncio.sleep(2)
                return True
        except Exception:
            continue

    # JS 兜底：遍历所有叶节点精确匹配
    try:
        hit = await page.evaluate(f"""(s) => {{
            for (const el of document.querySelectorAll('*')) {{
                if (el.childElementCount === 0 && el.textContent.trim() === s) {{
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

    # 诊断：打印当前叶节点
    try:
        leaves = await page.evaluate("""() =>
            [...document.querySelectorAll('*')]
            .filter(e => e.childElementCount === 0 && e.textContent.trim())
            .map(e => {
                const c = [...e.classList].join('.');
                return e.tagName.toLowerCase() + (c ? '.' + c : '') +
                       ': "' + e.textContent.trim().slice(0, 50) + '"';
            }).slice(0, 30)
        """)
        print(f"  ⚠ 未找到 {server} 按钮，当前叶节点（前30条）:")
        for l in (leaves or []):
            print(f"    {l}")
    except Exception:
        pass

    try:
        shot = DOWNLOAD_DIR / "debug_server.png"
        await page.screenshot(path=str(shot), full_page=True)
        print(f"  截图: {shot}")
    except Exception:
        pass

    print(f"  ⚠ {server} 按钮未找到，继续等待 m3u8...")
    return False


# ───────────────────────────────────────────────────────────
#  M3u8Catcher
# ───────────────────────────────────────────────────────────

class M3u8Catcher:
    """context.route() 拦截 + response body 检测 + player API + DOM 搜索。"""

    _M3U8_RE = re.compile(r'https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*', re.IGNORECASE)

    def __init__(self):
        self._url: Optional[str] = None
        self._event = asyncio.Event()

    async def install(self, ctx: BrowserContext, page: Page):
        await ctx.route("**/*", self._route_handler)
        page.on("response", self._response_handler)

    async def install_passive(self, ctx: BrowserContext, page: Page):
        """被动观察模式 — 不拦截请求（不触发 Fetch.enable），适用于 CDP connect 场景。
        使用 ctx.on("response") 捕获全上下文响应 + CDP Network 事件覆盖跨域 iframe。"""
        ctx.on("response", self._response_handler)
        try:
            cdp = await ctx.new_cdp_session(page)
            await cdp.send("Network.enable", {
                "maxTotalBufferSize": 0,
                "maxResourceBufferSize": 0,
                "maxPostDataSize": 0,
            })

            def _on_cdp_response(event):
                url = event.get("response", {}).get("url", "")
                if ".m3u8" in url.lower():
                    print(f"\n  ✓ [CDP] 捕获 m3u8: {url[:90]}")
                    self._set(url)

            cdp.on("Network.responseReceived", _on_cdp_response)
            print("  ✓ CDP Network 监听已启动（被动模式，不干扰请求）")
        except Exception as e:
            print(f"  ⚠ CDP Network 监听启动失败，降级为 response 监听: {e}")

    async def _route_handler(self, route: Route):
        url = route.request.url
        if ".m3u8" in url.lower() and not self._url:
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
        if ".m3u8" in url.lower():
            self._set(url)
            return
        try:
            ct = resp.headers.get("content-type", "").lower()
        except Exception:
            return
        if "mpegurl" in ct or "x-mpegurl" in ct:
            self._set(url)
            return
        # 无扩展名 HLS：检测响应 body
        url_l = url.lower().split("?")[0]
        skip = ('.js', '.css', '.html', '.json', '.png', '.jpg',
                '.gif', '.svg', '.woff', '.woff2', '.ico')
        if any(url_l.endswith(e) for e in skip):
            return
        try:
            body = await resp.text()
            if body.strip().startswith("#EXTM3U"):
                local = self._cache(url, body)
                print(f"\n  ✓ [response] 无扩展名 HLS 已缓存: {local}")
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
    def _cache(url: str, body: str) -> str:
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
#  VideoDownloader
# ───────────────────────────────────────────────────────────

class VideoDownloader:

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
    async def download(cls, m3u8: str, out: Path, seconds: int = 0) -> bool:
        out.parent.mkdir(parents=True, exist_ok=True)
        ff = cls._find_ffmpeg()
        if ff:
            return cls._ffmpeg(m3u8, out, ff, seconds)
        print("  ⚠ 未找到 ffmpeg，使用内置下载器（不支持 AES 加密流）")
        return await cls._python(m3u8, out, seconds)

    @staticmethod
    def _ffmpeg(m3u8: str, out: Path, ffbin: str, seconds: int) -> bool:
        cmd = [
            ffbin, "-y",
            "-allowed_extensions", "ALL",
            "-headers", f"Referer: {TARGET_URL}\r\nUser-Agent: {USER_AGENT}\r\n",
            "-i", m3u8,
        ]
        if seconds > 0:
            cmd += ["-t", str(seconds)]
        cmd += ["-c", "copy", "-bsf:a", "aac_adtstoasc", str(out)]
        lbl = f"前{seconds}秒" if seconds else "完整"
        print(f"  ffmpeg ({lbl}): {ffbin}")
        ret = subprocess.run(cmd, capture_output=True)
        if ret.returncode != 0:
            err = ret.stderr.decode(errors="replace")[-400:]
            print(f"  ✗ ffmpeg 失败 (exit {ret.returncode}):\n{err}")
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
                print("  ✗ AES 加密流，请安装 ffmpeg")
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

            ts_out = out.with_suffix(".ts")
            print(f"  下载 {len(segs)} 个分片...")
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
#  辅助：浏览器启动 / stealth / frame 树
# ───────────────────────────────────────────────────────────

def _find_chromium() -> Optional[str]:
    if CHROMIUM_BIN and Path(CHROMIUM_BIN).exists():
        return CHROMIUM_BIN
    for name in ("chromium", "chromium-browser", "google-chrome",
                 "google-chrome-stable"):
        if f := shutil.which(name):
            return f
    for p in (
        "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
        "/usr/bin/chromium", "/usr/bin/google-chrome",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Users\{}\AppData\Local\Google\Chrome\Application\chrome.exe".format(
            __import__("os").environ.get("USERNAME", "User")),
    ):
        if Path(p).exists():
            return p
    return None


def _find_chrome_binary() -> Optional[str]:
    """查找系统已安装的 Chrome/Chromium 可执行文件。"""
    import os
    candidates = []
    if sys.platform == "win32":
        for base in (os.environ.get("PROGRAMFILES", r"C:\Program Files"),
                     os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
                     os.environ.get("LOCALAPPDATA", "")):
            if base:
                candidates.append(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe")
    elif sys.platform == "darwin":
        candidates.append(Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"))
        candidates.append(Path("/Applications/Chromium.app/Contents/MacOS/Chromium"))
    else:
        for name in ("google-chrome", "google-chrome-stable", "chromium-browser", "chromium"):
            found = shutil.which(name)
            if found:
                return found
        candidates = [
            Path("/usr/bin/google-chrome"),
            Path("/usr/bin/chromium-browser"),
            Path("/usr/bin/chromium"),
        ]
    for p in candidates:
        if p.exists():
            return str(p)
    # 最后尝试 PATH
    for name in ("chrome", "google-chrome", "chromium"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _find_chrome_user_data() -> Optional[str]:
    """找到用户真实 Chrome 配置目录（Windows/Mac/Linux）。"""
    import os
    candidates = []
    # Windows
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        candidates.append(Path(local) / "Google" / "Chrome" / "User Data")
    appdata = os.environ.get("APPDATA", "")
    if appdata:
        candidates.append(Path(appdata) / ".." / "Local" / "Google" / "Chrome" / "User Data")
    # Mac
    candidates.append(Path.home() / "Library" / "Application Support" / "Google" / "Chrome")
    # Linux
    candidates.append(Path.home() / ".config" / "google-chrome")
    candidates.append(Path.home() / ".config" / "chromium")

    for p in candidates:
        try:
            resolved = p.resolve()
            if resolved.exists() and (resolved / "Default").exists():
                return str(resolved)
        except Exception:
            pass
    return None


async def _apply_stealth(page: Page):
    """应用 playwright-stealth（兼容 v1/v2）。"""
    try:
        from playwright_stealth import Stealth
        s = Stealth()
        if callable(getattr(s, "apply_stealth_async", None)):
            await s.apply_stealth_async(page)
            print("  ✓ stealth v2 已应用")
            return
        if callable(getattr(s, "use_async", None)):
            async with s.use_async(page):
                pass
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
        print("  ⚠ playwright-stealth 未安装")


_xvfb_proc = None  # global: keep Xvfb alive for the process lifetime

def _ensure_display():
    """如果没有 $DISPLAY 且有 Xvfb，自动启动虚拟显示器。"""
    import os
    global _xvfb_proc
    if os.environ.get("DISPLAY"):
        return
    xvfb = shutil.which("Xvfb")
    if not xvfb:
        return
    import subprocess
    try:
        _xvfb_proc = subprocess.Popen(
            [xvfb, ":99", "-screen", "0", "1920x1080x24"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        os.environ["DISPLAY"] = ":99"
        import time; time.sleep(0.8)
        print("  ✓ 虚拟显示器 Xvfb :99 已启动")
    except Exception as e:
        print(f"  ⚠ Xvfb 启动失败: {e}")


async def _launch_browser(pw, headless: bool):
    if not headless:
        _ensure_display()
    exe = _find_chromium()
    print(f"  Chromium: {exe or '(playwright 默认)'}")
    args = [
        "--no-sandbox", "--disable-dev-shm-usage",
        "--disable-blink-features=AutomationControlled",
        "--disable-web-security",
        "--disable-features=IsolateOrigins,site-per-process",
        "--window-size=1920,1080",
    ]
    if headless:
        args.append("--headless=new")
    kw = dict(headless=headless, args=args)
    if exe:
        kw["executable_path"] = exe
    return await pw.chromium.launch(**kw)


def dump_frames(page: Page):
    def _dump(frame: Frame, depth: int = 0):
        url = frame.url or "about:blank"
        icon = ("🎬" if any(kw in url for kw in _PLAYER_KW)
                else "🔒" if "challenges.cloudflare.com" in url
                else "🏠" if DOMAIN in url else "  ")
        print(f"{'  ' * depth}{icon} {url[:100]}")
        for ch in frame.child_frames:
            _dump(ch, depth + 1)
    print("  ── Frame 树 ─────────────────────────────────")
    _dump(page.main_frame)
    print("  ─────────────────────────────────────────────")


async def _trigger_play(page: Page):
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
                "() => { const vs=document.querySelectorAll('video');"
                "vs.forEach(v=>{try{v.play()}catch(e){}});return vs.length; }"
            )
            if n:
                print(f"  ✓ video.play() × {n}")
                return
        except Exception:
            pass


# ───────────────────────────────────────────────────────────
#  单会话主流程
# ───────────────────────────────────────────────────────────

async def run_single_session(pw, headless: bool,
                              prefetch_cookies: list[dict]) -> Optional[str]:
    """
    单会话：启动浏览器 → CF 通过 → DS 点击 → m3u8 捕获。
    CF 绕过与页面操作在同一 session 内完成，避免跨会话 cookie 失效。
    """
    browser = await _launch_browser(pw, headless)
    ctx = await browser.new_context(
        user_agent=USER_AGENT,
        viewport={"width": 1920, "height": 1080},
        ignore_https_errors=True,
        locale="zh-CN",
    )

    # 注入 curl-cffi 预热 cookie（可能缩短 CF 等待，也可能没用）
    if prefetch_cookies:
        await ctx.add_cookies(prefetch_cookies)

    page = await ctx.new_page()
    await _apply_stealth(page)

    # 安装 m3u8 拦截器（必须在 goto 之前）
    catcher = M3u8Catcher()
    await catcher.install(ctx, page)

    # 导航
    print(f"  ▶ 加载: {TARGET_URL}")
    try:
        await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        print(f"  ⚠ 加载超时（继续）: {e}")

    # 等待 CF 通过
    cf_timeout = CF_MANUAL_TIMEOUT if not headless else CF_AUTO_TIMEOUT
    if not headless:
        print(f"  ℹ 显示模式：请在浏览器中手动通过 CF 验证（最多 {cf_timeout}s）")

    cf_ok = await wait_for_cf_pass(page, cf_timeout, "主站")
    if not cf_ok:
        dump_frames(page)
        try:
            shot = DOWNLOAD_DIR / "debug_cf.png"
            await page.screenshot(path=str(shot), full_page=True)
            print(f"  截图: {shot}")
        except Exception:
            pass
        await browser.close()
        return None

    # 快速检查 route 是否已有 m3u8（某些 DS 是默认服务器，页面加载即触发）
    m3u8 = await catcher.wait(timeout=2)
    if m3u8:
        await browser.close()
        return m3u8

    # 点击 DS 服务器（同一 session，CF 已过）
    if VIDEO_SERVER:
        await click_server(page, VIDEO_SERVER)

    # 打印 frame 树
    dump_frames(page)

    # 等待 DS 播放器 iframe 的 CF 验证通过（lk1.supremejav.com 等也有 CF）
    # 从 frame 树检测是否有 challenges.cloudflare.com 子 iframe
    player_cf_wait = 60 if not headless else 20
    for _t in range(player_cf_wait):
        cf_in_frames = any(
            "challenges.cloudflare.com" in (f.url or "")
            for f in page.frames
            if f != page.main_frame
        )
        if not cf_in_frames:
            break
        if _t == 0:
            print(f"  ⏳ 播放器 iframe CF 验证中（最多 {player_cf_wait}s）...")
        if not headless and _t % 10 == 9:
            print(f"  ⏳ 播放器 iframe CF 等待 {_t + 1}s/{player_cf_wait}s...")
        await asyncio.sleep(1)

    # 等待 m3u8（最多 M3U8_WAIT_SEC 秒）
    print(f"\n  ⏳ 等待 m3u8（最多 {M3U8_WAIT_SEC}s）...")
    m3u8 = await catcher.wait(M3U8_WAIT_SEC)

    if not m3u8:
        print("  ▶ 触发视频播放...")
        await _trigger_play(page)
        await asyncio.sleep(3)
        m3u8 = await catcher.wait(10)

    if not m3u8:
        print("  ▶ 查询播放器 API...")
        m3u8 = await catcher.query_player_api(page)

    if not m3u8:
        print("  ▶ DOM 正则搜索...")
        m3u8 = await catcher.search_dom(page)

    if not m3u8:
        try:
            shot = DOWNLOAD_DIR / "debug_final.png"
            await page.screenshot(path=str(shot), full_page=True)
            print(f"  截图: {shot}")
        except Exception:
            pass

    await browser.close()
    return m3u8


# ───────────────────────────────────────────────────────────
#  策略 A0：connect_over_cdp（用户手动启动 Chrome + 过 CF）
# ───────────────────────────────────────────────────────────
#
#  【为什么需要这个策略】
#  Cloudflare Turnstile 会检测 Chrome 是否由 CDP（远程调试协议）自动化
#  启动。即使在有头模式下，Playwright 控制的 Chrome 在启动时就留下了
#  CDP 自动化指纹，导致 Turnstile 拒绝复选框点击。
#
#  【解决方案】
#  脚本自动用 subprocess.Popen 启动 Chrome（带调试端口），Chrome 以普通
#  用户模式启动，无任何 CDP 自动化指纹。脚本随后等待用户在弹出的 Chrome
#  窗口中手动完成 Turnstile 验证，然后自动接管会话、捕获 m3u8。
#
#  【工作流程（全自动启动，仅需手动点一次复选框）】
#  1. 脚本自动查找并启动 Chrome（带 --remote-debugging-port=9222）
#  2. Chrome 弹出窗口，脚本自动导航到目标页面
#  3. 若遇到 Cloudflare 验证：在 Chrome 窗口中点击「请验证您是真人」复选框
#  4. 验证通过后脚本自动继续，无需其他操作
#
#  【也支持预先手动启动 Chrome（端口已开放时跳过自动启动）】
#  Windows:
#    "C:\Program Files\Google\Chrome\Application\chrome.exe" ^
#      --remote-debugging-port=9222 --user-data-dir=%TEMP%\chrome_cdp
#  Mac/Linux:
#    google-chrome --remote-debugging-port=9222 --user-data-dir=/tmp/chrome_cdp

async def run_cdp_connect_session() -> Optional[str]:
    """
    自动启动 Chrome（调试模式）或连接用户已启动的 Chrome，完成 CF 验证后捕获 m3u8。

    关键设计：
    - Chrome 由 subprocess 启动（非 Playwright launch），无 CDP 自动化指纹
    - connect_over_cdp 接入后使用被动 Network 监听，不触发 Fetch.enable
    - 避免路由拦截干扰 lk1 播放器 iframe 的加载
    """
    import socket
    import subprocess
    import tempfile

    cdp_url = f"http://localhost:{CDP_PORT}"
    chrome_proc = None
    tmp_dir = None

    def _port_open() -> bool:
        try:
            s = socket.socket()
            s.settimeout(0.5)
            s.connect(("localhost", CDP_PORT))
            s.close()
            return True
        except Exception:
            return False

    if _port_open():
        print(f"  ✓ 检测到 Chrome 已在调试模式运行（端口 {CDP_PORT}）")
    else:
        chrome_bin = _find_chrome_binary()
        if not chrome_bin:
            print(f"  ⚠ 未找到 Chrome，跳过 CDP 策略（安装 Chrome 后重试）")
            return None

        tmp_dir = tempfile.mkdtemp(prefix="chrome_cdp_")
        cmd = [
            chrome_bin,
            f"--remote-debugging-port={CDP_PORT}",
            f"--user-data-dir={tmp_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
        ]
        print(f"  ▶ 自动启动 Chrome: {Path(chrome_bin).name}")
        try:
            chrome_proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        except Exception as e:
            print(f"  ✗ Chrome 启动失败: {e}")
            return None

        # 等待 Chrome 就绪（最多 10s）
        for _ in range(20):
            if _port_open():
                break
            await asyncio.sleep(0.5)
        else:
            print("  ✗ Chrome 启动超时（10s）")
            chrome_proc.terminate()
            return None
        print(f"  ✓ Chrome 已启动，调试端口 {CDP_PORT}")

    try:
        from playwright.async_api import async_playwright as _ap
        async with _ap() as pw:
            try:
                browser = await pw.chromium.connect_over_cdp(cdp_url, timeout=8000)
            except Exception as e:
                print(f"  ✗ CDP 连接失败: {e}")
                return None

            ctx = browser.contexts[0] if browser.contexts else None
            if ctx is None:
                print("  ⚠ Chrome 没有已打开的上下文")
                return None

            # 找到或创建目标页面
            target_page = None
            for p in ctx.pages:
                if DOMAIN in (p.url or ""):
                    target_page = p
                    print(f"  ✓ 找到目标页面: {p.url[:80]}")
                    break

            if not target_page:
                print(f"  ▶ 导航到: {TARGET_URL}")
                target_page = await ctx.new_page()
                try:
                    await target_page.goto(TARGET_URL,
                                           wait_until="domcontentloaded", timeout=30000)
                except Exception as e:
                    print(f"  ⚠ 加载中（继续等待）: {type(e).__name__}")

            # 检查 CF 状态
            if await cf_is_blocked(target_page):
                print(f"\n  {'='*56}")
                print(f"  ⚠ 检测到 Cloudflare 验证 — 请在 Chrome 窗口中：")
                print(f"    1. 点击复选框「请验证您是真人」")
                print(f"    2. 等待绿色对勾，页面跳转后脚本自动继续")
                print(f"  {'='*56}")
                cf_ok = await wait_for_cf_pass(
                    target_page, CF_MANUAL_TIMEOUT, "主站(CDP)"
                )
                if not cf_ok:
                    return None
            else:
                print("  ✓ CF 已通过，直接进入捕获流程")

            # 被动模式：只监听响应，不拦截请求（避免 Fetch.enable 影响 lk1 iframe 加载）
            catcher = M3u8Catcher()
            await catcher.install_passive(ctx, target_page)

            # 快速检查是否已在播放（已有 m3u8）
            m3u8 = await catcher.wait(timeout=2)
            if m3u8:
                return m3u8

            # 点击 DS 服务器
            if VIDEO_SERVER:
                print(f"  ▶ 切换到 {VIDEO_SERVER} 服务器...")
                await click_server(target_page, VIDEO_SERVER)

            dump_frames(target_page)

            # 等待 lk1 播放器 iframe 真正加载（从 about:blank 变为实际 URL）
            print("  ⏳ 等待播放器 iframe 初始化...")
            for _t in range(45):
                frames = target_page.frames
                # CF 子 iframe 检测
                if any("challenges.cloudflare.com" in (f.url or "") for f in frames):
                    if _t == 0:
                        print("  ⏳ 播放器 iframe CF 验证中（最多 45s）...")
                    await asyncio.sleep(1)
                    continue
                # 检查是否有 lk1/playmogo 播放器 frame 且已离开 about:blank
                player_ready = any(
                    any(kw in (f.url or "") for kw in _PLAYER_KW)
                    and (f.url or "").startswith("http")
                    for f in frames
                )
                if player_ready:
                    await asyncio.sleep(2)  # 等播放器内部 JS 初始化
                    break
                await asyncio.sleep(1)

            dump_frames(target_page)

            print(f"\n  ⏳ 等待 m3u8（最多 {M3U8_WAIT_SEC}s）...")
            m3u8 = await catcher.wait(M3U8_WAIT_SEC)

            if not m3u8:
                print("  ▶ 触发视频播放...")
                await _trigger_play(target_page)
                await asyncio.sleep(3)
                m3u8 = await catcher.wait(10)

            if not m3u8:
                m3u8 = await catcher.query_player_api(target_page)

            if not m3u8:
                m3u8 = await catcher.search_dom(target_page)

            return m3u8

    except Exception as e:
        print(f"  ✗ CDP 会话失败: {e}")
        return None
    finally:
        # 只有脚本自己启动的 Chrome 才自动关闭
        if chrome_proc:
            try:
                chrome_proc.terminate()
            except Exception:
                pass
            await asyncio.sleep(1)
            if tmp_dir:
                try:
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                except Exception:
                    pass


# ───────────────────────────────────────────────────────────
#  rebrowser-playwright 会话（CDP Runtime.enable 补丁）
# ───────────────────────────────────────────────────────────

async def run_rebrowser_session(prefetch_cookies: list[dict]) -> Optional[str]:
    """
    rebrowser-playwright 补丁 Playwright 的 CDP Runtime.enable 调用。
    Cloudflare Turnstile 通过 Runtime.enable 检测自动化；
    rebrowser 屏蔽该调用，让浏览器看起来是正常用户启动。
    使用已有的 Chromium 二进制，无需下载额外文件。
    """
    try:
        from rebrowser_playwright.async_api import async_playwright as rb_playwright
    except ImportError:
        print("  ⚠ rebrowser-playwright 未安装，跳过（pip install rebrowser-playwright）")
        return None

    print("  ▶ rebrowser-playwright（CDP Runtime.enable 补丁）...")
    try:
        async with rb_playwright() as pw:
            exe = _find_chromium()
            args = [
                "--no-sandbox", "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
                "--window-size=1920,1080",
            ]
            kw = dict(headless=True, args=args)
            if exe:
                kw["executable_path"] = exe
            browser = await pw.chromium.launch(**kw)
            ctx = await browser.new_context(
                user_agent=USER_AGENT,
                viewport={"width": 1920, "height": 1080},
                ignore_https_errors=True,
                locale="zh-CN",
            )
            if prefetch_cookies:
                await ctx.add_cookies(prefetch_cookies)

            page = await ctx.new_page()
            await _apply_stealth(page)

            catcher = M3u8Catcher()
            await catcher.install(ctx, page)

            print(f"  ▶ 加载: {TARGET_URL}")
            try:
                await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                print(f"  ⚠ 加载超时（继续）: {e}")

            cf_ok = await wait_for_cf_pass(page, CF_AUTO_TIMEOUT, "主站(rebrowser)")
            if not cf_ok:
                dump_frames(page)
                await browser.close()
                return None

            m3u8 = await catcher.wait(timeout=2)
            if m3u8:
                await browser.close()
                return m3u8

            if VIDEO_SERVER:
                await click_server(page, VIDEO_SERVER)

            dump_frames(page)
            print(f"\n  ⏳ 等待 m3u8（最多 {M3U8_WAIT_SEC}s）...")
            m3u8 = await catcher.wait(M3U8_WAIT_SEC)

            if not m3u8:
                await _trigger_play(page)
                await asyncio.sleep(3)
                m3u8 = await catcher.wait(10)

            if not m3u8:
                m3u8 = await catcher.query_player_api(page)

            if not m3u8:
                m3u8 = await catcher.search_dom(page)

            await browser.close()
            return m3u8

    except Exception as e:
        print(f"  ✗ rebrowser 失败: {e}")
        return None


# ───────────────────────────────────────────────────────────
#  Camoufox 会话（Firefox 反指纹，最强 CF 绕过）
# ───────────────────────────────────────────────────────────

async def run_camoufox_session(prefetch_cookies: list[dict]) -> Optional[str]:
    """
    Camoufox = Firefox + 硬件指纹伪装，完全没有 CDP 自动化痕迹。
    对 Cloudflare Turnstile 最有效。
    安装: pip install camoufox && python -m camoufox fetch
    """
    try:
        from camoufox.async_api import AsyncCamoufox
    except ImportError:
        print("  ⚠ camoufox 未安装，跳过（pip install camoufox && python -m camoufox fetch）")
        return None

    print("  ▶ Camoufox（Firefox 反指纹浏览器）...")
    try:
        async with AsyncCamoufox(headless=True) as browser:
            ctx = await browser.new_context(
                locale="zh-CN",
                ignore_https_errors=True,
            )
            if prefetch_cookies:
                await ctx.add_cookies(prefetch_cookies)

            page = await ctx.new_page()
            catcher = M3u8Catcher()
            await catcher.install(ctx, page)

            print(f"  ▶ 加载: {TARGET_URL}")
            try:
                await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                print(f"  ⚠ 加载超时（继续）: {e}")

            cf_ok = await wait_for_cf_pass(page, CF_AUTO_TIMEOUT, "主站(Camoufox)")
            if not cf_ok:
                dump_frames(page)
                return None

            m3u8 = await catcher.wait(timeout=2)
            if m3u8:
                return m3u8

            if VIDEO_SERVER:
                await click_server(page, VIDEO_SERVER)

            dump_frames(page)

            print(f"\n  ⏳ 等待 m3u8（最多 {M3U8_WAIT_SEC}s）...")
            m3u8 = await catcher.wait(M3U8_WAIT_SEC)

            if not m3u8:
                print("  ▶ 触发视频播放...")
                await _trigger_play(page)
                await asyncio.sleep(3)
                m3u8 = await catcher.wait(10)

            if not m3u8:
                m3u8 = await catcher.query_player_api(page)

            if not m3u8:
                m3u8 = await catcher.search_dom(page)

            return m3u8

    except Exception as e:
        print(f"  ✗ Camoufox 失败: {e}")
        return None


# ───────────────────────────────────────────────────────────
#  真实 Chrome 配置会话（launch_persistent_context）
# ───────────────────────────────────────────────────────────

async def run_persistent_context_session(pw, prefetch_cookies: list[dict]) -> Optional[str]:
    """
    使用真实 Chrome 二进制 + 临时配置目录启动浏览器（显示模式）。

    Chrome 115+ 禁止在默认 User Data Dir 上开启 CDP 远程调试，所以
    不能直接用真实 profile。改用临时目录 + 真实 Chrome 可执行文件：
      · 真实 Chrome 二进制 → 真实 GPU/WebGL/字体指纹
      · 临时目录            → 绕过 Chrome 115+ CDP 限制
      · 显示模式            → 允许用户手动通过 CF Turnstile
    """
    import tempfile

    exe = _find_chromium()
    if not exe:
        print("  ⚠ 未找到 Chrome/Chromium 可执行文件，跳过 persistent context")
        return None

    # 判断是否是真实 Chrome（不是 Playwright 内置的 chromium）
    exe_lower = exe.replace("\\", "/").lower()
    is_real_chrome = ("google/chrome" in exe_lower or "google-chrome" in exe_lower
                      or "google\\chrome" in exe_lower)
    if not is_real_chrome:
        print("  ⚠ 未检测到真实 Google Chrome（找到的是 Playwright Chromium），跳过 persistent context")
        return None

    temp_dir = tempfile.mkdtemp(prefix="pw_chrome_session_")
    print(f"  ▶ 真实 Chrome 二进制: {exe}")
    print(f"  ▶ 临时配置目录: {temp_dir}")
    _ensure_display()

    # 优先用 rebrowser-playwright 以修补 CDP Runtime.enable，
    # 让 Turnstile 复选框可以被真人点击通过
    try:
        from rebrowser_playwright.async_api import async_playwright as _rb
        _pw_module = _rb
        print("  ✓ 使用 rebrowser-playwright（CDP Runtime.enable 已修补）")
    except ImportError:
        from playwright.async_api import async_playwright as _pw_plain
        _pw_module = _pw_plain

    ctx = None
    try:
        # launch_persistent_context 在 rebrowser_playwright 下行为相同
        _pw_instance = pw  # 直接用外层传入的 pw（已初始化）
        ctx = await _pw_instance.chromium.launch_persistent_context(
            user_data_dir=temp_dir,
            executable_path=exe,
            headless=False,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--window-size=1920,1080",
            ],
            ignore_https_errors=True,
            locale="zh-CN",
        )
    except Exception as e:
        msg = str(e).lower()
        if "non-default data directory" in msg or "remote-debugging" in msg:
            print("  ✗ Chrome 拒绝 CDP（安全限制），跳过此策略")
        elif "already in use" in msg or "singleton" in msg:
            print("  ✗ Chrome 正在运行！请关闭所有 Chrome 窗口后重试。")
        else:
            print(f"  ✗ persistent context 启动失败: {e}")
        shutil.rmtree(temp_dir, ignore_errors=True)
        return None

    try:
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        if prefetch_cookies:
            await ctx.add_cookies(prefetch_cookies)

        catcher = M3u8Catcher()
        await catcher.install(ctx, page)

        print(f"  ▶ 加载: {TARGET_URL}")
        try:
            await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            print(f"  ⚠ 加载超时（继续）: {e}")

        cf_timeout = CF_MANUAL_TIMEOUT
        print(f"  ℹ 真实 Chrome 显示模式（请手动通过 CF 验证，最多 {cf_timeout}s）")
        cf_ok = await wait_for_cf_pass(page, cf_timeout, "主站(真实Chrome)")
        if not cf_ok:
            dump_frames(page)
            await ctx.close()
            shutil.rmtree(temp_dir, ignore_errors=True)
            return None

        m3u8 = await catcher.wait(timeout=2)
        if m3u8:
            await ctx.close()
            shutil.rmtree(temp_dir, ignore_errors=True)
            return m3u8

        if VIDEO_SERVER:
            await click_server(page, VIDEO_SERVER)

        dump_frames(page)

        print(f"\n  ⏳ 等待 m3u8（最多 {M3U8_WAIT_SEC}s）...")
        m3u8 = await catcher.wait(M3U8_WAIT_SEC)

        if not m3u8:
            await _trigger_play(page)
            await asyncio.sleep(3)
            m3u8 = await catcher.wait(10)

        if not m3u8:
            m3u8 = await catcher.query_player_api(page)

        if not m3u8:
            m3u8 = await catcher.search_dom(page)

        await ctx.close()
        shutil.rmtree(temp_dir, ignore_errors=True)
        return m3u8

    except Exception as e:
        print(f"  ✗ persistent context 会话失败: {e}")
        try:
            await ctx.close()
        except Exception:
            pass
        shutil.rmtree(temp_dir, ignore_errors=True)
        return None


# ───────────────────────────────────────────────────────────
#  入口
# ───────────────────────────────────────────────────────────

async def main():
    print("supjav 视频下载器 v3 — 单会话自动 CF 绕过")
    print("=" * 60)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    # ── Step 0: curl-cffi 预热（可选）─────────────────────────────────
    print("\n[预热] curl-cffi TLS 指纹伪装...")
    prefetch_cookies = await curl_cffi_prefetch(TARGET_URL)

    # ── Step 1: 多策略单会话 CF 绕过 + 视频流捕获 ───────────────────
    print(f"\n[1/3] 启动浏览器（多策略依次尝试）...")
    m3u8: Optional[str] = None

    # 策略 A0: CDP 连接（自动启动 Chrome + 用户手动过 CF 复选框一次即可）
    print("\n  [A0] CDP 连接（自动启动 Chrome，仅需手动通过 Turnstile）")
    m3u8 = await run_cdp_connect_session()

    if not m3u8:
        # 策略 A: rebrowser-playwright（补丁 CDP Runtime.enable，无需下载新浏览器）
        print("\n  [A] rebrowser-playwright（CDP 反检测补丁）")
        m3u8 = await run_rebrowser_session(prefetch_cookies)

    if not m3u8:
        # 策略 B: Camoufox（Firefox 反指纹，最强 Turnstile 绕过）
        print("\n  [B] Camoufox（Firefox + 反指纹）")
        m3u8 = await run_camoufox_session(prefetch_cookies)

    if not m3u8:
        async with async_playwright() as pw:
            # 策略 C: Playwright headless + stealth（快速，低 IP 风险）
            print("\n  [C] Playwright headless + stealth")
            m3u8 = await run_single_session(pw, headless=True,
                                             prefetch_cookies=prefetch_cookies)

            if not m3u8:
                # 策略 D: 真实 Chrome 二进制 + 临时目录（弹出窗口，请手动过 CF）
                print("\n  [D] 真实 Chrome 显示模式（persistent context）")
                print("      ← 弹出浏览器后，请手动点击 Turnstile 复选框")
                m3u8 = await run_persistent_context_session(pw, prefetch_cookies)

            if not m3u8:
                # 策略 E: Playwright 显示模式（弹出浏览器供手动操作）
                print("\n  [E] Playwright 显示模式（请手动通过 CF 验证）")
                print("      ← 弹出浏览器后，请手动点击 Turnstile 复选框")
                m3u8 = await run_single_session(pw, headless=False,
                                                 prefetch_cookies=prefetch_cookies)

    if not m3u8:
        print("\n  ✗ 所有策略均未找到视频流")
        print()
        print("  ★ 排查建议：")
        print("    A0 策略需要系统已安装 Chrome（脚本自动启动）")
        print("    Chrome 弹出后请手动点击「请验证您是真人」复选框")
        print(f"    也可提前手动启动 Chrome（端口 {CDP_PORT} 开放后脚本自动连接）："
              f'\n      "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"'
              f' --remote-debugging-port={CDP_PORT} --user-data-dir=%TEMP%\\chrome_cdp')
        print()
        print("  其他排查建议：")
        print("    · 安装 Camoufox: pip install camoufox && python -m camoufox fetch")
        print("    · DS 播放器超时: 增大 M3U8_WAIT_SEC")
        sys.exit(1)

    print(f"\n  ✓ 视频流: {m3u8[:80]}")

    # ── Step 2: 下载前 N 秒 ────────────────────────────────────────
    print(f"\n[2/3] 下载前 {DOWNLOAD_SECONDS} 秒视频...")
    print(f"  输出: {OUTPUT_FILE}")
    ok = await VideoDownloader.download(m3u8, OUTPUT_FILE, DOWNLOAD_SECONDS)

    if ok:
        size = OUTPUT_FILE.stat().st_size if OUTPUT_FILE.exists() else 0
        print(f"\n  ✓ 完成: {OUTPUT_FILE} ({size / 1024:.1f} KB)")
        print(f"  播放: ffplay \"{OUTPUT_FILE}\"")
    else:
        t = f"-t {DOWNLOAD_SECONDS} " if DOWNLOAD_SECONDS else ""
        print(f'\n  手动: ffmpeg -allowed_extensions ALL {t}'
              f'-i "{m3u8}" -c copy "{OUTPUT_FILE}"')
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
