#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MissAV 视频下载器（Cloudflare Turnstile 绕过版）
依赖: pip install playwright requests m3u8 tqdm
      playwright install chromium
"""

import asyncio
import json
import os
import re
import shutil
import sys
import time
import subprocess
import threading
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from tqdm import tqdm

try:
    from playwright.async_api import async_playwright
except ImportError:
    print("请先安装: pip install playwright && playwright install chromium")
    sys.exit(1)

# ==================== 配置区 ====================
TARGET_URL = "https://18av.mm-cg.com/zh/chinese_content/8381/SUN-020.html"

# 直接粘贴你的 Cookie 字符串（从浏览器 DevTools → Network → 请求头复制）
# 格式: "key1=val1; key2=val2; ..."
COOKIE_STRING = ""  # ← 填入你的 cookie

OUTPUT_DIR = Path("downloads")
OUTPUT_DIR.mkdir(exist_ok=True)

# 并发分片下载线程数
CONCURRENT_DOWNLOADS = 16

# 是否使用有头浏览器（True=显示窗口，更容易过CF；False=无头）
HEADLESS = False

# ffmpeg 可执行文件路径（留空 "" 则自动从系统 PATH 查找）
FFMPEG_PATH = r"D:\Tool\ffmpeg\bin\ffmpeg"
# ================================================


def parse_cookie_string(cookie_str: str) -> list[dict]:
    """解析 cookie 字符串为 Playwright 格式"""
    cookies = []
    domain = urlparse(TARGET_URL).netloc
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" in part:
            name, _, value = part.partition("=")
            cookies.append({
                "name": name.strip(),
                "value": value.strip(),
                "domain": domain,
                "path": "/",
            })
    return cookies


async def wait_cf_clearance(page, timeout=60):
    """等待 Cloudflare 验证通过（iframe 消失）"""
    print("  ▶ 等待 Cloudflare 验证通过...")
    start = time.time()
    while time.time() - start < timeout:
        frames = page.frames
        cf_frames = [f for f in frames if "challenges.cloudflare.com" in f.url]
        if not cf_frames:
            print("  ✓ Cloudflare 验证已通过")
            return True
        elapsed = int(time.time() - start)
        if elapsed % 5 == 0:
            print(f"  ▶ 等待CF验证 {elapsed}s... (CF frames: {len(cf_frames)})")
        await asyncio.sleep(0.5)
    print("  ✗ CF验证超时，尝试继续...")
    return False


async def extract_m3u8_via_network(page, url):
    """方案A：拦截网络请求获取 m3u8 URL"""
    m3u8_urls = []

    def on_request(request):
        req_url = request.url
        if ".m3u8" in req_url and "challenges.cloudflare.com" not in req_url:
            m3u8_urls.append(req_url)
            print(f"  ✓ [网络拦截] 捕获 m3u8: {req_url[:80]}...")

    page.on("request", on_request)
    return m3u8_urls


async def extract_m3u8_via_dom(page):
    """方案B：从 DOM/JS 变量中提取 m3u8 URL"""
    candidates = []

    # 1. 从 <video> / <source> 标签
    try:
        src = await page.eval_on_selector("video source", "el => el.src")
        if src and ".m3u8" in src:
            candidates.append(src)
    except Exception:
        pass

    try:
        src = await page.eval_on_selector("video", "el => el.src")
        if src and ".m3u8" in src:
            candidates.append(src)
    except Exception:
        pass

    # 2. 从页面 HTML 正则匹配
    html = await page.content()
    patterns = [
        r'https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*',
        r'source:\s*["\']([^"\']+\.m3u8[^"\']*)["\']',
        r'file:\s*["\']([^"\']+\.m3u8[^"\']*)["\']',
        r'src:\s*["\']([^"\']+\.m3u8[^"\']*)["\']',
    ]
    for pat in patterns:
        found = re.findall(pat, html)
        candidates.extend(found)

    # 3. 从 window 全局变量
    try:
        js_vars = await page.evaluate("""
            () => {
                const results = [];
                // 常见播放器变量名
                const keys = ['videoUrl', 'video_url', 'streamUrl', 'hlsUrl',
                              'manifestUrl', 'source', 'file', 'src', 'url'];
                for (const k of keys) {
                    try {
                        const v = window[k];
                        if (v && typeof v === 'string' && v.includes('.m3u8')) {
                            results.push(v);
                        }
                    } catch(e) {}
                }
                // jwplayer
                try {
                    const jw = window.jwplayer && window.jwplayer();
                    if (jw) {
                        const pl = jw.getPlaylist();
                        if (pl && pl[0]) results.push(pl[0].file || pl[0].sources?.[0]?.file);
                    }
                } catch(e) {}
                // videojs
                try {
                    const vjs = document.querySelector('.video-js');
                    if (vjs && vjs.player) {
                        results.push(vjs.player.currentSrc());
                    }
                } catch(e) {}
                return results.filter(Boolean);
            }
        """)
        candidates.extend(js_vars)
    except Exception:
        pass

    # 去重
    seen = set()
    result = []
    for c in candidates:
        if c and c not in seen:
            seen.add(c)
            result.append(c)
    return result


async def trigger_play(page):
    """尝试触发视频播放，让播放器发出网络请求"""
    try:
        # 点击播放按钮
        for selector in [".play-button", ".vjs-big-play-button",
                          "[class*='play']", "video"]:
            try:
                el = await page.query_selector(selector)
                if el:
                    await el.click()
                    await asyncio.sleep(1)
                    break
            except Exception:
                pass

        # 尝试 JS 触发
        await page.evaluate("""
            () => {
                const v = document.querySelector('video');
                if (v) { v.muted = true; v.play(); }
            }
        """)
    except Exception:
        pass


async def get_m3u8_url():
    """主流程：启动浏览器 → 注入Cookie → 过CF → 提取m3u8"""
    async with async_playwright() as pw:
        # 使用 chromium，添加 stealth 参数
        browser = await pw.chromium.launch(
            headless=HEADLESS,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-web-security",
                "--disable-features=IsolateOrigins,site-per-process",
            ]
        )

        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
            java_script_enabled=True,
            # 伪装非无头浏览器
            extra_http_headers={
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            }
        )

        # 注入 stealth 脚本（隐藏 webdriver 特征）
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
            Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN','zh','en']});
            window.chrome = {runtime: {}};
        """)

        # 注入 Cookie
        if COOKIE_STRING.strip():
            cookies = parse_cookie_string(COOKIE_STRING)
            await context.add_cookies(cookies)
            print(f"  ✓ 已注入 {len(cookies)} 个 Cookie")
        else:
            print("  ⚠ 未配置 Cookie，以游客身份访问")

        page = await context.new_page()

        # 注册网络拦截
        m3u8_captured = []
        await extract_m3u8_via_network(page, TARGET_URL)

        def on_req(req):
            u = req.url
            if ".m3u8" in u and "cloudflare" not in u:
                m3u8_captured.append(u)
                print(f"  ✓ [网络] 捕获: {u[:100]}")

        page.on("request", on_req)

        # 加载页面
        print(f"\n  ▶ 加载页面: {TARGET_URL}")
        try:
            await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            print(f"  ⚠ 页面加载警告: {e}")

        # 等待 CF 验证
        await wait_cf_clearance(page, timeout=60)

        # 等待页面稳定
        await asyncio.sleep(3)

        # 触发播放
        print("  ▶ 触发视频播放...")
        await trigger_play(page)
        await asyncio.sleep(3)

        # 方案A：网络拦截结果
        if m3u8_captured:
            print(f"\n  ✓ 方案A（网络拦截）找到 {len(m3u8_captured)} 个 m3u8")
            # 优先选最长（通常是主 m3u8）
            best = max(m3u8_captured, key=len)
            await browser.close()
            return best

        # 方案B：DOM 提取
        print("  ▶ 方案B：从 DOM 提取 m3u8...")
        dom_results = await extract_m3u8_via_dom(page)
        if dom_results:
            print(f"  ✓ 方案B 找到: {dom_results[0][:100]}")
            await browser.close()
            return dom_results[0]

        # 方案C：截图诊断
        shot_path = OUTPUT_DIR / "debug_screenshot.png"
        await page.screenshot(path=str(shot_path))
        print(f"  ⚠ 未找到 m3u8，已截图保存到: {shot_path}")

        # 打印当前帧诊断
        for i, frame in enumerate(page.frames):
            print(f"  frame[{i}] url={frame.url}")

        await browser.close()
        return None


