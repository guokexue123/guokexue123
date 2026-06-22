"""
视频托管平台子提取器包

每个提取器负责从特定托管服务的页面中提取真实视频流 URL。
"""

from .doodstream import DoodStreamExtractor
from .streamtape import StreamtapeExtractor
from .mixdrop import MixdropExtractor
from .filemoon import FilemoonExtractor
from .surrit import SurritStoreExtractor
from .generic import GenericExtractor

__all__ = [
    "DoodStreamExtractor",
    "StreamtapeExtractor",
    "MixdropExtractor",
    "FilemoonExtractor",
    "SurritStoreExtractor",
    "GenericExtractor",
]


def get_extractor(url: str):
    """根据 URL 返回匹配的提取器实例"""
    from urllib.parse import urlparse
    host = urlparse(url).netloc.lower()

    if any(d in host for d in ("doodstream", "dood.")):
        return DoodStreamExtractor()
    if "streamtape" in host:
        return StreamtapeExtractor()
    if "mixdrop" in host:
        return MixdropExtractor()
    if "filemoon" in host:
        return FilemoonExtractor()
    if "surrit." in host:
        return SurritStoreExtractor()
    return GenericExtractor()
