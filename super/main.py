"""
supjav.com 视频爬虫主入口

用法：
    python main.py <url>                          # 下载单个视频
    python main.py <url> --output /path/to/dir   # 指定下载目录
    python main.py <url> --debug                 # 开启调试日志

完整流程：
    1. 获取 supjav 视频详情页 HTML
    2. 提取所有嵌入视频源 URL（iframe/script/data 属性）
    3. 按优先级尝试每个视频源：
       a. 先用 yt-dlp 直接尝试（支持数百个站点）
       b. yt-dlp 失败 → 调用专用子提取器获取直链
       c. 子提取器成功 → 直链下载
    4. 任意一个源下载成功即停止
"""

import sys
import os
import argparse
import logging
from pathlib import Path

from page_parser import fetch_page, extract_embed_urls, get_page_title
from extractors import get_extractor
from downloader import download_with_ytdlp, download_direct
from config import DOWNLOAD_DIR, SUPPORTED_HOSTS


def setup_logging(debug: bool = False):
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )


def _is_supported_host(url: str) -> bool:
    return any(h in url for h in SUPPORTED_HOSTS)


def run(url: str, output_dir: str = DOWNLOAD_DIR, debug: bool = False) -> bool:
    """
    主下载流程。成功返回 True，失败返回 False。
    """
    setup_logging(debug)
    logger = logging.getLogger(__name__)

    os.makedirs(output_dir, exist_ok=True)

    print(f"\n{'='*60}")
    print(f"  目标页面: {url}")
    print(f"  下载目录: {output_dir}")
    print(f"{'='*60}\n")

    # ── Step 1: 获取页面 ─────────────────────────────────────────────────
    print("[1/4] 获取页面 HTML...")
    try:
        html = fetch_page(url)
    except RuntimeError as e:
        print(f"  ✗ 页面获取失败: {e}")
        return False

    title = get_page_title(html)
    print(f"  标题: {title}")

    # ── Step 2: 提取嵌入 URL ──────────────────────────────────────────────
    print("\n[2/4] 提取嵌入视频源...")
    embed_urls = extract_embed_urls(html, url)

    if not embed_urls:
        print("  ✗ 未发现任何视频嵌入 URL")
        return False

    # 过滤出已知托管平台，同时保留其他所有 URL 作为备选
    known = [u for u in embed_urls if _is_supported_host(u)]
    others = [u for u in embed_urls if not _is_supported_host(u)]
    ordered = known + others

    print(f"  发现 {len(ordered)} 个候选源（其中 {len(known)} 个已知平台）")
    for i, u in enumerate(ordered, 1):
        print(f"    [{i}] {u}")

    # ── Step 3 & 4: 逐一尝试 ─────────────────────────────────────────────
    print("\n[3/4] 逐一尝试下载...")

    for idx, embed_url in enumerate(ordered, 1):
        print(f"\n  ── 尝试源 [{idx}/{len(ordered)}]: {embed_url[:70]}...")

        # 策略 A: yt-dlp 直接尝试（很多站点 yt-dlp 原生支持）
        print("    策略 A: yt-dlp 直接下载...")
        result = download_with_ytdlp(embed_url, output_dir, title)
        if result:
            print(f"\n[4/4] ✓ 下载成功！\n  文件: {result}")
            return True

        # 策略 B: 专用子提取器 → 获取直链 → 下载
        print("    策略 B: 专用提取器...")
        extractor = get_extractor(embed_url)
        video_info = extractor.extract(embed_url)
        if video_info:
            print(f"    提取到直链: {video_info.url[:70]}...")
            result = download_direct(video_info, output_dir, title)
            if result:
                print(f"\n[4/4] ✓ 下载成功！\n  文件: {result}")
                return True

        print(f"    源 [{idx}] 失败，尝试下一个...")

    print("\n✗ 所有视频源均失败，请检查网络或更换代理。")
    return False


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="supjav.com 视频下载器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python main.py https://supjav.com/zh/132824.html
  python main.py https://supjav.com/zh/132824.html --output ~/Videos
  python main.py https://supjav.com/zh/132824.html --debug
        """,
    )
    parser.add_argument("url", help="supjav.com 视频页面 URL")
    parser.add_argument(
        "--output", "-o",
        default=DOWNLOAD_DIR,
        help=f"下载目录（默认: {DOWNLOAD_DIR}）",
    )
    parser.add_argument(
        "--debug", "-d",
        action="store_true",
        help="开启详细调试日志",
    )

    args = parser.parse_args()
    success = run(args.url, args.output, args.debug)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