# ==================== HLS 下载 ====================

def _resolve_ffmpeg() -> str | None:
    """
    查找 ffmpeg 可执行文件。
    自动处理 Windows 下省略 .exe 扩展名的情况。
    """
    if FFMPEG_PATH:
        for candidate in [FFMPEG_PATH, FFMPEG_PATH + ".exe"]:
            if Path(candidate).exists():
                return candidate
        print(f"  ⚠ FFMPEG_PATH 路径不存在: {FFMPEG_PATH}")
    return shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")


def _ffmpeg_direct_download(m3u8_url: str, output: Path, ffmpeg_bin: str) -> bool:
    """
    ffmpeg 直接从 m3u8 URL 下载并封装为 mp4。
    优势：自动处理 AES-128 加密、多级 m3u8、各种编码。
    ffmpeg 进度直接输出到终端（无需隐藏）。
    """
    headers_arg = (
        f"Referer: {TARGET_URL}\r\n"
        "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36\r\n"
    )
    print(f"  ▶ ffmpeg 下载（自动处理 AES 加密）: {output.name}")
    result = subprocess.run([
        ffmpeg_bin, "-y",
        "-headers", headers_arg,
        "-i", m3u8_url,
        "-c", "copy",
        "-bsf:a", "aac_adtstoasc",
        str(output),
    ])  # 不捕获输出，ffmpeg 进度直接显示在终端
    return result.returncode == 0


