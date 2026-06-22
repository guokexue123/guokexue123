"""
supjav.com 页面解析器

负责：
1. 获取目标页面 HTML（多级反爬绕过）
2. 从 HTML 中提取所有视频源入口（iframe、script、data 属性）
3. 返回候选嵌入 URL 列表，供各子提取器处理

绕过策略（按优先级）：
  Level 1 — curl_cffi：模拟真实 Chrome TLS 指纹（JA3/JA4），
             这是破解 Cloudflare Bot Management 最有效的方法。
  Level 2 — cloudscraper：自动执行 Cloudflare JS 挑战（适合旧版 CF）。
  Level 3 — requests + 完整浏览器头：普通站点的最后兜底。
"""

import re
import json
import time
import logging
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from config import HEADERS, TIMEOUT, MAX_RETRIES, PROXY

logger = logging.getLogger(__name__)


# ─── Level 1: curl_cffi（TLS 指纹模拟） ──────────────────────────────────────

def _fetch_with_curl_cffi(url: str, referer: str = "") -> Optional[str]:
    """
    使用 curl_cffi 模拟 Chrome 120 的 TLS 握手特征。
    这能绕过 Cloudflare 的 JA3/JA4 指纹检测（最有效方案）。
    安装：pip install curl_cffi
    """
    try:
        from curl_cffi import requests as cffi_req

        headers = {**HEADERS}
        if referer:
            headers["Referer"] = referer

        # curl_cffi 用 proxy= 字符串（None 表示不使用代理）
        if PROXY:
            logger.info(f"[curl_cffi] 使用代理: {PROXY}")
        resp = cffi_req.get(
            url,
            headers=headers,
            impersonate="chrome120",
            timeout=TIMEOUT,
            allow_redirects=True,
            proxy=PROXY,
        )

        if resp.status_code == 200:
            logger.info(f"[curl_cffi] 成功 (HTTP 200): {url}")
            return resp.text
        else:
            logger.warning(f"[curl_cffi] HTTP {resp.status_code}")
            return None

    except ImportError:
        logger.debug("[curl_cffi] 未安装，跳过 (pip install curl_cffi)")
        return None
    except Exception as e:
        logger.warning(f"[curl_cffi] 异常: {e}")
        return None


# ─── Level 2: cloudscraper（JS 挑战执行） ────────────────────────────────────

def _fetch_with_cloudscraper(url: str, referer: str = "") -> Optional[str]:
    """
    使用 cloudscraper 自动执行 Cloudflare 的 JS 挑战（IUAM 模式）。
    安装：pip install cloudscraper
    """
    try:
        import cloudscraper

        scraper = cloudscraper.create_scraper(
            browser={
                "browser": "chrome",
                "platform": "windows",
                "mobile": False,
            },
            delay=5,
        )

        if referer:
            scraper.headers.update({"Referer": referer})

        if PROXY:
            logger.info(f"[cloudscraper] 使用代理: {PROXY}")
        proxies = {"http": PROXY, "https": PROXY} if PROXY else None
        resp = scraper.get(url, timeout=TIMEOUT, proxies=proxies)

        if resp.status_code == 200:
            logger.info(f"[cloudscraper] 成功 (HTTP 200): {url}")
            return resp.text
        else:
            logger.warning(f"[cloudscraper] HTTP {resp.status_code}")
            return None

    except ImportError:
        logger.debug("[cloudscraper] 未安装，跳过 (pip install cloudscraper)")
        return None
    except Exception as e:
        logger.warning(f"[cloudscraper] 异常: {e}")
        return None


# ─── Level 3: requests（普通浏览器头，兜底） ─────────────────────────────────

def _make_session(referer: str = "") -> requests.Session:
    """构建带持久 cookie 的 Session，模拟正常浏览行为"""
    session = requests.Session()
    session.headers.update(HEADERS)
    if referer:
        session.headers.update({"Referer": referer})
    if PROXY:
        session.proxies.update({"http": PROXY, "https": PROXY})
    # 先访问首页热身，获取基础 cookie
    try:
        session.get("https://supjav.com/", timeout=TIMEOUT)
        time.sleep(1.0)
    except Exception:
        pass
    return session


