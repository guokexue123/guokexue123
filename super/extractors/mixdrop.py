"""
Mixdrop 提取器

技术原理：
  Mixdrop 使用 eval(atob(...)) 混淆 JS，解码后可找到 MDCore.wurl。
  直链格式：https://s-<id>.mixdrop.co/<path>
"""

import re
import base64
import logging
import requests

from .base import BaseExtractor, VideoInfo
from config import HEADERS, TIMEOUT

logger = logging.getLogger(__name__)


def _eval_atob_decode(js_code: str) -> str:
    """简单解码 eval(atob("...")) 形式"""
    m = re.search(r'atob\(["\']([^"\']+)["\']\)', js_code)
    if m:
        try:
            return base64.b64decode(m.group(1)).decode("utf-8", errors="replace")
        except Exception:
            pass
    return js_code


class MixdropExtractor(BaseExtractor):

    def can_handle(self, url: str) -> bool:
        return "mixdrop" in url

    def extract(self, url: str) -> VideoInfo | None:
        # /v/ → /e/ 嵌入页
        url = re.sub(r"/v/", "/e/", url)

        session = requests.Session()
        session.headers.update(HEADERS)

        try:
            resp = session.get(url, timeout=TIMEOUT)
            resp.raise_for_status()
            html = resp.text
        except Exception as e:
            logger.error(f"[Mixdrop] 获取嵌入页失败: {e}")
            return None

        # 先尝试解码 atob
        decoded = _eval_atob_decode(html)
        search_text = decoded if decoded != html else html

        # 找 MDCore.wurl
        m = re.search(r'MDCore\.wurl\s*=\s*["\']([^"\']+)["\']', search_text)
        if m:
            direct_url = m.group(1)
            if not direct_url.startswith("http"):
                direct_url = "https:" + direct_url
            logger.info(f"[Mixdrop] 提取成功: {direct_url[:80]}...")
            return VideoInfo(url=direct_url, headers={"Referer": url})

        # 备用：直接找 .mp4 链接
        m2 = re.search(r'["\']([^"\']*\.mp4[^"\']*)["\']', search_text)
        if m2:
            direct_url = m2.group(1)
            if not direct_url.startswith("http"):
                direct_url = "https:" + direct_url
            logger.info(f"[Mixdrop] 备用提取: {direct_url[:80]}...")
            return VideoInfo(url=direct_url, headers={"Referer": url})

        logger.warning("[Mixdrop] 未能提取直链")
        return None
