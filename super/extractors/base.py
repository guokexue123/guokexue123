"""
提取器基类
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class VideoInfo:
    """提取到的视频信息"""
    url: str                        # 直链或 m3u8
    title: str = ""
    ext: str = "mp4"
    headers: dict = field(default_factory=dict)
    is_m3u8: bool = False
    quality: str = "best"


class BaseExtractor(ABC):
    """所有提取器的基类"""

    logger = logging.getLogger(__name__)

    @abstractmethod
    def can_handle(self, url: str) -> bool:
        """判断是否能处理该 URL"""

    @abstractmethod
    def extract(self, url: str) -> Optional[VideoInfo]:
        """从嵌入页面 URL 提取真实视频信息，失败返回 None"""
