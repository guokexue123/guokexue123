"""
M3U8 / HLS 流下载器

supjav 部分视频源使用 HLS（HTTP Live Streaming）格式，
即 .m3u8 分片索引文件。本模块负责：
  1. 解析 m3u8 文件（含 master playlist → media playlist）
  2. 下载所有 .ts 分片（支持并发）
  3. 合并为单个 mp4 文件
"""

import os
import re
import time
import logging
import threading
import subprocess
from typing import Optional
from urllib.parse import urljoin, urlparse
from queue import Queue, Empty
from pathlib import Path

import requests

from config import HEADERS, TIMEOUT

logger = logging.getLogger(__name__)


def _parse_m3u8(content: str, base_url: str) -> tuple[list[str], bool]:
    """
    解析 m3u8 内容，返回 (片段URL列表, 是否为master)。
    master playlist 只包含媒体播放列表引用，需进一步解析。
    """
    lines = content.strip().splitlines()
    is_master = False
    segments = []

    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            if "#EXT-X-STREAM-INF" in line:
                is_master = True
            continue
        # 相对路径转绝对路径
        seg_url = urljoin(base_url, line)
        segments.append(seg_url)

    return segments, is_master


def fetch_m3u8_segments(
    m3u8_url: str,
    extra_headers: Optional[dict] = None,
    prefer_quality: str = "best",
) -> list[str]:
    """
    获取 m3u8 的所有 ts 分片 URL 列表。
    自动处理 master playlist（选择最高质量流）。
    """
    headers = {**HEADERS, **(extra_headers or {})}
    session = requests.Session()
    session.headers.update(headers)

    try:
        resp = session.get(m3u8_url, timeout=TIMEOUT)
        resp.raise_for_status()
        content = resp.text
    except Exception as e:
        logger.error(f"[m3u8] 获取 m3u8 失败: {e}")
        return []

    segments, is_master = _parse_m3u8(content, m3u8_url)

    if is_master:
        # 选择最后一个（通常是最高质量）
        if not segments:
            return []
        chosen = segments[-1] if prefer_quality == "best" else segments[0]
        logger.info(f"[m3u8] master playlist → 选择: {chosen}")
        try:
            resp2 = session.get(chosen, timeout=TIMEOUT)
            resp2.raise_for_status()
            media_segs, _ = _parse_m3u8(resp2.text, chosen)
            return media_segs
        except Exception as e:
            logger.error(f"[m3u8] 获取媒体 playlist 失败: {e}")
            return []

    return segments


def download_m3u8(
    m3u8_url: str,
    output_path: str,
    extra_headers: Optional[dict] = None,
    workers: int = 4,
) -> bool:
    """
    下载 m3u8 视频到 output_path（.mp4 格式）。
    优先使用 ffmpeg 直接拉流（最稳定），
    ffmpeg 不可用时回退到手动分片下载+合并。
    """
    # 方法1：ffmpeg 直接处理 m3u8（最佳方案）
    if _ffmpeg_available():
        return _download_with_ffmpeg(m3u8_url, output_path, extra_headers)

    # 方法2：手动分片下载
    return _download_manual(m3u8_url, output_path, extra_headers, workers)


def _ffmpeg_available() -> bool:
    try:
        result = subprocess.run(
            ["ffmpeg", "-version"],
            capture_output=True, timeout=5
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _download_with_ffmpeg(
    m3u8_url: str,
    output_path: str,
    extra_headers: Optional[dict] = None,
) -> bool:
    """使用 ffmpeg 下载并合并 HLS 流"""
    headers = {**HEADERS, **(extra_headers or {})}
    ua = headers.get("User-Agent", "")
    referer = headers.get("Referer", "")

    cmd = [
        "ffmpeg", "-y",
        "-user_agent", ua,
    ]
    if referer:
        cmd += ["-headers", f"Referer: {referer}\r\n"]
    cmd += [
        "-i", m3u8_url,
        "-c", "copy",
        "-bsf:a", "aac_adtstoasc",
        output_path,
    ]

    logger.info(f"[ffmpeg] 开始下载: {m3u8_url}")
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=3600,  # 1小时超时
        )
        if proc.returncode == 0:
            logger.info(f"[ffmpeg] 完成: {output_path}")
            return True
        else:
            logger.error(f"[ffmpeg] 失败: {proc.stderr.decode('utf-8', errors='replace')[-500:]}")
            return False
    except subprocess.TimeoutExpired:
        logger.error("[ffmpeg] 超时")
        return False
    except Exception as e:
        logger.error(f"[ffmpeg] 异常: {e}")
        return False


def _download_manual(
    m3u8_url: str,
    output_path: str,
    extra_headers: Optional[dict] = None,
    workers: int = 4,
) -> bool:
    """手动分片下载后合并"""
    headers = {**HEADERS, **(extra_headers or {})}
    segments = fetch_m3u8_segments(m3u8_url, headers)

    if not segments:
        logger.error("[m3u8_manual] 无分片可下载")
        return False

    total = len(segments)
    logger.info(f"[m3u8_manual] 共 {total} 个分片，开始并发下载...")

    ts_dir = output_path + "_ts_segments"
    os.makedirs(ts_dir, exist_ok=True)

    downloaded = [None] * total
    lock = threading.Lock()
    counter = [0]

    def worker(queue: Queue):
        session = requests.Session()
        session.headers.update(headers)
        while True:
            try:
                idx, seg_url = queue.get(timeout=3)
            except Empty:
                break
            ts_path = os.path.join(ts_dir, f"{idx:06d}.ts")
            for attempt in range(3):
                try:
                    resp = session.get(seg_url, timeout=30)
                    resp.raise_for_status()
                    with open(ts_path, "wb") as f:
                        f.write(resp.content)
                    with lock:
                        downloaded[idx] = ts_path
                        counter[0] += 1
                        pct = counter[0] / total * 100
                        print(f"\r  分片 {counter[0]}/{total} ({pct:.1f}%)    ",
                              end="", flush=True)
                    break
                except Exception as e:
                    if attempt == 2:
                        logger.warning(f"[m3u8_manual] 分片 {idx} 下载失败: {e}")
            queue.task_done()

    q = Queue()
    for i, seg in enumerate(segments):
        q.put((i, seg))

    threads = [threading.Thread(target=worker, args=(q,), daemon=True)
               for _ in range(min(workers, total))]
    for t in threads:
        t.start()
    q.join()
    print()

    # 合并 ts 文件
    logger.info("[m3u8_manual] 合并分片...")
    try:
        with open(output_path, "wb") as out:
            for ts_path in downloaded:
                if ts_path and os.path.exists(ts_path):
                    with open(ts_path, "rb") as f:
                        out.write(f.read())
        logger.info(f"[m3u8_manual] 合并完成: {output_path}")

        # 清理临时目录
        import shutil
        shutil.rmtree(ts_dir, ignore_errors=True)
        return True
    except Exception as e:
        logger.error(f"[m3u8_manual] 合并失败: {e}")
        return False
