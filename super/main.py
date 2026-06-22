"""
supjav.com 视频爬虫主入口

完整流程：
    1. 获取 supjav 视频详情页 HTML（支持本地文件注入）
    2. 提取嵌入视频源，按 SERVER_PREF 过滤/优先排序
    3. 逐一尝试：yt-dlp 直接下载 → 专用提取器 + 直链下载
    4. 下载成功后用 ffmpeg 截取第 10 秒画面
"""

import sys
import os
import subprocess
import logging

from page_parser import fetch_page, extract_embed_urls, get_page_title
from extractors import get_extractor
from downloader import download_with_ytdlp, download_direct
import config
from config import DOWNLOAD_DIR, SUPPORTED_HOSTS

# ── 目标 URL（直接修改这里） ───────────────────────────────────────────────
TARGET_URL = "https://supjav.com/zh/132824.html"
# ──────────────────────────────────────────────────────────────────────────


def setup_logging(debug: bool = False):
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )


def _is_supported_host(url: str) -> bool:
    return any(h in url for h in SUPPORTED_HOSTS)


def _matches_pref(url: str, pref: str) -> bool:
    if not pref:
        return True
    keywords = config.SERVER_HOSTS.get(pref.upper(), [])
    return any(kw in url.lower() for kw in keywords)


def _sort_by_pref(urls: list[str], pref: str) -> list[str]:
    if not pref:
        return urls
    preferred = [u for u in urls if _matches_pref(u, pref)]
    others = [u for u in urls if not _matches_pref(u, pref)]
    return preferred + others


def _get_ffmpeg() -> str:
    """查找可用的 ffmpeg 可执行文件路径（优先系统 ffmpeg，备用 imageio_ffmpeg）"""
    import shutil
    sys_ffmpeg = shutil.which("ffmpeg")
    if sys_ffmpeg:
        # 验证系统 ffmpeg 能正常启动（可能有依赖缺失）
        try:
            r = subprocess.run([sys_ffmpeg, "-version"], capture_output=True, timeout=5)
            if r.returncode == 0:
                return sys_ffmpeg
        except Exception:
            pass
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        pass
    raise FileNotFoundError("找不到 ffmpeg，请安装: pip install imageio[ffmpeg]")


def screenshot_at_10s(video_path: str) -> str | None:
    """
    用 ffmpeg 截取视频第 10 秒的画面，保存为 jpg。
    自动检测系统 ffmpeg 或 imageio_ffmpeg 内置版本。
    返回截图路径，失败返回 None。
    """
    screenshot_path = video_path.rsplit(".", 1)[0] + "_10s.jpg"
    try:
        ffmpeg = _get_ffmpeg()
    except FileNotFoundError as e:
        logging.getLogger(__name__).warning(f"[screenshot] {e}")
        return None

    cmd = [
        ffmpeg, "-y",
        "-ss", "10",
        "-i", video_path,
        "-frames:v", "1",
        "-update", "1",
        "-q:v", "2",
        screenshot_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=30)
        if result.returncode == 0 and os.path.exists(screenshot_path):
            return screenshot_path
        err = result.stderr.decode("utf-8", errors="replace")[-400:]
        logging.getLogger(__name__).warning(f"[screenshot] ffmpeg 错误: {err}")
    except Exception as e:
        logging.getLogger(__name__).warning(f"[screenshot] 异常: {e}")
    return None


def _load_html(url: str) -> str:
    """
    获取页面 HTML。运行时读取 config.LOCAL_HTML_FILE，
    保证用户修改 config.py 后无需重新导入即可生效。
    """
    local = config.LOCAL_HTML_FILE
    if local and os.path.exists(local):
        print(f"  [本地模式] 读取: {local}")
        with open(local, encoding="utf-8", errors="replace") as f:
            return f.read()
    return fetch_page(url)


