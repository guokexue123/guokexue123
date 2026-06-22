"""
Streamtape 提取器

技术原理：
  Streamtape 将真实下载链接混淆在 JS 中：
    var robotlink = '/streamtape.com/get_video?...'
    document.getElementById('ideoolink').innerHTML = robotlink + "xxxxx"
  需要把两段字符串拼接后加上 https: 前缀。
"""

import re
import logging
import requests

from .base import BaseExtractor, VideoInfo
from config import HEADERS, TIMEOUT

logger = logging.getLogger(__name__)


class StreamtapeExtractor(BaseExtractor):

    def can_handle(self, url: str) -> bool:
        return "streamtape.com" in url

    def extract(self, url: str) -> VideoInfo | None:
        # 把 /v/ 换成 /e/（嵌入页）
        url = re.sub(r"/v/", "/e/", url)

        session = requests.Session()
        session.headers.update(HEADERS)

        try:
            resp = session.get(url, timeout=TIMEOUT)
            resp.raise_for_status()
            html = resp.text
        except Exception as e:
            logger.error(f"[Streamtape] 获取嵌入页失败: {e}")
            return None

        # 方法 1：标准混淆格式
        m = re.search(
            r"document\.getElementById\('ideoolink'\)\.innerHTML\s*=\s*"
            r"(['\"])(.+?)\1\s*\+\s*(['\"])(.+?)\3",
            html,
        )
        if m:
            part1, part2 = m.group(2), m.group(4)
            # part1 末尾可能是被截断的 part2 前半部分
            direct_url = "https:" + part1 + part2
            logger.info(f"[Streamtape] 提取成功 (方法1): {direct_url[:80]}...")
            return VideoInfo(url=direct_url, headers={"Referer": url})

        # 方法 2：robotlink 模式
        m2 = re.search(r"var robotlink\s*=\s*['\"]([^'\"]+)['\"]", html)
        if m2:
            direct_url = "https:" + m2.group(1)
            logger.info(f"[Streamtape] 提取成功 (方法2): {direct_url[:80]}...")
            return VideoInfo(url=direct_url, headers={"Referer": url})

        # 方法 3：直接找 /get_video 链接
        m3 = re.search(r"(//streamtape\.com/get_video\?[^'\"<\s]+)", html)
        if m3:
            direct_url = "https:" + m3.group(1)
            logger.info(f"[Streamtape] 提取成功 (方法3): {direct_url[:80]}...")
            return VideoInfo(url=direct_url, headers={"Referer": url})

        logger.warning("[Streamtape] 未能提取直链")
        return None
