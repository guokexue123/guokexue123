"""
通用提取器

处理所有未被专用提取器识别的视频托管服务。
策略：
  1. 获取页面 HTML
  2. 找所有 m3u8 / mp4 直链
  3. 找 JWPlayer sources
  4. 找 Video.js sources
"""

import re
import json
import logging
import requests

from .base import BaseExtractor, VideoInfo
from config import HEADERS, TIMEOUT

logger = logging.getLogger(__name__)


class GenericExtractor(BaseExtractor):

    def can_handle(self, url: str) -> bool:
        return True  # 兜底

    def extract(self, url: str) -> VideoInfo | None:
        session = requests.Session()
        session.headers.update(HEADERS)

        try:
            resp = session.get(url, headers={"Referer": url}, timeout=TIMEOUT)
            resp.raise_for_status()
            html = resp.text
        except Exception as e:
            logger.error(f"[Generic] 获取页面失败 ({url}): {e}")
            return None

        # 1. 直接 m3u8
        m = re.search(r'["\' ](https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*)', html)
        if m:
            logger.info(f"[Generic] 找到 m3u8: {m.group(1)[:80]}")
            return VideoInfo(url=m.group(1), is_m3u8=True, headers={"Referer": url})

        # 2. 直接 mp4
        m2 = re.search(r'["\' ](https?://[^\s"\'<>]+\.mp4[^\s"\'<>]*)', html)
        if m2:
            logger.info(f"[Generic] 找到 mp4: {m2.group(1)[:80]}")
            return VideoInfo(url=m2.group(1), headers={"Referer": url})

        # 3. JWPlayer / Video.js sources 数组
        # 匹配 {"file":"..."} 或 {src:"..."}
        patterns = [
            r'"file"\s*:\s*"(https?://[^"]+)"',
            r"'file'\s*:\s*'(https?://[^']+)'",
            r'"src"\s*:\s*"(https?://[^"]+)"',
        ]
        for pat in patterns:
            for found in re.finditer(pat, html):
                u = found.group(1)
                if any(ext in u for ext in (".m3u8", ".mp4", ".flv", "stream")):
                    logger.info(f"[Generic] sources 中找到: {u[:80]}")
                    is_m3u8 = ".m3u8" in u
                    return VideoInfo(url=u, is_m3u8=is_m3u8, headers={"Referer": url})

        # 4. 嵌套 iframe：把 iframe 的 src 也返回（让主流程再试一次）
        m3 = re.search(r'<iframe[^>]+src=["\']([^"\']+)["\']', html, re.IGNORECASE)
        if m3:
            iframe_src = m3.group(1).strip()
            if iframe_src.startswith("http"):
                logger.info(f"[Generic] 发现嵌套 iframe: {iframe_src[:80]}")
                return self.extract(iframe_src)  # 递归一层

        logger.warning(f"[Generic] 无法从 {url} 提取视频")
        return None