def _fetch_with_requests(url: str, referer: str = "") -> Optional[str]:
    """普通 requests 重试，作为最终兜底"""
    session = _make_session(referer)

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(url, timeout=TIMEOUT)
            resp.raise_for_status()
            logger.info(f"[requests] HTTP {resp.status_code}: {url}")
            return resp.text
        except requests.HTTPError as e:
            logger.warning(f"[requests] 第{attempt}次失败: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(2 ** attempt)
        except requests.RequestException as e:
            logger.warning(f"[requests] 请求异常: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(2 ** attempt)

    return None


# ─── 统一入口 ────────────────────────────────────────────────────────────────

def check_proxy() -> bool:
    """
    代理连通性自检：用 requests 通过代理访问一个简单的测试地址。
    返回 True 表示代理正常，False 表示代理不可用。
    """
    if not PROXY:
        return False
    test_url = "https://httpbin.org/ip"
    try:
        resp = requests.get(
            test_url,
            proxies={"http": PROXY, "https": PROXY},
            timeout=8,
        )
        if resp.status_code == 200:
            data = resp.json()
            logger.info(f"[proxy] 连通性正常，出口 IP: {data.get('origin', '?')}")
            return True
        logger.warning(f"[proxy] 测试返回 HTTP {resp.status_code}")
    except Exception as e:
        logger.warning(f"[proxy] 连通性测试失败: {e}")
    return False


def fetch_page(url: str, session=None) -> str:
    """
    获取页面 HTML，依次尝试三种方案：
      1. curl_cffi  →  TLS 指纹模拟（绕过 Cloudflare Bot Management）
      2. cloudscraper  →  JS 挑战执行（绕过 CF IUAM 模式）
      3. requests  →  普通浏览器头重试
    全部失败则抛出 RuntimeError。
    """
    parsed = urlparse(url)
    referer = f"{parsed.scheme}://{parsed.netloc}/"

    if PROXY:
        print(f"    代理已配置: {PROXY}")
        ok = check_proxy()
        if not ok:
            print(f"    ⚠ 代理连通性测试失败，请检查代理软件是否正在运行")
        else:
            print(f"    ✓ 代理连通正常")

    print("    尝试 Level 1: curl_cffi (TLS 指纹模拟)...")
    html = _fetch_with_curl_cffi(url, referer)
    if html and _is_real_page(html):
        return html

    print("    尝试 Level 2: cloudscraper (JS 挑战)...")
    html = _fetch_with_cloudscraper(url, referer)
    if html and _is_real_page(html):
        return html

    print("    尝试 Level 3: requests (普通浏览器头)...")
    html = _fetch_with_requests(url, referer)
    if html and _is_real_page(html):
        return html

    raise RuntimeError(f"无法获取页面: {url}")


def _is_real_page(html: str) -> bool:
    """
    检测获取到的页面是否是真实内容（而非 CF 挑战/错误页）。
    """
    if not html or len(html) < 500:
        return False
    cf_blocks = [
        "Just a moment",
        "cf-browser-verification",
        "Checking your browser",
        "DDoS protection by Cloudflare",
        "__cf_chl_",
        "challenge-running",
        "cf_clearance",
    ]
    for indicator in cf_blocks:
        if indicator in html:
            logger.warning(f"[_is_real_page] 检测到 Cloudflare 挑战页: {indicator!r}")
            return False
    return True


# ─────────────────────────────────────────────────────────────────────────────
#  从 HTML 中提取视频源的各种方法
# ─────────────────────────────────────────────────────────────────────────────

def _extract_iframes(soup: BeautifulSoup, base_url: str) -> list[str]:
    results = []
    for iframe in soup.find_all("iframe"):
        src = iframe.get("src") or iframe.get("data-src") or ""
        if src:
            results.append(urljoin(base_url, src.strip()))
    return results


def _extract_from_scripts(html: str, base_url: str) -> list[str]:
    """
    从 <script> 标签中提取视频 URL。
    supjav 常见模式：
      - var player_url = "https://..."
      - sources: [{file: "..."}]
      - post_id / video_id → AJAX 请求
    """
    results = []

    patterns = [
        r'(?:player_url|embed_url|video_url|src)\s*[=:]\s*["\']([^"\']+)["\']',
        r'"(?:file|src|source|url|link)"\s*:\s*"(https?://[^"]+\.(?:m3u8|mp4|flv)[^"]*)"',
        r"'(?:file|src|source|url|link)'\s*:\s*'(https?://[^']+\.(?:m3u8|mp4|flv)[^']*)'",
        r'(https?://(?:'
        + '|'.join([
            r'doodstream\.com', r'dood\.\w+', r'streamtape\.com',
            r'mixdrop\.\w+', r'filemoon\.\w+', r'voe\.sx',
            r'vidhide\.com', r'mp4upload\.com', r'upstream\.to',
            r'emturbovid\.com', r'embedrise\.com', r'vidplay\.\w+',
        ])
        + r')[^"\'<>\s]+)',
    ]

    for pat in patterns:
        for m in re.finditer(pat, html, re.IGNORECASE):
            u = m.group(1).strip()
            if u and u.startswith("http"):
                results.append(u)

    # 内联 JSON 块
    for block in re.findall(r'\{[^{}]{5,300}?"(?:url|src|file|embed)"[^{}]{0,300}?\}', html):
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
    results = []
    for tag in soup.find_all(True):
        for attr, val in tag.attrs.items():
            if not isinstance(val, str):
                continue
            if attr.startswith("data-") and val.startswith("http"):
                results.append(val.strip())
    return results


def _extract_video_tags(soup: BeautifulSoup, base_url: str) -> list[str]:
    results = []
    for tag in soup.find_all(["video", "source"]):
        src = tag.get("src") or tag.get("data-src") or ""
        if src:
            results.append(urljoin(base_url, src.strip()))
    return results


def _extract_m3u8_direct(html: str) -> list[str]:
    return re.findall(r'https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*', html)


def _extract_server_links(soup: BeautifulSoup, base_url: str) -> list[str]:
    """
    supjav 多服务器按钮：
    <a class="server active" data-url="https://doodstream.com/e/...">Server 1</a>
    """
    results = []
    for tag in soup.find_all(True, attrs={"class": re.compile(r"server|mirror|source|player", re.I)}):
        for attr in ("data-url", "data-src", "data-embed", "href"):
            val = tag.get(attr, "")
            if val and val.startswith("http"):
                results.append(val.strip())
    return results


def extract_embed_urls(html: str, page_url: str) -> list[str]:
    """综合提取页面中所有候选视频嵌入 URL，去重后返回。"""
    soup = BeautifulSoup(html, "lxml")
    seen: set[str] = set()
    results: list[str] = []

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
    soup = BeautifulSoup(html, "lxml")
    title = soup.title
    if title:
        return re.sub(r'[\\/:*?"<>|]', "_", title.text.strip())
    return "video"
