"""
视频下载模块

优先使用 yt-dlp（支持最广泛的视频站点），
当 yt-dlp 无法处理时回退到直链下载（requests + 进度条）。
"""

import os
import re
import sys
import logging
import time
from pathlib import Path
from typing import Optional

import requests
import yt_dlp

from config import HEADERS, TIMEOUT, DOWNLOAD_DIR, YDL_FORMAT
from extractors.base import VideoInfo

logger = logging.getLogger(__name__)


def _sanitize_filename(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", name).strip()


# ─── yt-dlp 下载 ─────────────────────────────────────────────────────────────

class _ProgressLogger:
    def debug(self, msg): pass
    def warning(self, msg): logger.warning(msg)
    def error(self, msg): logger.error(msg)


def _ytdlp_progress_hook(d):
    if d["status"] == "downloading":
        pct = d.get("_percent_str", "?").strip()
        speed = d.get("_speed_str", "?").strip()
        eta = d.get("_eta_str", "?").strip()
        print(f"\r  下载中: {pct}  速度: {speed}  剩余: {eta}    ", end="", flush=True)
    elif d["status"] == "finished":
        print(f"\r  下载完成，正在合并...{' ' * 40}")


def download_with_ytdlp(
    url: str,
    output_dir: str = DOWNLOAD_DIR,
    title: str = "video",
    extra_headers: Optional[dict] = None,
) -> Optional[str]:
    """
    使用 yt-dlp 下载视频。
    返回下载后的文件路径，失败返回 None。
    """
    os.makedirs(output_dir, exist_ok=True)
    safe_title = _sanitize_filename(title)
    output_tmpl = os.path.join(output_dir, f"{safe_title}.%(ext)s")

    http_headers = {**HEADERS}
    if extra_headers:
        http_headers.update(extra_headers)

    ydl_opts = {
        "format": YDL_FORMAT,
        "outtmpl": output_tmpl,
        "http_headers": http_headers,
        "logger": _ProgressLogger(),
        "progress_hooks": [_ytdlp_progress_hook],
        "quiet": True,
        "no_warnings": False,
        "retries": 5,
        "fragment_retries": 10,
        "concurrent_fragment_downloads": 4,
        "merge_output_format": "mp4",
        "postprocessors": [{
            "key": "FFmpegVideoConvertor",
            "preferedformat": "mp4",
        }],
    }

    try:
        logger.info(f"[yt-dlp] 开始下载: {url}")
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            # 找到实际文件名
            if info:
                ext = info.get("ext", "mp4")
                actual_path = os.path.join(output_dir, f"{safe_title}.{ext}")
                # 如果有合并为 mp4
                mp4_path = os.path.join(output_dir, f"{safe_title}.mp4")
                if os.path.exists(mp4_path):
                    return mp4_path
                if os.path.exists(actual_path):
                    return actual_path
        return output_dir  # 返回目录让调用者自己找
    except yt_dlp.utils.DownloadError as e:
        logger.warning(f"[yt-dlp] 下载失败: {e}")
        return None
    except Exception as e:
        logger.error(f"[yt-dlp] 异常: {e}")
        return None


# ─── 直链下载（requests 分块） ───────────────────────────────────────────────

def download_direct(
    video_info: VideoInfo,
    output_dir: str = DOWNLOAD_DIR,
    title: str = "video",
) -> Optional[str]:
    """
    直接用 requests 下载 mp4/m3u8 直链。
    m3u8 会优先尝试用 yt-dlp 处理；纯 mp4 直链用 requests 分块写入。
    """
    os.makedirs(output_dir, exist_ok=True)
    safe_title = _sanitize_filename(title)

    if video_info.is_m3u8:
        # m3u8 交给 yt-dlp 处理更稳定
        logger.info("[download_direct] m3u8 → 转交 yt-dlp")
        return download_with_ytdlp(
            video_info.url, output_dir, title, extra_headers=video_info.headers
        )

    output_path = os.path.join(output_dir, f"{safe_title}.mp4")
    headers = {**HEADERS, **video_info.headers}

    try:
        logger.info(f"[download_direct] 开始直链下载: {video_info.url[:80]}")
        with requests.get(video_info.url, headers=headers,
                          stream=True, timeout=TIMEOUT) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            start = time.time()

            with open(output_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1024 * 512):  # 512 KB
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total:
                            pct = downloaded / total * 100
                            speed = downloaded / (time.time() - start + 1e-9) / 1024
                            print(f"\r  {pct:.1f}%  {speed:.0f} KB/s    ",
                                  end="", flush=True)

        print()
        logger.info(f"[download_direct] 完成: {output_path}")
        return output_path

    except Exception as e:
        logger.error(f"[download_direct] 失败: {e}")
        if os.path.exists(output_path):
            os.remove(output_path)
        return None
