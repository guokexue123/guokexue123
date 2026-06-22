"""
supjav.com 爬虫配置文件
"""

import os

# ── 代理设置（可选，遇到地区封锁时填写） ─────────────────────────────────
# 格式示例：
#   PROXY = "http://127.0.0.1:7890"        # HTTP/HTTPS 代理（Clash 默认端口）
#   PROXY = "socks5://127.0.0.1:1080"      # SOCKS5 代理
#   PROXY = None                            # 不使用代理
PROXY = None

# ── 服务器偏好（优先选择哪个视频托管服务） ────────────────────────────────
# 可选值：
#   "DS"  → DoodStream（推荐，速度快）
#   "ST"  → Streamtape
#   "MD"  → Mixdrop
#   "FM"  → Filemoon
#   ""    → 不过滤，按页面顺序尝试所有服务器
SERVER_PREF = "DS"

# ── 本地 HTML 注入（跳过网络请求，直接解析本地保存的页面） ────────────────
# 使用方法：
#   1. 用浏览器打开目标 URL（如 https://supjav.com/zh/132824.html）
#   2. Ctrl+S 另存为"网页，仅 HTML"（.html 文件）
#   3. 把路径填到下面，脚本会直接解析本地文件，无需联网
# 不需要时设置为 None
LOCAL_HTML_FILE = None

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

# ── 服务器简称 → 域名关键词映射 ──────────────────────────────────────────
SERVER_HOSTS: dict[str, list[str]] = {
    "DS": ["doodstream", "dood."],
    "ST": ["streamtape"],
    "MD": ["mixdrop"],
    "FM": ["filemoon"],
}

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
