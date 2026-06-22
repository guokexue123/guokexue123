"""
Surrit.store 提取器（123av.com 专用视频 CDN）

URL 模式：https://surrit.store/e/<ID>?poster=...
技术原理：
  1. 用 curl_cffi 带代理抓取嵌入页面（该站也有 CF 防护）
  2. 在 HTML/JS 中查找 m3u8 主列表或直链 mp4
  3. m3u8 通常以 /master.m3u8 或 /playlist.m3u8 结尾
"""

import re
import time
import logging
import requests

from .base import BaseExtractor, VideoInfo
from config import HEADERS, TIMEOUT, PROXY

logger = logging.getLogger(__name__)


class SurritStoreExtractor(BaseExtractor):

    def can_handle(self, url: str) -> bool:
        return "surrit." in url.lower()

    def extract(self, url: str) -> VideoInfo | None:
        html = self._fetch_embed(url)
        if not html:
            return None

        # 1. 明文 m3u8
        m = re.search(r'["\' ](https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*)', html)
        if m:
            logger.info(f"[Surrit] 找到 m3u8: {m.group(1)[:80]}")
            return VideoInfo(url=m.group(1), is_m3u8=True,
                             headers={"Referer": url, "Origin": "https://surrit.store"})

        # 2. 相对路径 m3u8（以 /hls/ 或 /stream/ 开头）
        m2 = re.search(r'["\' ](/[^\s"\'<>]*\.m3u8[^\s"\'<>]*)', html)
        if m2:
            from urllib.parse import urljoin
            full = urljoin(url, m2.group(1))
            logger.info(f"[Surrit] 相对 m3u8: {full[:80]}")
            return VideoInfo(url=full, is_m3u8=True,
                             headers={"Referer": url, "Origin": "https://surrit.store"})

        # 3. JWPlayer / Video.js sources 数组
        patterns = [
            r'"file"\s*:\s*"(https?://[^"]+)"',
            r"'file'\s*:\s*'(https?://[^']+)'",
            r'"src"\s*:\s*"(https?://[^"]+(?:\.m3u8|\.mp4)[^"]*)"',
            r'source\s*src\s*=\s*["\']([^"\']+\.m3u8[^"\']*)["\']',
        ]
        for pat in patterns:
            for found in re.finditer(pat, html, re.IGNORECASE):
                u = found.group(1)
                if u.startswith("//"):
                    u = "https:" + u
                logger.info(f"[Surrit] sources 中找到: {u[:80]}")
                is_m3u8 = ".m3u8" in u
                return VideoInfo(url=u, is_m3u8=is_m3u8,
                                 headers={"Referer": url})

        # 4. 直接 mp4
        m3 = re.search(r'["\' ](https?://[^\s"\'<>]+\.mp4[^\s"\'<>]*)', html)
        if m3:
            logger.info(f"[Surrit] 找到 mp4: {m3.group(1)[:80]}")
            return VideoInfo(url=m3.group(1), headers={"Referer": url})

        logger.warning(f"[Surrit] 未能提取视频，页面长度: {len(html)}")
        return None

    def _fetch_embed(self, url: str) -> str | None:
        """先用 curl_cffi 尝试（CF 保护），失败降级到普通 requests"""
        headers = {
            **HEADERS,
            "Referer": "https://123av.com/",
            "Origin": "https://123av.com",
            "Sec-Fetch-Dest": "iframe",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "cross-site",
        }

        # curl_cffi 方案（带代理 + TLS 指纹）
        try:
            from curl_cffi import requests as cffi_req
            resp = cffi_req.get(
                url,
                headers=headers,
                impersonate="chrome120",
                proxy=PROXY,
                timeout=TIMEOUT,
                allow_redirects=True,
            )
            if resp.status_code == 200 and len(resp.text) > 200:
                logger.info(f"[Surrit] curl_cffi 获取成功 ({len(resp.text)} 字节)")
                return resp.text
            logger.warning(f"[Surrit] curl_cffi HTTP {resp.status_code}")
        except ImportError:
            pass
        except Exception as e:
            logger.warning(f"[Surrit] curl_cffi 异常: {e}")

        # requests 降级方案
        try:
            session = requests.Session()
            session.headers.update(headers)
            if PROXY:
                session.proxies.update({"http": PROXY, "https": PROXY})
            resp = session.get(url, timeout=TIMEOUT)
            if resp.status_code == 200:
                return resp.text
            logger.warning(f"[Surrit] requests HTTP {resp.status_code}")
        except Exception as e:
            logger.error(f"[Surrit] 获取嵌入页失败: {e}")

        return None
