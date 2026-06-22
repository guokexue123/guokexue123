#!/usr/bin/env python3
"""
supjav.com 视频下载器 v4 — 单会话自动 CF 绕过 + 高性能多线程下载

新特性 (v4 vs v3):
  · 可配置下载线程数（默认 16，支持 8 / 16 / 32 / 64）
  · 动态线程：DYNAMIC_THREADS=True 时自动按分片数选择最优并发
  · 实时进度条（█░）+ 速度（MB/s）+ 剩余时间 ETA
  · aria2c 自动切换（aria2c 可用时优先，速度更快，原生多连接）
  · ffmpeg 自动合并（分片下载完成后自动 concat 为 mp4）
  · 断点续传（已下载的分片自动跳过，重启无需重下）

工作流程（单会话）：
  ┌── 浏览器启动（stealth）
  ├── [可选] curl-cffi 预热
  ├── goto(target) → wait_for_cf_pass()
  ├── click_server(DS)
  ├── 拦截 m3u8
  └── 下载器流程:
        解析 m3u8 → 检查已有分片（断点续传）
        ├── aria2c 可用 → aria2c -j N -c → ffmpeg concat
        └── 否则       → ThreadPoolExecutor(N) → ffmpeg concat
              └── 进度线程：实时刷新进度条 / MB/s / ETA

依赖：
  pip install playwright playwright-stealth curl-cffi httpx
  playwright install chromium
  # Windows: winget install ffmpeg  或  winget install aria2
  # Linux:   apt install ffmpeg aria2
"""

import asyncio
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional, List, Tuple
from urllib.parse import urlparse, urljoin

import httpx
from playwright.async_api import (
    async_playwright, BrowserContext, Page, Frame, Route
)

# ═══════════════════════════════════════════════════════════
#  用户配置
# ═══════════════════════════════════════════════════════════

TARGET_URL       = "https://supjav.com/132824.html"
VIDEO_SERVER     = "DS"              # DS / TV / JPA / ST
DOWNLOAD_DIR     = Path("downloads")
OUTPUT_FILE      = DOWNLOAD_DIR / "video_5s.mp4"
DOWNLOAD_SECONDS = 5                 # 0 = 下载全部

# ── 下载线程配置 ────────────────────────────────────────────
DOWNLOAD_THREADS = 16                # 默认线程数（8 / 16 / 32 / 64）
DYNAMIC_THREADS  = True              # True = 自动按分片数选择线程数
#   分片数  →  线程数映射（DYNAMIC_THREADS=True 时生效）
THREAD_MAP = {
    0:   8,    # < 50 分片
    50:  16,   # 50–199 分片
    200: 32,   # 200–499 分片
    500: 64,   # ≥ 500 分片
}

# ── 分片存储目录 ─────────────────────────────────────────────
SEGMENTS_DIR     = DOWNLOAD_DIR / "segments"   # 分片缓存目录（断点续传）

# headless=True 时 CF 自动等待上限（秒）
CF_AUTO_TIMEOUT  = 30
CF_MANUAL_TIMEOUT = 300
M3U8_WAIT_SEC    = 60

CHROMIUM_BIN = ""
CDP_PORT     = 9222

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

_CF_TITLES = frozenset({
    "just a moment...", "checking your browser...",
    "attention required! | cloudflare", "请稍候…", "请稍候",
    "access denied", "one more step",
})

_CF_DOM_SELS = (
    "form#challenge-form",
    "#cf-challenge-running",
    ".cf-browser-verification",
    "#challenge-error-title",
    "[id^='cf-chl-widget']",
    "#cf-wrapper",
    "#cf-error-details",
)

_REAL_CONTENT_SELS = (
    "video", "article", ".entry-content", "#content", ".post-content",
    "#player", "[class*='server']", "[class*='source']", "[class*='sorc']",
    ".post-title", ".entry-title", "h1.title",
)


# ───────────────────────────────────────────────────────────
#  CF 状态检测
# ───────────────────────────────────────────────────────────

async def cf_is_blocked(page: Page) -> bool:
    if any("challenges.cloudflare.com" in (f.url or "") for f in page.frames):
        return True
    try:
        result = await page.evaluate(f"""() => {{
            const t = document.title.trim().toLowerCase();
            const cfTitles = {list(_CF_TITLES)};
            if (cfTitles.includes(t)) return 'title';
            const cfSels = {list(_CF_DOM_SELS)};
            if (cfSels.some(s => document.querySelector(s))) return 'dom';
            const realSels = {list(_REAL_CONTENT_SELS)};
            if (realSels.some(s => document.querySelector(s))) return 'real';
            const links = document.querySelectorAll('a[href]').length;
            if (links > 8) return 'links';
            return 'unknown';
        }}""")
        return result in ("title", "dom")
    except Exception:
        return False


