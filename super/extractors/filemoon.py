"""
Filemoon 提取器

技术原理：
  Filemoon 使用 JWPlayer 加载视频，真实 m3u8 URL 经过
  p,a,c,k,e,d 混淆（eval(function(p,a,c,k,e,d){...})）。
  需要解包该 JS 后找到 sources 中的 file 字段。
"""

import re
import logging
import requests

from .base import BaseExtractor, VideoInfo
from config import HEADERS, TIMEOUT

logger = logging.getLogger(__name__)


def _unpack_packer(packed: str) -> str:
    """
    简化版 p,a,c,k,e,d 解包器。
    提取 p,a,c,k,e,d 参数并还原。
    """
    m = re.search(
        r"eval\(function\(p,a,c,k,e,(?:r|d)\).*?'([^']+)'\.split\('\|'\)",
        packed, re.DOTALL,
    )
    if not m:
        return packed

    # 尝试用 regex 提取关键 m3u8 URL（比完整解包更可靠）
    parts = m.group(1).split("|")
    # 找包含 .m3u8 的组合
    for part in parts:
        if ".m3u8" in part or "master" in part:
            return part
    return packed


class FilemoonExtractor(BaseExtractor):

    def can_handle(self, url: str) -> bool:
        return any(d in url for d in ("filemoon.sx", "filemoon.in", "filemoon.to"))

    def extract(self, url: str) -> VideoInfo | None:
        session = requests.Session()
        session.headers.update(HEADERS)

        # 修正为嵌入页
        url = re.sub(r"/v/", "/e/", url)

        try:
            resp = session.get(url, timeout=TIMEOUT)
            resp.raise_for_status()
            html = resp.text
        except Exception as e:
            logger.error(f"[Filemoon] 获取嵌入页失败: {e}")
            return None

        # 先找明文 m3u8
        m = re.search(r'"file"\s*:\s*"([^"]+\.m3u8[^"]*)"', html)
        if m:
            logger.info(f"[Filemoon] 明文 m3u8: {m.group(1)[:80]}...")
            return VideoInfo(url=m.group(1), is_m3u8=True, headers={"Referer": url})

        # 尝试解包 packer
        unpacked = _unpack_packer(html)
        m2 = re.search(r'"file"\s*:\s*"([^"]+\.m3u8[^"]*)"', unpacked)
        if m2:
            logger.info(f"[Filemoon] 解包后 m3u8: {m2.group(1)[:80]}...")
            return VideoInfo(url=m2.group(1), is_m3u8=True, headers={"Referer": url})

        # 备用：找任意 m3u8
        m3 = re.search(r'(https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*)', html)
        if m3:
            return VideoInfo(url=m3.group(1), is_m3u8=True, headers={"Referer": url})

        logger.warning("[Filemoon] 未能提取 m3u8")
        return None