def fetch_m3u8_segments(
    m3u8_url: str, headers: dict
) -> tuple[list[str], str, bytes | None, bytes | None]:
    """
    解析 m3u8，返回 (分片URL列表, 扩展名, AES密钥字节, IV字节)。
    同时处理主播放列表（多码率）和 AES-128 加密。
    """
    resp = requests.get(m3u8_url, headers=headers, timeout=15)
    resp.raise_for_status()
    content = resp.text
    base_url = m3u8_url.rsplit("/", 1)[0] + "/"

    lines = content.splitlines()

    # 检查是否是主 m3u8（含多码率 #EXT-X-STREAM-INF）
    sub_m3u8 = None
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and ".m3u8" in stripped:
            sub_m3u8 = stripped if stripped.startswith("http") else urljoin(base_url, stripped)
            break

    if sub_m3u8:
        print(f"  ▶ 主 m3u8 → 选择最高码率: {sub_m3u8}")
        return fetch_m3u8_segments(sub_m3u8, headers)

    # 解析 AES-128 加密信息（#EXT-X-KEY）
    key_bytes: bytes | None = None
    iv_bytes:  bytes | None = None
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("#EXT-X-KEY"):
            continue
        m_method = re.search(r'METHOD=([^,\s]+)', stripped)
        m_uri    = re.search(r'URI="([^"]+)"', stripped)
        m_iv     = re.search(r'IV=0x([0-9a-fA-F]+)', stripped)
        if m_method and m_method.group(1) == "AES-128" and m_uri:
            key_url = m_uri.group(1)
            if not key_url.startswith("http"):
                key_url = urljoin(base_url, key_url)
            key_bytes = requests.get(key_url, headers=headers, timeout=10).content
            print(f"  ℹ 检测到 AES-128 加密，已下载密钥")
            if m_iv:
                iv_bytes = bytes.fromhex(m_iv.group(1).zfill(32))

    # 提取分片 URL
    segments = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            seg_url = stripped if stripped.startswith("http") else urljoin(base_url, stripped)
            segments.append(seg_url)

    ext = ".ts"
    if segments and ".aac" in segments[0]:
        ext = ".aac"

    return segments, ext, key_bytes, iv_bytes


def _aes_decrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
    """AES-128-CBC 解密，优先用 pycryptodome，次选 cryptography。"""
    try:
        from Crypto.Cipher import AES
        return AES.new(key, AES.MODE_CBC, iv).decrypt(data)
    except ImportError:
        pass
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.backends import default_backend
        dec = Cipher(algorithms.AES(key), modes.CBC(iv),
                     backend=default_backend()).decryptor()
        return dec.update(data) + dec.finalize()
    except ImportError:
        pass
    raise RuntimeError(
        "流已 AES-128 加密，需安装解密库:\n"
        "  pip install pycryptodome\n"
        "或  pip install cryptography"
    )


def download_segment(args):
    """下载单个分片（支持 AES-128 解密）"""
    idx, url, path, headers, key_bytes, iv_bytes = args
    if path.exists():
        return idx, True

    try:
        r = requests.get(url, headers=headers, timeout=20, stream=True)
        r.raise_for_status()
        data = b"".join(r.iter_content(65536))

        if key_bytes:
            # IV 未指定时使用分片序号（零填充到 16 字节）
            iv = iv_bytes if iv_bytes else idx.to_bytes(16, "big")
            data = _aes_decrypt(data, key_bytes, iv)

        path.write_bytes(data)
        return idx, True
    except Exception:
        return idx, False