async def wait_for_cf_pass(page: Page, timeout: int, label: str = "页面") -> bool:
    import random
    await asyncio.sleep(1.5)
    for i in range(timeout):
        blocked = await cf_is_blocked(page)
        if not blocked:
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
#  curl-cffi 预热
# ───────────────────────────────────────────────────────────

async def curl_cffi_prefetch(url: str) -> list[dict]:
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
    if not server:
        return False
    print(f"  ▶ 切换到 {server} 服务器...")
    found_content = False
    for sel in (
        f":text('{server}')", "button.srv-btn", "[class*='server']",
        "[class*='source']", "[class*='sorc']", "li", "button",
    ):
        try:
            await page.wait_for_selector(sel, timeout=3000)
            found_content = True
            break
        except Exception:
            continue
    if not found_content:
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
    try:
        await page.evaluate(
            "window.scrollTo(0, Math.min(300, document.body.scrollHeight * 0.1))"
        )
        await asyncio.sleep(0.3)
    except Exception:
        pass
    for sel in (
        f":text-is('{server}')", f":text('{server}')",
        f"li:has-text('{server}')", f"button:has-text('{server}')",
        f"a:has-text('{server}')", f"span:has-text('{server}')",
        f"div:has-text('{server}')", f"[class*='{server.lower()}']",
        f"[class*='server']:has-text('{server}')", f"[class*='source']:has-text('{server}')",
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
    _M3U8_RE = re.compile(r'https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*', re.IGNORECASE)

    def __init__(self):
        self._url: Optional[str] = None
        self._event = asyncio.Event()

    async def install(self, ctx: BrowserContext, page: Page):
        await ctx.route("**/*", self._route_handler)
        page.on("response", self._response_handler)

    async def install_passive(self, ctx: BrowserContext, page: Page):
        ctx.on("response", self._response_handler)
        try:
            cdp = await ctx.new_cdp_session(page)
            await cdp.send("Network.enable", {
                "maxTotalBufferSize": 0, "maxResourceBufferSize": 0, "maxPostDataSize": 0,
            })
            def _on_cdp_response(event):
                url = event.get("response", {}).get("url", "")
                if ".m3u8" in url.lower():
                    print(f"\n  ✓ [CDP] 捕获 m3u8: {url[:90]}")
                    self._set(url)
            cdp.on("Network.responseReceived", _on_cdp_response)
            print("  ✓ CDP Network 监听已启动（被动模式）")
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
#  v4 下载核心：进度跟踪
# ───────────────────────────────────────────────────────────

class SegmentProgress:
    """线程安全进度跟踪器，供进度条线程读取。"""

    def __init__(self, total_segs: int, skipped: int = 0):
        self.total_segs   = total_segs
        self.done_segs    = skipped          # 已跳过（已下载）的算作完成
        self.done_bytes   = 0
        self.skipped_segs = skipped
        self._lock        = threading.Lock()
        self._start_time  = time.monotonic()
        self._speed_bytes = 0.0              # 当前速度 bytes/s（EMA 平滑）
        self._last_bytes  = 0
        self._last_ts     = time.monotonic()
        self.failed_segs  = 0
        self._active      = True             # False 时进度线程退出

    def add(self, seg_bytes: int):
        now = time.monotonic()
        with self._lock:
            self.done_segs  += 1
            self.done_bytes += seg_bytes
            dt = now - self._last_ts
            if dt >= 0.4:
                instant = (self.done_bytes - self._last_bytes) / dt
                # EMA α=0.3
                self._speed_bytes = 0.3 * instant + 0.7 * self._speed_bytes
                self._last_bytes  = self.done_bytes
                self._last_ts     = now

    def add_fail(self):
        with self._lock:
            self.failed_segs += 1
            self.done_segs   += 1   # 失败也算结束，避免进度卡住

    def stop(self):
        self._active = False

    def render(self) -> str:
        with self._lock:
            done   = self.done_segs
            total  = self.total_segs
            speed  = self._speed_bytes
            mbytes = self.done_bytes / 1024 / 1024
            failed = self.failed_segs
            skipped = self.skipped_segs

        pct      = min(done / total, 1.0) if total else 0.0
        bar_w    = 28
        filled   = int(bar_w * pct)
        bar      = "█" * filled + "░" * (bar_w - filled)
        spd_str  = f"{speed/1024/1024:.2f} MB/s" if speed > 0 else "-- MB/s"

        if speed > 0 and done < total and done > 0:
            avg_b   = (mbytes * 1024 * 1024) / done
            eta_s   = int(avg_b * (total - done) / speed)
            mm, ss  = divmod(eta_s, 60)
            eta_str = f"ETA {mm:02d}:{ss:02d}"
        elif done >= total:
            eta_str = "完成"
        else:
            eta_str = "ETA --:--"

        skip_str = f" | 续传:{skipped}" if skipped else ""
        fail_str = f" | ✗{failed}" if failed else ""
        return (
            f"\r  [{bar}] {done}/{total} ({pct*100:.1f}%)"
            f" {mbytes:.1f}MB | {spd_str} | {eta_str}{skip_str}{fail_str}  "
        )


def _progress_loop(prog: SegmentProgress):
    """在子线程中每 0.3s 刷新一次进度行。"""
    while prog._active:
        sys.stdout.write(prog.render())
        sys.stdout.flush()
        time.sleep(0.3)
    # 最后刷一次完整行
    sys.stdout.write(prog.render())
    sys.stdout.write("\n")
    sys.stdout.flush()


# ───────────────────────────────────────────────────────────
#  v4 下载核心：工具查找
# ───────────────────────────────────────────────────────────

def _find_ffmpeg() -> Optional[str]:
    if found := shutil.which("ffmpeg"):
        return found
    for p in (
        r"D:\Tool\ffmpeg\bin\ffmpeg.exe",
        r"C:\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files (x86)\ffmpeg\bin\ffmpeg.exe",
    ):
        if Path(p).exists():
            return p
    return None


def _find_aria2c() -> Optional[str]:
    if found := shutil.which("aria2c"):
        return found
    for p in (
        r"D:\Tool\aria2\aria2c.exe",
        r"C:\aria2\aria2c.exe",
        r"C:\Program Files\aria2\aria2c.exe",
    ):
        if Path(p).exists():
            return p
    return None


def _choose_threads(total_segs: int) -> int:
    if not DYNAMIC_THREADS:
        return DOWNLOAD_THREADS
    for threshold in sorted(THREAD_MAP.keys(), reverse=True):
        if total_segs >= threshold:
            return THREAD_MAP[threshold]
    return DOWNLOAD_THREADS


# ───────────────────────────────────────────────────────────
#  v4 下载核心：m3u8 解析
# ───────────────────────────────────────────────────────────

def _parse_m3u8(m3u8: str, seconds: int, headers: dict) -> Tuple[str, List[str], bool]:
    """
    解析 m3u8，返回 (base_url, segment_urls, is_encrypted)。
    若 m3u8 是主播放列表（含 EXT-X-STREAM-INF），自动选最高码率子列表。
    seconds > 0 时只取前 N 秒的分片。
    """
    is_local  = not m3u8.startswith("http") and Path(m3u8).exists()
    base_url  = (Path(m3u8).parent.as_uri() + "/") if is_local \
                else (m3u8.rsplit("/", 1)[0] + "/")

    if is_local:
        text = Path(m3u8).read_text(encoding="utf-8")
    else:
        with httpx.Client(headers=headers, timeout=20, follow_redirects=True) as c:
            text = c.get(m3u8).text

    # 主列表 → 选最高码率子列表
    if "#EXT-X-STREAM-INF" in text:
        best_url, best_bw = None, -1
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if line.startswith("#EXT-X-STREAM-INF"):
                bw_m = re.search(r"BANDWIDTH=(\d+)", line)
                bw = int(bw_m.group(1)) if bw_m else 0
                if bw > best_bw and i + 1 < len(lines):
                    candidate = lines[i + 1].strip()
                    if candidate and not candidate.startswith("#"):
                        best_url = candidate if candidate.startswith("http") \
                                   else urljoin(base_url, candidate)
                        best_bw  = bw
        if best_url:
            print(f"  ℹ 主播放列表，选子流（{best_bw} bps）: {best_url[:80]}")
            return _parse_m3u8(best_url, seconds, headers)

    # 提取分片 URL
    is_enc  = "#EXT-X-KEY" in text
    segs    = []
    elapsed = 0.0
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("#EXTINF:"):
            try:
                elapsed += float(s[8:].split(",")[0])
            except Exception:
                pass
        elif s and not s.startswith("#"):
            url = s if s.startswith("http") else urllib.parse.urljoin(base_url, s)
            segs.append(url)
            if seconds > 0 and elapsed >= seconds:
                break

    return base_url, segs, is_enc


# ───────────────────────────────────────────────────────────
#  v4 下载核心：单分片下载（带重试）
# ───────────────────────────────────────────────────────────

def _download_segment(
    idx: int,
    url: str,
    out_path: Path,
    headers: dict,
    prog: SegmentProgress,
    retries: int = 4,
) -> bool:
    """下载单个分片到 out_path，失败自动重试，更新进度。"""
    # 断点续传：文件已存在且非空
    if out_path.exists() and out_path.stat().st_size > 0:
        prog.add(out_path.stat().st_size)
        return True

    for attempt in range(retries):
        try:
            with httpx.Client(headers=headers, timeout=30, follow_redirects=True) as c:
                resp = c.get(url)
                resp.raise_for_status()
                data = resp.content
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(data)
            prog.add(len(data))
            return True
        except Exception as e:
            if attempt == retries - 1:
                prog.add_fail()
                return False
            time.sleep(1.5 * (attempt + 1))
    return False


# ───────────────────────────────────────────────────────────
#  v4 下载核心：aria2c 下载器
# ───────────────────────────────────────────────────────────

def _download_aria2c(
    segs: List[str],
    seg_dir: Path,
    headers: dict,
    threads: int,
    aria2c_bin: str,
) -> bool:
    """
    用 aria2c 下载所有分片。
    返回 True 表示全部成功（包括已存在的断点续传分片）。
    """
    seg_dir.mkdir(parents=True, exist_ok=True)

    # 构建 aria2c 输入文件（已有分片自动跳过）
    input_lines = []
    need_download = 0
    for i, url in enumerate(segs):
        out_name = f"seg_{i:06d}.ts"
        out_path = seg_dir / out_name
        if out_path.exists() and out_path.stat().st_size > 0:
            continue  # 断点续传：跳过
        input_lines.append(url)
        input_lines.append(f"  out={out_name}")
        input_lines.append("")
        need_download += 1

    skipped = len(segs) - need_download
    if skipped:
        print(f"  ✓ 断点续传：跳过 {skipped} 个已下载分片")

    if need_download == 0:
        print("  ✓ 所有分片已下载（断点续传完成）")
        return True

    input_file = seg_dir / "aria2_input.txt"
    input_file.write_text("\n".join(input_lines), encoding="utf-8")

    header_args = []
    for k, v in headers.items():
        header_args += ["--header", f"{k}: {v}"]

    cmd = [
        aria2c_bin,
        f"--input-file={input_file}",
        f"--dir={seg_dir}",
        f"--max-concurrent-downloads={threads}",
        f"--split={max(1, threads // 4)}",
        "--continue=true",           # 断点续传
        "--auto-file-renaming=false",
        "--retry-wait=2",
        "--max-tries=5",
        "--file-allocation=none",
        "--quiet=false",             # 显示 aria2c 自带进度
        "--summary-interval=3",
    ] + header_args

    print(f"  aria2c 并发下载 {need_download} 个分片（{threads} 连接）...")
    ret = subprocess.run(cmd)
    input_file.unlink(missing_ok=True)

    # 验证分片完整性
    missing = [
        i for i, url in enumerate(segs)
        if not (seg_dir / f"seg_{i:06d}.ts").exists()
        or (seg_dir / f"seg_{i:06d}.ts").stat().st_size == 0
    ]
    if missing:
        print(f"  ⚠ aria2c 完成后仍缺 {len(missing)} 个分片: {missing[:10]}")
        return False
    return ret.returncode == 0


# ───────────────────────────────────────────────────────────
#  v4 下载核心：Python 多线程下载器
# ───────────────────────────────────────────────────────────

def _download_python(
    segs: List[str],
    seg_dir: Path,
    headers: dict,
    threads: int,
) -> bool:
    """ThreadPoolExecutor 多线程下载分片，实时进度条。"""
    seg_dir.mkdir(parents=True, exist_ok=True)

    # 统计已有分片（断点续传）
    skipped = sum(
        1 for i in range(len(segs))
        if (seg_dir / f"seg_{i:06d}.ts").exists()
        and (seg_dir / f"seg_{i:06d}.ts").stat().st_size > 0
    )
    if skipped:
        print(f"  ✓ 断点续传：跳过 {skipped} 个已下载分片")

    prog = SegmentProgress(total_segs=len(segs), skipped=skipped)
    # 把已有分片的字节数也统计进来（近似）
    for i in range(len(segs)):
        p = seg_dir / f"seg_{i:06d}.ts"
        if p.exists() and p.stat().st_size > 0:
            prog.done_bytes += p.stat().st_size

    pth = threading.Thread(target=_progress_loop, args=(prog,), daemon=True)
    pth.start()

    futures = {}
    with ThreadPoolExecutor(max_workers=threads) as pool:
        for i, url in enumerate(segs):
            out_path = seg_dir / f"seg_{i:06d}.ts"
            if out_path.exists() and out_path.stat().st_size > 0:
                continue  # 已存在，跳过提交
            fut = pool.submit(_download_segment, i, url, out_path, headers, prog)
            futures[fut] = i

        for fut in as_completed(futures):
            pass  # 进度由 _download_segment 内部更新

    prog.stop()
    pth.join(timeout=2)

    if prog.failed_segs:
        print(f"  ⚠ {prog.failed_segs} 个分片下载失败")
        return False
    return True


# ───────────────────────────────────────────────────────────
#  v4 下载核心：ffmpeg 合并分片
# ───────────────────────────────────────────────────────────

def _merge_segments(seg_dir: Path, seg_count: int, out: Path, ffmpeg_bin: str) -> bool:
    """用 ffmpeg concat demuxer 合并所有 .ts 分片为 mp4。"""
    concat_file = seg_dir / "concat.txt"
    lines = []
    for i in range(seg_count):
        p = seg_dir / f"seg_{i:06d}.ts"
        if p.exists():
            lines.append(f"file '{p.resolve()}'")
    concat_file.write_text("\n".join(lines), encoding="utf-8")

    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg_bin, "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(concat_file),
        "-c", "copy",
        "-bsf:a", "aac_adtstoasc",
        str(out),
    ]
    print(f"  ffmpeg 合并 {len(lines)} 个分片 → {out}")
    ret = subprocess.run(cmd, capture_output=True)
    concat_file.unlink(missing_ok=True)
    if ret.returncode != 0:
        err = ret.stderr.decode(errors="replace")[-500:]
        print(f"  ✗ ffmpeg 合并失败 (exit {ret.returncode}):\n{err}")
        return False
    return True