def run(url: str, output_dir: str = DOWNLOAD_DIR, debug: bool = False) -> bool:
    """主下载流程，成功返回 True。"""
    setup_logging(debug)

    os.makedirs(output_dir, exist_ok=True)

    pref_label = f"偏好服务器: {config.SERVER_PREF}" if config.SERVER_PREF else "偏好服务器: 全部"
    print(f"\n{'='*60}")
    print(f"  目标页面: {url}")
    print(f"  下载目录: {output_dir}")
    print(f"  {pref_label}")
    print(f"{'='*60}\n")

    # ── Step 1: 获取页面 HTML ─────────────────────────────────────────────
    print("[1/5] 获取页面 HTML...")
    try:
        html = _load_html(url)
    except RuntimeError as e:
        print(f"  ✗ 页面获取失败: {e}")
        _print_tips()
        return False

    title = get_page_title(html)
    print(f"  标题: {title}")

    # ── Step 2: 提取嵌入 URL ──────────────────────────────────────────────
    print("\n[2/5] 提取嵌入视频源...")
    embed_urls = extract_embed_urls(html, url)

    if not embed_urls:
        print("  ✗ 未发现任何视频嵌入 URL")
        return False

    # 已知平台优先，再按 SERVER_PREF 排序
    known = [u for u in embed_urls if _is_supported_host(u)]
    others = [u for u in embed_urls if not _is_supported_host(u)]
    ordered = _sort_by_pref(known, config.SERVER_PREF) + others

    print(f"  发现 {len(ordered)} 个候选源")
    if config.SERVER_PREF:
        pref_count = sum(1 for u in ordered if _matches_pref(u, config.SERVER_PREF))
        print(f"  其中 {pref_count} 个匹配偏好 [{config.SERVER_PREF}]")
    for i, u in enumerate(ordered, 1):
        tag = f" ← [{config.SERVER_PREF}]" if _matches_pref(u, config.SERVER_PREF) and config.SERVER_PREF else ""
        print(f"    [{i}] {u}{tag}")

    # ── Step 3 & 4: 逐一尝试下载 ─────────────────────────────────────────
    print("\n[3/5] 逐一尝试下载...")
    result_path = None

    for idx, embed_url in enumerate(ordered, 1):
        print(f"\n  ── 源 [{idx}/{len(ordered)}]: {embed_url[:70]}...")

        print("    策略 A: yt-dlp 直接下载...")
        result_path = download_with_ytdlp(embed_url, output_dir, title)
        if result_path:
            break

        print("    策略 B: 专用提取器 + 直链下载...")
        extractor = get_extractor(embed_url)
        video_info = extractor.extract(embed_url)
        if video_info:
            print(f"    提取到直链: {video_info.url[:70]}...")
            result_path = download_direct(video_info, output_dir, title)
            if result_path:
                break

        print(f"    源 [{idx}] 失败，继续...")

    if not result_path:
        print("\n✗ 所有视频源均失败，请检查网络或更换代理。")
        _print_tips()
        return False

    print(f"\n[4/5] ✓ 下载成功！\n  文件: {result_path}")

    # ── Step 5: 截取第 10 秒截图 ──────────────────────────────────────────
    print("\n[5/5] 截取第 10 秒画面...")
    screenshot = screenshot_at_10s(result_path)
    if screenshot:
        print(f"  ✓ 截图已保存: {screenshot}")
        _show_screenshot(screenshot)
    else:
        print("  ✗ 截图失败（视频时长可能不足 10 秒，或 ffmpeg 不可用）")

    return True


def _print_tips():
    from config import PROXY, LOCAL_HTML_FILE
    print()
    print("  ── 解决 403 / 访问被拒绝 " + "─" * 34)
    if PROXY:
        # 代理已设，给出调试建议
        print(f"  代理已配置: {PROXY}")
        print("  代理仍然 403，可能原因：")
        print("    1. 代理软件未运行（请确认端口可用）")
        print("    2. 代理 IP 也被目标站封锁（换节点试试）")
        print("    3. SOCKS5 代理需改为 HTTP 代理格式")
        print()
        print("  快速验证代理：")
        print(f"    curl -x {PROXY} https://supjav.com/zh/132824.html -I")
    else:
        print("  方法 1（最简单）: 用浏览器打开目标页面 → Ctrl+S 另存为 HTML")
        print("    然后在 config.py 设置:")
        print('      LOCAL_HTML_FILE = r"C:\\path\\to\\132824.html"')
        print()
        print("  方法 2: 配置代理，在 config.py 设置:")
        print('      PROXY = "http://127.0.0.1:7890"   # Clash HTTP 端口')
        print('      PROXY = "socks5://127.0.0.1:7891"  # Clash SOCKS5 端口')
    if not LOCAL_HTML_FILE:
        print()
        print("  ✓ LOCAL_HTML_FILE 方式完全绕过网络问题，最推荐")
    print("  " + "─" * 54)


def _show_screenshot(path: str):
    """在终端打印截图路径，并尝试用 ASCII/ANSI 显示（如环境支持）"""
    print(f"\n  {'─'*50}")
    print(f"  截图路径: {path}")
    # 尝试用 Pillow 打印图片基本信息
    try:
        from PIL import Image
        img = Image.open(path)
        w, h = img.size
        print(f"  尺寸: {w} × {h} px")
        print(f"  {'─'*50}")
    except ImportError:
        pass


if __name__ == "__main__":
    success = run(TARGET_URL, DOWNLOAD_DIR)
    sys.exit(0 if success else 1)
