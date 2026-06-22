"""
DoodStream / dood.* 提取器

技术原理：
  DoodStream 的播放页面会动态生成一个带时效 token 的直链。
  流程：
    1. GET /e/<id>  → 获取 pass_md5 路径
    2. GET /pass_md5/<token>  → 返回视频直链前缀（302 或文本）
    3. 拼接随机后缀和时间戳参数得到完整直链
"""

import re
import time
import random
import string
import logging
import requests

from .base import BaseExtractor, VideoInfo
from config import HEADERS, TIMEOUT

logger = logging.getLogger(__name__)


class DoodStreamExtractor(BaseExtractor):

    DOOD_DOMAINS = ("doodstream.com", "dood.watch", "dood.la", "dood.cx",
                    "dood.sh", "dood.pm", "dood.wf", "dood.re", "dood.yt",
                    "ds2play.com", "doods.pro")

    def can_handle(self, url: str) -> bool:
        return any(d in url for d in self.DOOD_DOMAINS)

    def extract(self, url: str) -> VideoInfo | None:
        session = requests.Session()
        session.headers.update(HEADERS)

        # 把 /d/ 格式换成 /e/ 格式
        url = re.sub(r"/d/", "/e/", url)

        try:
            resp = session.get(url, timeout=TIMEOUT)
            resp.raise_for_status()
            html = resp.text
        except Exception as e:
            logger.error(f"[DoodStream] 获取嵌入页失败: {e}")
            return None

        # 1. 找 pass_md5 路径
        m = re.search(r"pass_md5/[^'\"]+", html)
        if not m:
            logger.warning("[DoodStream] 未找到 pass_md5 路径")
            return None

        pass_path = "/" + m.group(0)
        base_url = re.match(r"https?://[^/]+", url).group(0)
        pass_url = base_url + pass_path

        # 2. 获取直链前缀
        try:
            resp2 = session.get(
                pass_url,
                headers={"Referer": url},
                timeout=TIMEOUT,
            )
            resp2.raise_for_status()
            prefix = resp2.text.strip()
        except Exception as e:
            logger.error(f"[DoodStream] 获取 pass_md5 失败: {e}")
            return None

        # 3. 拼接完整直链（随机 10 位字符 + 时间戳）
        rand = "".join(random.choices(string.ascii_letters + string.digits, k=10))
        ts = int(time.time() * 1000)
        direct_url = f"{prefix}{rand}?token={pass_path.split('/')[-1]}&expiry={ts}"

        logger.info(f"[DoodStream] 提取成功: {direct_url[:80]}...")
        return VideoInfo(
            url=direct_url,
            headers={"Referer": base_url + "/"},
        )