# ───────────────────────────────────────────────────────────
#  v4 下载核心：ffmpeg 直接下载（远程 m3u8 + 时间截断）
# ───────────────────────────────────────────────────────────

def _ffmpeg_direct(m3u8: str, out: Path, ffbin: str, seconds: int) -> bool:
    """ffmpeg 直接从远程 m3u8 下载（支持 -t 截断、AES 解密）。"""
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
    print(f"  ffmpeg 直接下载（{lbl}）: {ffbin}")
    ret = subprocess.run(cmd, capture_output=True)
    if ret.returncode != 0:
        err = ret.stderr.decode(errors="replace")[-400:]
        print(f"  ✗ ffmpeg 失败 (exit {ret.returncode}):\n{err}")
    return ret.returncode == 0


# ───────────────────────────────────────────────────────────
#  v4 VideoDownloader：主下载入口
# ───────────────────────────────────────────────────────────

class VideoDownloader:
    """
    v4 下载器流程：
      1. 查找 ffmpeg / aria2c
      2. 解析 m3u8 → 分片列表
      3. 检测加密（AES）→ 加密则只用 ffmpeg direct
      4. 选线程数（动态）
      5. aria2c 可用 → aria2c 下载 → ffmpeg 合并
         否则       → Python 多线程下载 → ffmpeg 合并
      6. 无 ffmpeg   → Python 多线程下载 → 保存 .ts
    """

    @classmethod
    async def download(cls, m3u8: str, out: Path, seconds: int = 0) -> bool:
        out.parent.mkdir(parents=True, exist_ok=True)
        ffmpeg  = _find_ffmpeg()
        aria2c  = _find_aria2c()

        print(f"  ffmpeg : {ffmpeg or '未找到'}")
        print(f"  aria2c : {aria2c or '未找到'}")

        is_remote = m3u8.startswith("http")
        headers   = {"Referer": TARGET_URL, "User-Agent": USER_AGENT}

        # ── 解析 m3u8 ────────────────────────────────────────
        print("  解析 m3u8...")
        try:
            _, segs, is_enc = _parse_m3u8(m3u8, seconds, headers)
        except Exception as e:
            print(f"  ✗ m3u8 解析失败: {e}")
            return False

        total = len(segs)
        if not total:
            print("  ✗ 未找到分片")
            return False

        print(f"  分片数: {total}  加密: {'是（AES）' if is_enc else '否'}")

        # ── AES 加密 → 只能用 ffmpeg direct ─────────────────
        if is_enc:
            if not ffmpeg:
                print("  ✗ AES 加密流需要 ffmpeg，但未找到 ffmpeg")
                return False
            print("  ℹ AES 加密流 → ffmpeg 直接下载（自动解密）")
            return _ffmpeg_direct(m3u8, out, ffmpeg, seconds)

        # ── 选线程数 ──────────────────────────────────────────
        threads = _choose_threads(total)
        print(f"  并发线程: {threads}（分片数={total}，动态={DYNAMIC_THREADS}）")

        # ── 分片存储目录（以输出文件名命名，保证隔离）─────────
        seg_dir = SEGMENTS_DIR / out.stem
        seg_dir.mkdir(parents=True, exist_ok=True)

        # ── 下载分片 ──────────────────────────────────────────
        if aria2c:
            ok = _download_aria2c(segs, seg_dir, headers, threads, aria2c)
        else:
            print(f"  Python 多线程下载（{threads} 线程）...")
            ok = _download_python(segs, seg_dir, headers, threads)

        if not ok:
            print("  ⚠ 部分分片下载失败，尝试合并已有分片...")

        # ── ffmpeg 合并 ────────────────────────────────────────
        if ffmpeg:
            merged = _merge_segments(seg_dir, total, out, ffmpeg)
            if merged:
                # 合并成功后清理分片缓存
                try:
                    shutil.rmtree(seg_dir)
                    print(f"  ✓ 分片缓存已清理: {seg_dir}")
                except Exception:
                    pass
            return merged
        else:
            # 无 ffmpeg：将所有分片追加合并为 .ts
            print("  ⚠ 未找到 ffmpeg，合并为 .ts（需手动转码）")
            ts_out = out.with_suffix(".ts")
            with open(ts_out, "wb") as f:
                for i in range(total):
                    p = seg_dir / f"seg_{i:06d}.ts"
                    if p.exists():
                        f.write(p.read_bytes())
            print(f"  ✓ 已保存: {ts_out}")
            print(f'  转换命令: ffmpeg -i "{ts_out}" -c copy "{out}"')
            return True


