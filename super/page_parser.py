"""
supjav.com 页面解析器

绕过策略（按优先级）：
  Level 0 — DrissionPage：驱动真实 Chrome，能执行 Cloudflare Managed Challenge/Turnstile JS 挑战。
  Level 1 — curl_cffi：模拟 Chrome TLS 指纹（JA3/JA4），绕过 Bot Management。
  Level 2 — cloudscraper：执行旧版 CF IUAM JS 挑战。
  Level 3 — requests + 完整浏览器头：普通站点兜底。
"""

import re
import json
import time
import socket
import logging
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from config import HEADERS, TIMEOUT, MAX_RETRIES, PROXY, NON_VIDEO_DOMAINS

logger = logging.getLogger(__name__)


# ─── 代理连通性检测（TCP socket，不依赖第三方服务） ───────────────────────────

def check_proxy() -> bool:
    """
    通过 TCP socket 测试代理端口是否可达。
    不依赖 httpbin.org 等第三方服务，更可靠。
    """
    if not PROXY:
        return False
    parsed = urlparse(PROXY)
    host = parsed.hostname
    port = parsed.port
    if not host or not port:
        logger.warning(f"[proxy] 无法解析代理地址: {PROXY}")
        return False
    try:
        sock = socket.create_connection((host, port), timeout=5)
        sock.close()
        logger.info(f"[proxy] 端口 {host}:{port} 连通正常")
        return True
    except OSError as e:
        logger.warning(f"[proxy] 端口连接失败: {e}")
        return False


# ─── Level 0: DrissionPage（真实 Chrome，处理 CF JS 挑战） ────────────────────

def _fetch_with_drissionpage(url: str, referer: str = "") -> Optional[str]:
    """
    驱动系统已安装的 Chrome 浏览器，能完整执行 Cloudflare
    Managed Challenge / Turnstile JS 挑战。
    安装：pip install DrissionPage
    """
    try:
        from DrissionPage import ChromiumPage, ChromiumOptions
    except ImportError:
        logger.debug("[drission] DrissionPage 未安装，跳过 (pip install DrissionPage)")
        return None

    try:
        co = ChromiumOptions()
        co.headless(True)
        co.set_argument("--no-sandbox")
        co.set_argument("--disable-dev-shm-usage")
        co.set_argument("--disable-blink-features=AutomationControlled")
        co.set_argument("--disable-extensions")
        co.set_pref("credentials_enable_service", False)

        if PROXY:
            co.set_proxy(PROXY)
            logger.info(f"[drission] 使用代理: {PROXY}")

        page = ChromiumPage(co)
        try:
            # 先访问首页热身（有助于 CF 信任度）
            if referer:
                page.get(referer)
                time.sleep(1.5)

            page.get(url)

            # 等待 CF 挑战完成，最多 20 秒
            deadline = time.time() + 20
            while time.time() < deadline:
                html = page.html
                if not any(kw in html for kw in
                           ("Just a moment", "cf-browser-verification",
                            "__cf_chl_", "challenge-running")):
                    break
                logger.debug("[drission] 等待 CF 挑战通过...")
                time.sleep(1)

            html = page.html
            logger.info(f"[drission] 获取成功，页面长度: {len(html)}")
            return html
        finally:
            try:
                page.quit()
            except Exception:
                pass

    except Exception as e:
        logger.warning(f"[drission] 异常: {e}")
        return None


# ─── Level 1: curl_cffi（TLS 指纹模拟） ──────────────────────────────────────

def _fetch_with_curl_cffi(url: str, referer: str = "") -> Optional[str]:
    """模拟 Chrome 120 TLS 握手特征，绕过 JA3/JA4 指纹检测。"""
    try:
        from curl_cffi import requests as cffi_req
    except ImportError:
        logger.debug("[curl_cffi] 未安装，跳过 (pip install curl_cffi)")
        return None

    headers = {**HEADERS}
    if referer:
        headers["Referer"] = referer
    if PROXY:
        logger.info(f"[curl_cffi] 使用代理: {PROXY}")

    try:
        resp = cffi_req.get(
            url,
            headers=headers,
            impersonate="chrome120",
            timeout=TIMEOUT,
            allow_redirects=True,
            proxy=PROXY,        # curl_cffi 用 proxy= 字符串，None 表示不使用
        )
        if resp.status_code == 200:
            logger.info(f"[curl_cffi] 成功 (HTTP 200): {url}")
            return resp.text
        logger.warning(f"[curl_cffi] HTTP {resp.status_code}")
        return None
    except Exception as e:
        logger.warning(f"[curl_cffi] 异常: {e}")
        return None


