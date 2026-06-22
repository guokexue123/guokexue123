"""
supjav.com / 123av.com 爬虫配置文件
"""

import os

# ── 代理设置 ───────────────────────────────────────────────────────────────
#   PROXY = "http://127.0.0.1:7890"        # HTTP/HTTPS 代理（Clash 默认端口）
#   PROXY = "socks5://127.0.0.1:1080"      # SOCKS5 代理
#   PROXY = None                            # 不使用代理
PROXY = None

# ── ffmpeg 可执行文件路径 ──────────────────────────────────────────────────
# 填写本地 ffmpeg 路径后，截图和 HLS 合并都会使用指定版本
# Windows 示例： FFMPEG_PATH = r"D:\Tool\ffmpeg\bin\ffmpeg.exe"
# None 表示自动查找（依次尝试系统 PATH → imageio_ffmpeg 内置版本）
FFMPEG_PATH = r"D:\Tool\ffmpeg\bin\ffmpeg.exe"

# ── 服务器偏好（优先选择哪个视频托管服务） ────────────────────────────────
#   "DS"  → DoodStream
#   "ST"  → Streamtape
#   "MD"  → Mixdrop
#   "FM"  → Filemoon
#   "SR"  → Surrit（123av 专用）
#   ""    → 不过滤，按页面顺序尝试所有服务器
SERVER_PREF = "DS"

# ── 本地 HTML 注入（跳过网络请求，直接解析本地保存的页面） ────────────────
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

# ── yt-dlp 下载质量偏好 ───────────────────────────────────────────────────
YDL_FORMAT = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best[ext=mp4]/best"

# ── 服务器简称 → 域名关键词映射 ──────────────────────────────────────────
SERVER_HOSTS: dict[str, list[str]] = {
    "DS": ["doodstream", "dood."],
    "ST": ["streamtape"],
    "MD": ["mixdrop"],
    "FM": ["filemoon"],
    "SR": ["surrit."],
}

# ── 支持的视频托管平台 ────────────────────────────────────────────────────
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
    "surrit.store",
    "surrit.com",
    "ok.ru",
]

# ── 非视频域名黑名单（从提取结果中排除） ─────────────────────────────────
NON_VIDEO_DOMAINS = [
    "googletagmanager.com",
    "google-analytics.com",
    "cloudflareinsights.com",
    "cloudflare.com/cdn",
    "wingsmob.com",
    "magsrv.com",
    "exoclick.com",
    "trafficjunky.net",
    "juicyads.com",
    "a-ads.com",
    "adnxs.com",
    "doubleclick.net",
    "amazon-adsystem.com",
    "scorecardresearch.com",
]