# ───────────────────────────────────────────────────────────
#  浏览器辅助
# ───────────────────────────────────────────────────────────

def _find_chromium() -> Optional[str]:
    if CHROMIUM_BIN and Path(CHROMIUM_BIN).exists():
        return CHROMIUM_BIN
    for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable"):
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
    for name in ("chrome", "google-chrome", "chromium"):
        found = shutil.which(name)
        if found:
            return found
    return None


async def _apply_stealth(page: Page):
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


_xvfb_proc = None

def _ensure_display():
    import os
    global _xvfb_proc
    if os.environ.get("DISPLAY"):
        return
    xvfb = shutil.which("Xvfb")
    if not xvfb:
        return
    try:
        _xvfb_proc = subprocess.Popen(
            [xvfb, ":99", "-screen", "0", "1920x1080x24"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        os.environ["DISPLAY"] = ":99"
        time.sleep(0.8)
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
    browser = await _launch_browser(pw, headless)
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

    m3u8 = await catcher.wait(timeout=2)
    if m3u8:
        await browser.close()
        return m3u8

    if VIDEO_SERVER:
        await click_server(page, VIDEO_SERVER)

    dump_frames(page)

    player_cf_wait = 60 if not headless else 20
    for _t in range(player_cf_wait):
        cf_in_frames = any(
            "challenges.cloudflare.com" in (f.url or "")
            for f in page.frames if f != page.main_frame
        )
        if not cf_in_frames:
            break
        if _t == 0:
            print(f"  ⏳ 播放器 iframe CF 验证中（最多 {player_cf_wait}s）...")
        await asyncio.sleep(1)

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
#  策略 A0：connect_over_cdp
# ───────────────────────────────────────────────────────────

async def run_cdp_connect_session() -> Optional[str]:
    import socket
    import tempfile

    cdp_url    = f"http://localhost:{CDP_PORT}"
    chrome_proc = None
    tmp_dir    = None

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
            print("  ⚠ 未找到 Chrome，跳过 CDP 策略")
            return None

        tmp_dir = tempfile.mkdtemp(prefix="chrome_cdp_")
        cmd = [
            chrome_bin,
            f"--remote-debugging-port={CDP_PORT}",
            f"--user-data-dir={tmp_dir}",
            "--no-first-run", "--no-default-browser-check", "--disable-extensions",
        ]
        print(f"  ▶ 自动启动 Chrome: {Path(chrome_bin).name}")
        try:
            chrome_proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            print(f"  ✗ Chrome 启动失败: {e}")
            return None

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
                    await target_page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=30000)
                except Exception as e:
                    print(f"  ⚠ 加载中（继续等待）: {type(e).__name__}")

            if await cf_is_blocked(target_page):
                print(f"\n  {'='*56}")
                print(f"  ⚠ 检测到 Cloudflare 验证 — 请在 Chrome 窗口中点击复选框")
                print(f"  {'='*56}")
                cf_ok = await wait_for_cf_pass(target_page, CF_MANUAL_TIMEOUT, "主站(CDP)")
                if not cf_ok:
                    return None
            else:
                print("  ✓ CF 已通过，直接进入捕获流程")

            catcher = M3u8Catcher()
            await catcher.install_passive(ctx, target_page)

            m3u8 = await catcher.wait(timeout=2)
            if m3u8:
                return m3u8

            if VIDEO_SERVER:
                await click_server(target_page, VIDEO_SERVER)

            lk1_url = None
            for _t in range(15):
                for f in target_page.frames:
                    url = f.url or ""
                    if ("lk1." in url or "supremejav.com" in url) and url.startswith("http"):
                        lk1_url = url
                        break
                if lk1_url:
                    break
                await asyncio.sleep(1)

            dump_frames(target_page)

            if lk1_url:
                print(f"  ▶ 新标签页打开播放器: {lk1_url[:80]}")
                player_page = await ctx.new_page()
                player_page.on("response", catcher._response_handler)
                try:
                    await player_page.goto(lk1_url, wait_until="domcontentloaded", timeout=20000)
                except Exception as e:
                    print(f"  ⚠ 播放器页加载: {type(e).__name__}")

                print(f"  ⏳ 等待 m3u8（最多 30s）...")
                m3u8 = await catcher.wait(30)
                if not m3u8:
                    await _trigger_play(player_page)
                    await asyncio.sleep(3)
                    m3u8 = await catcher.wait(10)
                if not m3u8:
                    m3u8 = await catcher.query_player_api(player_page)
                if not m3u8:
                    m3u8 = await catcher.search_dom(player_page)
            else:
                print(f"  ⚠ 未找到 lk1 iframe URL，降级到 iframe 模式...")
                print(f"  ⏳ 等待 m3u8（最多 {M3U8_WAIT_SEC}s）...")
                m3u8 = await catcher.wait(M3U8_WAIT_SEC)
                if not m3u8:
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
        if chrome_proc:
            try:
                chrome_proc.terminate()
            except Exception:
                pass
            await asyncio.sleep(1)
            if tmp_dir:
                shutil.rmtree(tmp_dir, ignore_errors=True)


# ───────────────────────────────────────────────────────────
#  rebrowser-playwright 会话
# ───────────────────────────────────────────────────────────

async def run_rebrowser_session(prefetch_cookies: list[dict]) -> Optional[str]:
    try:
        from rebrowser_playwright.async_api import async_playwright as rb_playwright
    except ImportError:
        print("  ⚠ rebrowser-playwright 未安装，跳过（pip install rebrowser-playwright）")
        return None

    print("  ▶ rebrowser-playwright（CDP Runtime.enable 补丁）...")
    try:
        async with rb_playwright() as pw:
            exe  = _find_chromium()
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
#  Camoufox 会话
# ───────────────────────────────────────────────────────────

async def run_camoufox_session(prefetch_cookies: list[dict]) -> Optional[str]:
    try:
        from camoufox.async_api import AsyncCamoufox
    except ImportError:
        print("  ⚠ camoufox 未安装，跳过（pip install camoufox && python -m camoufox fetch）")
        return None

    print("  ▶ Camoufox（Firefox 反指纹浏览器）...")
    try:
        async with AsyncCamoufox(headless=True) as browser:
            ctx = await browser.new_context(locale="zh-CN", ignore_https_errors=True)
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
#  真实 Chrome 持久上下文会话
# ───────────────────────────────────────────────────────────

async def run_persistent_context_session(pw, prefetch_cookies: list[dict]) -> Optional[str]:
    import tempfile

    exe = _find_chromium()
    if not exe:
        print("  ⚠ 未找到 Chrome/Chromium，跳过 persistent context")
        return None

    exe_lower = exe.replace("\\", "/").lower()
    is_real_chrome = ("google/chrome" in exe_lower or "google-chrome" in exe_lower
                      or "google\\chrome" in exe_lower)
    if not is_real_chrome:
        print("  ⚠ 未检测到真实 Google Chrome，跳过 persistent context")
        return None

    temp_dir = tempfile.mkdtemp(prefix="pw_chrome_session_")
    print(f"  ▶ 真实 Chrome: {exe}")
    _ensure_display()

    ctx = None
    try:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=temp_dir,
            executable_path=exe,
            headless=False,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled", "--window-size=1920,1080"],
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

        print(f"  ℹ 真实 Chrome 显示模式（请手动通过 CF 验证，最多 {CF_MANUAL_TIMEOUT}s）")
        cf_ok = await wait_for_cf_pass(page, CF_MANUAL_TIMEOUT, "主站(真实Chrome)")
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
    print("supjav 视频下载器 v4 — 单会话 CF 绕过 + 多线程高速下载")
    print("=" * 60)
    print(f"  目标  : {TARGET_URL}")
    print(f"  服务器: {VIDEO_SERVER}  截断: {DOWNLOAD_SECONDS}s  线程: {DOWNLOAD_THREADS}")
    print(f"  ffmpeg: {_find_ffmpeg() or '未找到'}  aria2c: {_find_aria2c() or '未找到'}")
    print("=" * 60)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    SEGMENTS_DIR.mkdir(parents=True, exist_ok=True)

    # ── Step 0: curl-cffi 预热 ────────────────────────────
    print("\n[预热] curl-cffi TLS 指纹伪装...")
    prefetch_cookies = await curl_cffi_prefetch(TARGET_URL)

    # ── Step 1: 多策略 CF 绕过 + m3u8 捕获 ───────────────
    print(f"\n[1/3] 启动浏览器（多策略依次尝试）...")
    m3u8: Optional[str] = None

    print("\n  [A0] CDP 连接（自动启动 Chrome，仅需手动通过 Turnstile）")
    m3u8 = await run_cdp_connect_session()

    if not m3u8:
        print("\n  [A] rebrowser-playwright（CDP 反检测补丁）")
        m3u8 = await run_rebrowser_session(prefetch_cookies)

    if not m3u8:
        print("\n  [B] Camoufox（Firefox + 反指纹）")
        m3u8 = await run_camoufox_session(prefetch_cookies)

    if not m3u8:
        async with async_playwright() as pw:
            print("\n  [C] Playwright headless + stealth")
            m3u8 = await run_single_session(pw, headless=True, prefetch_cookies=prefetch_cookies)

            if not m3u8:
                print("\n  [D] 真实 Chrome 显示模式（persistent context）")
                print("      ← 弹出浏览器后，请手动点击 Turnstile 复选框")
                m3u8 = await run_persistent_context_session(pw, prefetch_cookies)

            if not m3u8:
                print("\n  [E] Playwright 显示模式（请手动通过 CF 验证）")
                print("      ← 弹出浏览器后，请手动点击 Turnstile 复选框")
                m3u8 = await run_single_session(pw, headless=False, prefetch_cookies=prefetch_cookies)

    if not m3u8:
        print("\n  ✗ 所有策略均未找到视频流")
        print("\n  ★ 排查建议：")
        print("    · A0 策略需系统已安装 Chrome（脚本自动启动）")
        print("    · Chrome 弹出后请手动点击「请验证您是真人」复选框")
        print(f"    · 也可提前手动启动 Chrome（端口 {CDP_PORT} 开放后脚本自动连接）：")
        print(f'      "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"')
        print(f'      --remote-debugging-port={CDP_PORT} --user-data-dir=%TEMP%\\chrome_cdp')
        print("\n  其他建议：")
        print("    · pip install camoufox && python -m camoufox fetch")
        print("    · 增大 M3U8_WAIT_SEC（当前=%d）" % M3U8_WAIT_SEC)
        if sys.platform == "win32":
            input("\n  按 Enter 键退出...")
        sys.exit(1)

    print(f"\n  ✓ 视频流: {m3u8[:80]}")

    # ── Step 2: 高速多线程下载 ────────────────────────────
    lbl = f"前{DOWNLOAD_SECONDS}秒" if DOWNLOAD_SECONDS else "完整"
    print(f"\n[2/3] 下载视频（{lbl}）...")
    print(f"  输出: {OUTPUT_FILE}")
    ok = await VideoDownloader.download(m3u8, OUTPUT_FILE, DOWNLOAD_SECONDS)

    # ── Step 3: 结果 ──────────────────────────────────────
    print("\n[3/3] 结果")
    if ok and OUTPUT_FILE.exists():
        size = OUTPUT_FILE.stat().st_size
        print(f"  ✓ 完成: {OUTPUT_FILE} ({size / 1024 / 1024:.2f} MB)")
        print(f"  播放: ffplay \"{OUTPUT_FILE}\"")
    else:
        ff = _find_ffmpeg() or "ffmpeg"
        t  = f"-t {DOWNLOAD_SECONDS} " if DOWNLOAD_SECONDS else ""
        print(f'  手动: {ff} -allowed_extensions ALL {t}-i "{m3u8}" -c copy "{OUTPUT_FILE}"')
        if sys.platform == "win32":
            input("\n  按 Enter 键退出...")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
