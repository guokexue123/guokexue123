"""
m3u8 提取器：用 Playwright 打开视频页面，通过网络拦截捕获 m3u8 URL。
返回 (m3u8_url, referer, user_agent)。
"""

import asyncio
import re
from urllib.parse import urlparse, urljoin
from pathlib import Path

from playwright.async_api import async_playwright, Page

from config import USER_AGENT, M3U8_TIMEOUT, BROWSER_HEADLESS, DOWNLOAD_DIR

_M3U8_RE = re.compile(r'https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*', re.IGNORECASE)

_AD_KW = (
    "googlesyndication.", "doubleclick.", "adnxs.", "amazon-adsystem.",
    "adtech.", "advertising.", "tracker.", "tracking.", "analytics.",
    "metrics.", "pixel.", "campaign", "banner", "widget",
)

_SKIP_CT  = ("image/", "font/", "text/javascript", "text/css",
             "text/html", "application/javascript", "application/json")
_SKIP_EXT = (".js", ".css", ".html", ".htm", ".json",
             ".png", ".jpg", ".jpeg", ".gif", ".svg",
             ".woff", ".woff2", ".ttf", ".eot", ".ico")


# ── stealth 兼容加载 ──────────────────────────────────────────────────────────

def _load_stealth():
    try:
        from playwright_stealth import stealth_async
        return stealth_async
    except ImportError:
        pass
    try:
        from playwright_stealth import Stealth
        s = Stealth()
        for method in ("apply_stealth_async", "use_async", "async_stealth", "__call__"):
            fn = getattr(s, method, None)
            if callable(fn):
                return fn
    except ImportError:
        pass
    return None


# ── 网络拦截 ──────────────────────────────────────────────────────────────────

def _is_m3u8_url(url: str, ct: str = "") -> bool:
    if ".m3u8" in url.lower():
        return True
    ct = ct.lower()
    return "mpegurl" in ct or "x-mpegurl" in ct


def _is_media_candidate(url: str, ct: str) -> bool:
    if any(ct.lower().startswith(t) for t in _SKIP_CT):
        return False
    return not any(url.lower().split("?")[0].endswith(e) for e in _SKIP_EXT)


def _cache_playlist(url: str, body: str, dl_dir: Path) -> str:
    """把无扩展名 m3u8 内容写本地，将相对 URL 转绝对，规避 CDN token 过期。"""
    base = url.rsplit("/", 1)[0] + "/"
    lines = []
    for line in body.splitlines():
        s = line.strip()
        if s and not s.startswith("#") and not s.startswith("http"):
            line = urljoin(base, s)
        lines.append(line)
    local = dl_dir / "playlist_cache.m3u8"
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_text("\n".join(lines), encoding="utf-8")
    return str(local)