def download_hls(m3u8_url: str, output_name: str) -> Path:
    """
    下载 HLS 流并输出 mp4。

    策略：
      1. ffmpeg 可用 → 直接从 m3u8 URL 下载（ffmpeg 自动处理 AES 加密）
      2. ffmpeg 不可用 → Python 并发下载 + AES 解密 + 二进制拼接 → .ts
    """
    import concurrent.futures

    mp4_path = OUTPUT_DIR / f"{output_name}.mp4"
    ffmpeg_bin = _resolve_ffmpeg()

    # ── 路径1: ffmpeg 直接下载（推荐，自动处理 AES / 多级 m3u8）───────────────
    if ffmpeg_bin:
        print(f"\n  ▶ 合并分片 → {mp4_path}")
        print(f"  ✓ 找到 ffmpeg: {ffmpeg_bin}")
        if _ffmpeg_direct_download(m3u8_url, mp4_path, ffmpeg_bin):
            return mp4_path
        print("  ✗ ffmpeg 下载失败，切换到 Python 下载器...")

    # ── 路径2: Python 并发下载 + AES 解密 + 二进制拼接 ───────────────────────
    if not ffmpeg_bin:
        print("  ⚠ 未找到 ffmpeg，使用 Python 下载器")
        print("    安装 ffmpeg 可获得更好的兼容性: winget install ffmpeg")

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": TARGET_URL,
    }

    print(f"\n  ▶ 解析 m3u8: {m3u8_url[:80]}...")
    segments, ext, key_bytes, iv_bytes = fetch_m3u8_segments(m3u8_url, headers)
    total = len(segments)
    print(f"  ✓ 共 {total} 个分片，并发数: {CONCURRENT_DOWNLOADS}")

    tmp_dir = OUTPUT_DIR / f"{output_name}_segments"
    tmp_dir.mkdir(exist_ok=True)

    tasks = [
        (i, url, tmp_dir / f"{i:05d}{ext}", headers, key_bytes, iv_bytes)
        for i, url in enumerate(segments)
    ]

    failed = []
    with tqdm(total=total, desc="下载分片", unit="seg") as pbar:
        with concurrent.futures.ThreadPoolExecutor(max_workers=CONCURRENT_DOWNLOADS) as executor:
            futures = {executor.submit(download_segment, t): t[0] for t in tasks}
            for future in concurrent.futures.as_completed(futures):
                idx, ok = future.result()
                if not ok:
                    failed.append(idx)
                pbar.update(1)

    if failed:
        print(f"  ⚠ {len(failed)} 个分片失败，重试...")
        for t in [tasks[i] for i in failed]:
            idx, ok = download_segment(t)
            if not ok:
                print(f"  ✗ 分片 {idx} 最终失败")

    # 二进制拼接输出 .ts（MPEG-TS 容器，不能命名为 .mp4）
    ts_path = OUTPUT_DIR / f"{output_name}.ts"
    print(f"\n  ▶ 合并分片 → {ts_path}")
    with open(ts_path, "wb") as out:
        for i in range(total):
            seg = tmp_dir / f"{i:05d}{ext}"
            if seg.exists():
                out.write(seg.read_bytes())
    print(f"  ✓ 合并完成（MPEG-TS）: {ts_path}")
    print(f"  ℹ .ts 文件可用 VLC / mpv 直接播放")
    print(f'    转 mp4: {ffmpeg_bin or "ffmpeg"} -i "{ts_path}" -c copy "{mp4_path}"')
    output_path: Path = ts_path

    shutil.rmtree(tmp_dir, ignore_errors=True)
    return output_path


# ==================== 主程序 ====================

async def main():
    print("=" * 60)
    print("  MissAV 视频下载器 (CF Turnstile 绕过版)")
    print("=" * 60)

    # Step 1: 提取 m3u8
    m3u8_url = await get_m3u8_url()

    if not m3u8_url:
        print("\n  ✗ 无法提取 m3u8 URL")
        print("  建议：")
        print("  1. 设置 HEADLESS=False 手动通过 CF 验证")
        print("  2. 检查并更新 COOKIE_STRING")
        print("  3. 查看 downloads/debug_screenshot.png 截图")
        return

    print(f"\n  ✓ m3u8 URL: {m3u8_url}")

    # Step 2: 下载
    video_name = TARGET_URL.rstrip("/").split("/")[-1]
    output = download_hls(m3u8_url, video_name)
    print(f"\n  完成: {output}")


if __name__ == "__main__":
    asyncio.run(main())
