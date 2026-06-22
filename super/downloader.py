"""
视频下载模块

优先使用 yt-dlp，当 yt-dlp 无法处理时回退到直链下载。
"""

import os
import re
import logging
import time
from typing import Optional

import requests
import yt_dlp

import config
from config import HEADERS, TIMEOUT, DOWNLOAD_DIR, YDL_FORMAT
from extractors.base import VideoInfo

logger = logging.getLogger(__name__)


def _sanitize_filename(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", name).strip()


def get_ffmpeg_path() -> str | None:
    """
    按优先级查找 ffmpeg：
      1. config.FFMPEG_PATH（用户指定路径）
      2. 系统 PATH 中的 ffmpeg
      3. imageio_ffmpeg 内置版本
    """
    import shutil

    # 1. 用户指定路径
    if config.FFMPEG_PATH and os.path.isfile(config.FFMPEG_PATH):
        return config.FFMPEG_PATH

    # 2. 系统 PATH
    sys_ffmpeg = shutil.which("ffmpeg")
    if sys_ffmpeg:
        try:
            import subprocess
            r = subprocess.run([sys_ffmpeg, "-version"], capture_output=True, timeout=5)
            if r.returncode == 0:
                return sys_ffmpeg
        except Exception:
            pass

    # 3. imageio_ffmpeg 内置
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        pass

    return None


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
        print(f"\r  下载完成，正在合并...{' ' * 40}", flush=True)


def download_with_ytdlp(
    url: str,
    output_dir: str = DOWNLOAD_DIR,
    title: str = "video",
    extra_headers: Optional[dict] = None,
) -> Optional[str]:
    """
    使用 yt-dlp 下载视频。
    - 自动带入代理（修复代理环境下的 SSL: WRONG_VERSION_NUMBER）
    - 自动使用 config.FFMPEG_PATH 指定的 ffmpeg
    返回下载后的文件路径，失败返回 None。
    """
    os.makedirs(output_dir, exist_ok=True)
    safe_title = _sanitize_filename(title)
    output_tmpl = os.path.join(output_dir, f"{safe_title}.%(ext)s")

    http_headers = {**HEADERS}
    if extra_headers:
        http_headers.update(extra_headers)

    ffmpeg = get_ffmpeg_path()

    ydl_opts: dict = {
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
        # 修复代理环境 SSL 问题：SSL 握手通过代理后版本号可能不一致
        "nocheckcertificate": True,
        "postprocessors": [{
            "key": "FFmpegVideoConvertor",
            "preferedformat": "mp4",
        }],
    }

    # 代理：传给 yt-dlp，防止直连时 SSL WRONG_VERSION_NUMBER
    if config.PROXY:
        ydl_opts["proxy"] = config.PROXY

    # ffmpeg 路径
    if ffmpeg:
        ydl_opts["ffmpeg_location"] = os.path.dirname(ffmpeg)

    try:
        logger.info(f"[yt-dlp] 开始下载: {url}")
        if config.PROXY:
            logger.info(f"[yt-dlp] 使用代理: {config.PROXY}")
        if ffmpeg:
            logger.info(f"[yt-dlp] ffmpeg: {ffmpeg}")

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info:
                ext = info.get("ext", "mp4")
                mp4_path = os.path.join(output_dir, f"{safe_title}.mp4")
                actual_path = os.path.join(output_dir, f"{safe_title}.{ext}")
                if os.path.exists(mp4_path):
                    return mp4_path
                if os.path.exists(actual_path):
                    return actual_path
        return output_dir
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
    m3u8 转交 yt-dlp；mp4 用 requests 分块写入。
    """
    os.makedirs(output_dir, exist_ok=True)
    safe_title = _sanitize_filename(title)

    if video_info.is_m3u8:
        logger.info("[download_direct] m3u8 → 转交 yt-dlp")
        return download_with_ytdlp(
            video_info.url, output_dir, title, extra_headers=video_info.headers
        )

    output_path = os.path.join(output_dir, f"{safe_title}.mp4")
    headers = {**HEADERS, **video_info.headers}
    proxies = {"http": config.PROXY, "https": config.PROXY} if config.PROXY else None

    try:
        logger.info(f"[download_direct] 开始直链下载: {video_info.url[:80]}")
        with requests.get(video_info.url, headers=headers, proxies=proxies,
                          stream=True, timeout=TIMEOUT) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            start = time.time()

            with open(output_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1024 * 512):
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
