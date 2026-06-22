"""
反爬绕过工具集

supjav.com 使用 Cloudflare 保护，常见的绕过手段：
  1. 正确的 TLS 指纹（使用 curl_cffi 模拟浏览器 TLS）
  2. 随机 User-Agent 轮换
  3. 请求间隔随机化
  4. Cookie 持久化
"""

import re
import time
import random
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ── User-Agent 池 ─────────────────────────────────────────────────────────────
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
]


def random_ua() -> str:
    return random.choice(USER_AGENTS)


def random_sleep(min_s: float = 0.5, max_s: float = 2.0):
    """随机等待，模拟人类行为"""
    time.sleep(random.uniform(min_s, max_s))


def build_headers(referer: str = "", ua: Optional[str] = None) -> dict:
    """构建完整的浏览器请求头"""
    headers = {
        "User-Agent": ua or random_ua(),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin" if referer else "none",
        "Sec-Fetch-User": "?1",
    }
    if referer:
        headers["Referer"] = referer
    return headers


def try_curl_cffi(url: str, referer: str = "") -> Optional[str]:
    """
    尝试使用 curl_cffi 模拟真实浏览器 TLS 指纹，绕过 Cloudflare Bot 检测。
    curl_cffi 需要单独安装：pip install curl_cffi
    """
    try:
        from curl_cffi import requests as cffi_requests

        headers = build_headers(referer)
        resp = cffi_requests.get(
            url,
            headers=headers,
            impersonate="chrome120",
            timeout=30,
        )
        if resp.status_code == 200:
            logger.info(f"[curl_cffi] 成功绕过: {url}")
            return resp.text
        else:
            logger.warning(f"[curl_cffi] HTTP {resp.status_code}")
            return None
    except ImportError:
        logger.debug("[curl_cffi] 未安装，跳过")
        return None
    except Exception as e:
        logger.warning(f"[curl_cffi] 失败: {e}")
        return None


def detect_cloudflare(html: str) -> bool:
    """检测页面是否是 Cloudflare 挑战页"""
    cf_indicators = [
        "Just a moment",
        "cf-browser-verification",
        "Checking your browser",
        "DDoS protection by Cloudflare",
        "__cf_chl_",
        "challenge-running",
    ]
    return any(indicator in html for indicator in cf_indicators)


def detect_403_waf(response_code: int, html: str) -> bool:
    """检测是否遇到 WAF 403"""
    if response_code == 403:
        return True
    waf_indicators = ["Access Denied", "403 Forbidden", "Blocked by security policy"]
    return any(ind in html for ind in waf_indicators)
