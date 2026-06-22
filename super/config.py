"""
supjav.com 爬虫配置文件
"""

import os

# ── 请求头：模拟 Chrome 120 浏览器 ──────────────────────────────────────
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}

# ── 下载目录 ──────────────────────────────────────────────────────────────
DOWNLOAD_DIR = os.path.join(os.path.dirname(__file__), "downloads")

# ── 请求超时（秒） ────────────────────────────────────────────────────────
TIMEOUT = 30

# ── 重试次数 ──────────────────────────────────────────────────────────────
MAX_RETRIES = 3

# ── yt-dlp 下载质量偏好（按优先级） ─────────────────────────────────────
YDL_FORMAT = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best[ext=mp4]/best"

# ── 支持的视频托管平台（supjav 常用） ────────────────────────────────────
SUPPORTED_HOSTS = [
    "doodstream.com",
    "dood.watch",
    "dood.la",
    "dood.cx",
    "streamtape.com",
    "mixdrop.co",
    "mixdrop.bz",
    "filemoon.sx",
    "filemoon.in",
    "voe.sx",
    "vidhide.com",
    "streamvid.net",
    "vidplay.online",
    "mp4upload.com",
    "upstream.to",
    "emturbovid.com",
    "embedrise.com",
    "okru",
    "ok.ru",
]