async def _intercept_m3u8(page: Page, timeout: int, dl_dir: Path) -> str | None:
    """拦截网络请求，返回第一个 m3u8 URL（或本地缓存路径）。"""
    found  = asyncio.Event()
    result: list[str] = []

    def on_request(r):
        if not result and _is_m3u8_url(r.url):
            result.append(r.url)
            found.set()

    async def on_response(r):
        if result:
            return
        ct = ""
        try:
            ct = r.headers.get("content-type", "")
        except Exception:
            pass
        if _is_m3u8_url(r.url, ct):
            if not result:
                result.append(r.url)
                found.set()
            return
        if not _is_media_candidate(r.url, ct):
            return
        try:
            body = await r.text()
            if body.strip().startswith("#EXTM3U") and not result:
                local = _cache_playlist(r.url, body, dl_dir)
                result.append(local)
                found.set()
        except Exception:
            pass

    page.on("request", on_request)
    page.on("response", on_response)

    try:
        await asyncio.wait_for(found.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        pass

    return result[0] if result else None


# ── 触发播放 ──────────────────────────────────────────────────────────────────

async def _trigger_play(page: Page):
    """在主页面及非广告 iframe 中触发播放。"""
    play_sels = [
        "video", ".vjs-big-play-button", ".jw-display-icon-container",
        ".jw-icon-display", ".play-btn", ".btn-play",
        "[class*='play']", "[aria-label*='play' i]", "#player",
    ]
    for sel in play_sels:
        try:
            el = page.locator(sel).first
            if await el.count() > 0:
                try:
                    await el.scroll_into_view_if_needed()
                except Exception:
                    pass
                await el.click(timeout=3000)
                print("  触发视频播放...")
                return
        except Exception:
            pass

    for frame in page.frames:
        furl = frame.url or ""
        if not furl or "cloudflare.com" in furl:
            continue
        if any(kw in furl.lower() for kw in _AD_KW):
            continue
        for sel in play_sels[:4]:
            try:
                el = frame.locator(sel).first
                if await el.count() > 0:
                    await el.click(timeout=3000)
                    print("  触发视频播放（iframe）...")
                    return
            except Exception:
                pass

    # JS 强制播放
    for ctx in [page, *page.frames]:
        ctx_url = getattr(ctx, "url", "") or ""
        if "cloudflare.com" in ctx_url:
            continue
        try:
            n = await ctx.evaluate("""() => {
                const vs = document.querySelectorAll('video');
                vs.forEach(v => { try { v.play(); } catch(e) {} });
                return vs.length;
            }""")
            if n:
                print("  触发视频播放...")
                return
        except Exception:
            pass


# ── 备用：DOM/JS 搜索 m3u8 ───────────────────────────────────────────────────

async def _fallback_search(page: Page) -> str | None:
    ctxs = [page, *[
        f for f in page.frames
        if (f.url or "") and "cloudflare.com" not in (f.url or "")
        and not any(kw in (f.url or "").lower() for kw in _AD_KW)
    ]]

    for ctx in ctxs:
        for script in (
            "(() => { const v = document.querySelector('video'); return v && (v.currentSrc || v.src) || null; })()",
            "(() => { try { return jwplayer().getPlaylistItem().file || null; } catch(e) { return null; } })()",
        ):
            try:
                r = await ctx.evaluate(script)
                if r and isinstance(r, str) and r.startswith("http"):
                    return r
            except Exception:
                pass

    for ctx in ctxs:
        try:
            html = await ctx.content()
            m = _M3U8_RE.search(html)
            if m:
                return m.group(0)
        except Exception:
            pass

    return None


# ── 公共接口 ──────────────────────────────────────────────────────────────────

async def extract_m3u8_async(
    video_url: str,
) -> tuple[str | None, str, str]:
    """
    打开视频页面，提取 m3u8 URL。
    返回 (m3u8_url_or_None, referer, user_agent)
    """
    dl_dir = Path(DOWNLOAD_DIR)
    dl_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=BROWSER_HEADLESS,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled",
                  "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1920, "height": 1080},
        )
        page = await context.new_page()

        stealth = _load_stealth()
        if stealth:
            try:
                await stealth(page)
            except Exception:
                pass

        intercept_task = asyncio.create_task(
            _intercept_m3u8(page, M3U8_TIMEOUT, dl_dir)
        )

        try:
            await page.goto(video_url, wait_until="domcontentloaded", timeout=30_000)
        except Exception:
            pass

        await asyncio.sleep(2)

        if not intercept_task.done():
            await _trigger_play(page)
            await asyncio.sleep(3)

        # 等待网络拦截结果（最多再等 30s）
        m3u8: str | None = None
        if intercept_task.done():
            try:
                m3u8 = intercept_task.result()
            except Exception:
                pass
        else:
            try:
                m3u8 = await asyncio.wait_for(asyncio.shield(intercept_task), timeout=30)
            except asyncio.TimeoutError:
                pass

        if m3u8:
            print(f"  ✓ 方案A（网络拦截）找到 1 个 m3u8")
        else:
            m3u8 = await _fallback_search(page)
            if m3u8:
                print(f"  ✓ 方案B（DOM搜索）找到 m3u8")

        if m3u8:
            print(f"  ✓ m3u8 URL: {m3u8}")

        await browser.close()

    return m3u8, video_url, USER_AGENT
