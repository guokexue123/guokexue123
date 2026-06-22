"""
supjav.com 页面解析器

负责：
1. 获取目标页面 HTML（含反爬绕过）
2. 从 HTML 中提取所有视频源入口（iframe、script、data 属性）
3. 返回候选嵌入 URL 列表，供各子提取器处理
"""

import re
import json
import time
import logging
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from config import HEADERS, TIMEOUT, MAX_RETRIES

logger = logging.getLogger(__name__)


def _make_session() -> requests.Session:
    """构建带持久 cookie 的 Session，模拟正常浏览行为"""
    session = requests.Session()
    session.headers.update(HEADERS)
    # 先访问首页，获取 cookie（部分站点检查 Referer + cookie 一致性）
    try:
        session.get("https://supjav.com/", timeout=TIMEOUT)
        time.sleep(0.8)
    except Exception:
        pass
    return session


def fetch_page(url: str, session: Optional[requests.Session] = None) -> str:
    """
    获取页面 HTML。
    - 自动带 Referer
    - 失败自动重试
    """
    if session is None:
        session = _make_session()

    parsed = urlparse(url)
    referer = f"{parsed.scheme}://{parsed.netloc}/"
    headers = {"Referer": referer}

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(url, headers=headers, timeout=TIMEOUT)
            resp.raise_for_status()
            logger.info(f"[fetch_page] HTTP {resp.status_code}  {url}")
            return resp.text
        except requests.HTTPError as e:
            logger.warning(f"[fetch_page] 第{attempt}次失败: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(2 ** attempt)
        except requests.RequestException as e:
            logger.warning(f"[fetch_page] 请求异常: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(2 ** attempt)

    raise RuntimeError(f"无法获取页面: {url}")


# ─────────────────────────────────────────────────────────────────────────────
#  以下是从 HTML 中提取视频源的各种方法
# ─────────────────────────────────────────────────────────────────────────────

def _extract_iframes(soup: BeautifulSoup, base_url: str) -> list[str]:
    """提取所有 iframe src"""
    results = []
    for iframe in soup.find_all("iframe"):
        src = iframe.get("src") or iframe.get("data-src") or ""
        if src:
            src = urljoin(base_url, src.strip())
            results.append(src)
    return results


def _extract_from_scripts(html: str, base_url: str) -> list[str]:
    """
    从 <script> 标签中提取视频 URL。
    supjav 常见模式：
      - var player_url = "https://..."
      - sources: [{file: "..."}]
      - {"url": "https://..."}
      - 直接出现 m3u8/mp4 链接
    """
    results = []

    # 1. 匹配常见播放器变量赋值
    patterns = [
        r'(?:player_url|embed_url|video_url|src)\s*[=:]\s*["\']([^"\']+)["\']',
        r'"(?:file|src|source|url|link)"\s*:\s*"(https?://[^"]+\.(?:m3u8|mp4|flv)[^"]*)"',
        r"'(?:file|src|source|url|link)'\s*:\s*'(https?://[^']+\.(?:m3u8|mp4|flv)[^']*)'",
        r'(https?://(?:' + '|'.join([
            r'doodstream\.com', r'dood\.\w+', r'streamtape\.com',
            r'mixdrop\.\w+', r'filemoon\.\w+', r'voe\.sx',
            r'vidhide\.com', r'mp4upload\.com', r'upstream\.to',
        ]) + r')[^"\'<>\s]+)',
    ]

    for pat in patterns:
        for m in re.finditer(pat, html, re.IGNORECASE):
            url = m.group(1).strip()
            if url and url.startswith("http"):
                results.append(url)

    # 2. 提取内联 JSON（wp_ajax / REST 常见模式）
    json_blocks = re.findall(r'\{[^{}]*?"(?:url|src|file|embed)"[^{}]*?\}', html)
    for block in json_blocks:
        try:
            data = json.loads(block)
            for key in ("url", "src", "file", "embed"):
                if key in data and isinstance(data[key], str):
                    u = data[key].strip()
                    if u.startswith("http"):
                        results.append(u)
        except json.JSONDecodeError:
            pass

    return results


def _extract_data_attrs(soup: BeautifulSoup, base_url: str) -> list[str]:
    """
    提取 data-* 属性中的视频 URL。
    supjav 有时把嵌入 URL 写在按钮/div 的 data-url、data-src 等属性里。
    """
    results = []
    for tag in soup.find_all(True):
        for attr, val in tag.attrs.items():
            if not isinstance(val, str):
                continue
            if attr.startswith("data-") and val.startswith("http"):
                results.append(val.strip())
    return results


def _extract_video_tags(soup: BeautifulSoup, base_url: str) -> list[str]:
    """提取 <video> 和 <source> 标签的 src"""
    results = []
    for tag in soup.find_all(["video", "source"]):
        src = tag.get("src") or tag.get("data-src") or ""
        if src:
            results.append(urljoin(base_url, src.strip()))
    return results


def _extract_m3u8_direct(html: str) -> list[str]:
    """直接从 HTML 文本中提取 m3u8 链接"""
    return re.findall(r'https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*', html)


def _extract_server_links(soup: BeautifulSoup, base_url: str) -> list[str]:
    """
    supjav 的多服务器选项通常是带 data-url / href 的 <a> 或 <li>，
    例如 <a class="server" data-url="https://doodstream.com/e/...">Server 1</a>
    """
    results = []
    selectors = [
        {"class": re.compile(r"server|mirror|source|player", re.I)},
    ]
    for sel in selectors:
        for tag in soup.find_all(True, attrs=sel):
            for attr in ("data-url", "data-src", "data-embed", "href"):
                val = tag.get(attr, "")
                if val and val.startswith("http"):
                    results.append(val.strip())
    return results


# ─────────────────────────────────────────────────────────────────────────────

def extract_embed_urls(html: str, page_url: str) -> list[str]:
    """
    综合提取页面中所有候选视频嵌入 URL，去重后返回。
    """
    soup = BeautifulSoup(html, "lxml")
    seen = set()
    results = []

    sources = (
        _extract_iframes(soup, page_url)
        + _extract_from_scripts(html, page_url)
        + _extract_data_attrs(soup, page_url)
        + _extract_video_tags(soup, page_url)
        + _extract_m3u8_direct(html)
        + _extract_server_links(soup, page_url)
    )

    for url in sources:
        url = url.strip().rstrip("/")
        if url and url not in seen:
            seen.add(url)
            results.append(url)

    logger.info(f"[extract_embed_urls] 共发现 {len(results)} 个候选 URL")
    return results


def get_page_title(html: str) -> str:
    """提取页面标题，用于命名下载文件"""
    soup = BeautifulSoup(html, "lxml")
    title = soup.title
    if title:
        return re.sub(r'[\\/:*?"<>|]', "_", title.text.strip())
    return "video"