# ─── Level 2: cloudscraper（旧版 CF JS 挑战） ────────────────────────────────

def _fetch_with_cloudscraper(url: str, referer: str = "") -> Optional[str]:
    """执行 Cloudflare IUAM 模式的 JS 挑战（适合旧版 CF 防护）。"""
    try:
        import cloudscraper
    except ImportError:
        logger.debug("[cloudscraper] 未安装，跳过 (pip install cloudscraper)")
        return None

    try:
        scraper = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False},
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
        logger.warning(f"[cloudscraper] HTTP {resp.status_code}")
        return None
    except Exception as e:
        logger.warning(f"[cloudscraper] 异常: {e}")
        return None


# ─── Level 3: requests（普通浏览器头，兜底） ─────────────────────────────────

def _make_session(referer: str = "") -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    if referer:
        session.headers.update({"Referer": referer})
    if PROXY:
        session.proxies.update({"http": PROXY, "https": PROXY})
    try:
        session.get("https://supjav.com/", timeout=TIMEOUT)
        time.sleep(1.0)
    except Exception:
        pass
    return session


def _fetch_with_requests(url: str, referer: str = "") -> Optional[str]:
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

def fetch_page(url: str, session=None) -> str:
    """
    依次尝试四个级别获取页面 HTML，全部失败则抛出 RuntimeError。
    """
    parsed = urlparse(url)
    referer = f"{parsed.scheme}://{parsed.netloc}/"

    if PROXY:
        print(f"    代理已配置: {PROXY}", flush=True)
        if check_proxy():
            print(f"    ✓ 代理端口连通正常", flush=True)
        else:
            print(f"    ⚠ 代理端口不可达，请确认代理软件正在运行", flush=True)

    print("    尝试 Level 0: DrissionPage (真实 Chrome，CF JS 挑战)...", flush=True)
    html = _fetch_with_drissionpage(url, referer)
    if html and _is_real_page(html):
        return html

    print("    尝试 Level 1: curl_cffi (TLS 指纹模拟)...", flush=True)
    html = _fetch_with_curl_cffi(url, referer)
    if html and _is_real_page(html):
        return html

    print("    尝试 Level 2: cloudscraper (旧版 CF JS 挑战)...", flush=True)
    html = _fetch_with_cloudscraper(url, referer)
    if html and _is_real_page(html):
        return html

    print("    尝试 Level 3: requests (普通浏览器头)...", flush=True)
    html = _fetch_with_requests(url, referer)
    if html and _is_real_page(html):
        return html

    raise RuntimeError(f"无法获取页面: {url}")


def _is_real_page(html: str) -> bool:
    """检测页面是否是真实内容（排除 CF 挑战页）。"""
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
            logger.warning(f"[_is_real_page] 仍是 CF 挑战页: {indicator!r}")
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
    results = []
    for tag in soup.find_all(True, attrs={"class": re.compile(r"server|mirror|source|player", re.I)}):
        for attr in ("data-url", "data-src", "data-embed", "href"):
            val = tag.get(attr, "")
            if val and val.startswith("http"):
                results.append(val.strip())
    return results


def _is_video_candidate(url: str) -> bool:
    """
    过滤掉明显不是视频的 URL：
    - .js / .css / .png / .gif / .svg / .woff 等静态资源
    - 广告、统计、CDN 脚本域名
    """
    lower = url.lower()

    # 排除静态资源扩展名（路径部分，忽略 query）
    path = lower.split("?")[0]
    skip_exts = (".js", ".css", ".png", ".jpg", ".jpeg", ".gif",
                 ".svg", ".woff", ".woff2", ".ttf", ".ico", ".xml", ".json")
    if any(path.endswith(ext) for ext in skip_exts):
        return False

    # 排除已知非视频域名
    for domain in NON_VIDEO_DOMAINS:
        if domain in lower:
            return False

    return True


def extract_embed_urls(html: str, page_url: str) -> list[str]:
    """综合提取页面中所有候选视频嵌入 URL，过滤非视频资源后去重返回。"""
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
        if url and url not in seen and _is_video_candidate(url):
            seen.add(url)
            results.append(url)
    logger.info(f"[extract_embed_urls] 共发现 {len(results)} 个候选 URL（已过滤非视频资源）")
    return results


def get_page_title(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    title = soup.title
    if title:
        return re.sub(r'[\\/:*?"<>|]', "_", title.text.strip())
    return "video"
