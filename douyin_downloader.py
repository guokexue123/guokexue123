#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
抖音视频下载器 v2
依赖: pip install playwright requests tqdm
      playwright install chromium
"""

import asyncio
import json
import re
import shutil
import sys
import time
import subprocess
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import requests
from tqdm import tqdm

try:
    from playwright.async_api import async_playwright
except ImportError:
    print("请先安装: pip install playwright && playwright install chromium")
    sys.exit(1)

# ==================== 配置区 ====================

TARGET_URL = "https://www.douyin.com/user/MS4wLjABAAAA-iX6NHZffBZ-V_473LCr35TlkbWaFmDtU3Ml4AKqk7M?from_tab_name=main&modal_id=7653370930916804518"

# 直接粘贴你的 Cookie 字符串（从浏览器 DevTools → Network → 请求头复制）
# 格式: "key1=val1; key2=val2; ..."
COOKIE_STRING = "UIFID_TEMP=4f370c6d8fb718a382d31de50859ac71a63e1a5cc19a7a2de24e63803f467b4185e8ee03f95c45e4fbc6c1f82f6ca45899af9d189772a76dc896a3e53bb95aa90f6c3bcae3e7df88138fd1df48ce76ee; x-web-secsdk-uid=974a0e02-301f-4b2e-84a6-87eee1454ab3; s_v_web_id=verify_mqp2lrzx_t46vJwUy_sEGu_4BLd_BUe7_dG7070kSlVQV; douyin.com; device_web_cpu_core=12; device_web_memory_size=16; architecture=amd64; is_support_rtm_web_ts=1; dy_swidth=1440; dy_sheight=960; hevc_supported=true; bd_ticket_guard_client_web_domain=2; passport_csrf_token=d9b269c321d65fca93e3360b30041f64; passport_csrf_token_default=d9b269c321d65fca93e3360b30041f64; fpk1=U2FsdGVkX19f3/H1zhEM5Bi/9zWsZfkn3npQMespXAS3tdye2n4RaP310Yphocf3hBWM1ViSNOEuPW6ObxmmCA==; fpk2=16fee37559dbd42b448204446d02089f; strategyABtestKey=%221782123987.946%22; ttwid=1%7CGHJcGPwt8WXpBEjvxE-AXuye-CJ4h0RqJKwXkVkUpa0%7C1782123988%7C4e0d89ccaf05e76088e8a8e4604f2ca69afb2128f84ddad9ff859168cf518233; passport_assist_user=CkHbGEQgu-rIfQE3u8tjyhV6gB_HA2zsST1HOv0iHgbkDMNnXK0ttWmfx3zRZcudpWKCmy084pRHETnWArAlKT_F0hpKCjwAAAAAAAAAAAAAUJIIPyiE2yDzlIImEByv6cPWxxidYgVVWUO3EIuptNC67xPWmwnXdDZ6fTKc0PJlcW4Q-O6UDhiJr9ZUIAEiAQPaZ3R2; n_mh=UKX2LUQu2SXFrmTxbqzL4ODO4M0jm_o9UdL_5Mq3Ti4; sid_guard=f0258c84230ddfeec544491693f9fb51%7C1782124009%7C5184000%7CFri%2C+21-Aug-2026+10%3A26%3A49+GMT; uid_tt=690d7bbcdc2621def8be81945c5aeab0; uid_tt_ss=690d7bbcdc2621def8be81945c5aeab0; sid_tt=f0258c84230ddfeec544491693f9fb51; sessionid=f0258c84230ddfeec544491693f9fb51; sessionid_ss=f0258c84230ddfeec544491693f9fb51; session_tlb_tag=sttt%7C12%7C8CWMhCMN3-7FREkWk_n7Uf_________CH2r72DnGVlI55mOX5cxVNnEv-UhvRGvd7hbPJjsaWCw%3D; is_staff_user=false; has_biz_token=false; sid_ucp_v1=1.0.0-KDdlYWRkMzc1ZTEzMmY0ODk5N2FmMmY2MTlmMzViZjFjYjk4MDVmMjMKIQi66cCn6M2OBhDpm-TRBhjvMSAMMPOfoa8GOAdA9AdIBBoCaGwiIGYwMjU4Yzg0MjMwZGRmZWVjNTQ0NDkxNjkzZjlmYjUx; ssid_ucp_v1=1.0.0-KDdlYWRkMzc1ZTEzMmY0ODk5N2FmMmY2MTlmMzViZjFjYjk4MDVmMjMKIQi66cCn6M2OBhDpm-TRBhjvMSAMMPOfoa8GOAdA9AdIBBoCaGwiIGYwMjU4Yzg0MjMwZGRmZWVjNTQ0NDkxNjkzZjlmYjUx; _bd_ticket_crypt_cookie=f7ab8b87c7222d49307802def31c1a04; __security_mc_1_s_sdk_sign_data_key_web_protect=beac786b-49b5-8f77; __security_mc_1_s_sdk_cert_key=1c311e65-447a-b7fb; __security_mc_1_s_sdk_crypt_sdk=c5060dae-45bd-b760; __security_server_data_status=1; login_time=1782124009599; __ac_nonce=06a390de9002cfe6f33a3; __ac_signature=_02B4Z6wo00f012zOSLAAAIDCFie8VUjwKz9s7kwAALEV4c; UIFID=4f370c6d8fb718a382d31de50859ac71a63e1a5cc19a7a2de24e63803f467b4185e8ee03f95c45e4fbc6c1f82f6ca45810cae4a92e1012e49a80a06fe77963b237e4020e5c1361a742fc5fdaac4f536d8ea7d4faf3e3e9c421c12920cec16ff3cb437a6190c865c905aa1a7db475b98c7ee914bbcc97f95d7d46d4a2a6872b27c58bbeca6903bc0613b27c7733c37b8619aa51136f46e773d7b36983675bad5e; stream_recommend_feed_params=%22%7B%5C%22cookie_enabled%5C%22%3Atrue%2C%5C%22screen_width%5C%22%3A1440%2C%5C%22screen_height%5C%22%3A960%2C%5C%22browser_online%5C%22%3Atrue%2C%5C%22cpu_core_num%5C%22%3A12%2C%5C%22device_memory%5C%22%3A16%2C%5C%22downlink%5C%22%3A10%2C%5C%22effective_type%5C%22%3A%5C%224g%5C%22%2C%5C%22round_trip_time%5C%22%3A50%7D%22; publish_badge_show_info=%220%2C0%2C0%2C1782124012120%22; FOLLOW_NUMBER_YELLOW_POINT_INFO=%22MS4wLjABAAAAKWT7glJitZ1-lvbx9TgFQ4-YB9RDrr3dauIFOLXnRMQfDn2WXYy1luANRm8M8v-B%2F1782144000000%2F0%2F1782124012171%2F0%22; is_dash_user=1; my_rd=2; FOLLOW_LIVE_POINT_INFO=%22MS4wLjABAAAAKWT7glJitZ1-lvbx9TgFQ4-YB9RDrr3dauIFOLXnRMQfDn2WXYy1luANRm8M8v-B%2F1782144000000%2F1782124099569%2F1782124077769%2F0%22; SelfTabRedDotControl=%5B%7B%22id%22%3A%227574797773711607827%22%2C%22u%22%3A73%2C%22c%22%3A73%7D%5D; __druidClientInfo=JTdCJTIyY2xpZW50V2lkdGglMjIlM0ExMjgwJTJDJTIyY2xpZW50SGVpZ2h0JTIyJTNBNzM1JTJDJTIyd2lkdGglMjIlM0ExMjgwJTJDJTIyaGVpZ2h0JTIyJTNBNzM1JTJDJTIyZGV2aWNlUGl4ZWxSYXRpbyUyMiUzQTEuNSUyQyUyMnVzZXJBZ2VudCUyMiUzQSUyMk1vemlsbGElMkY1LjAlMjAoV2luZG93cyUyME5UJTIwMTAuMCUzQiUyMFdpbjY0JTNCJTIweDY0KSUyMEFwcGxlV2ViS2l0JTJGNTM3LjM2JTIwKEtIVE1MJTJDJTIwbGlrZSUyMEdlY2tvKSUyMENocm9tZSUyRjE0OS4wLjAuMCUyMFNhZmFyaSUyRjUzNy4zNiUyMiU3RA==; __live_version__=%221.1.5.3126%22; webcast_local_quality=null; live_use_vvc=%22false%22; live_private_user=0; live_can_add_dy_2_desktop=%221%22; playRecommendGuideTagCount=1; totalRecommendGuideTagCount=1; SEARCH_RESULT_LIST_TYPE=%22single%22; csrf_session_id=d026ccf0e0693617aa2e0f4491f38c22; download_guide=%223%2F20260622%2F0%22; sdk_source_info=7e276470716a68645a606960273f276364697660272927676c715a6d6069756077273f276364697660272927666d776a68605a607d71606b766c6a6b5a7666776c7571273f275e58272927666a6b766a69605a696c6061273f27636469766027292762696a6764695a7364776c6467696076273f275e582729277672715a646971273f2763646976602729277f6b5a666475273f2763646976602729276d6a6e5a6b6a716c273f2763646976602729276c6b6f5a7f6367273f27636469766027292771273f273135353d3430303734373d3234272927676c715a75776a716a666a69273f2763646976602778; bit_env=wLyzqbTLFp13ks9jjYtDDnJs9XuixSTMiSasJLO-oWuRvTzs4ZQIluWK8nssuV43UTDO0lrgeai3pO0yVShocHismZ8J6iw8D5ANRBmjniI5ZU6CfRXUFV_xTBpojDeXWOwvEHcia8r9kMEwbenU-LUIlrdXLCw6CpakFTmxffq6LJ7C1sA2LYCNDmRTll8payash1OJ1ADlKuFeAyZ6bj-PEgjhJGYR8TRrvK1dFF6X_K-GNajbRadTrHZPOZMF6FhVU5igV7CPac6uijVZbxdlXzJCFpNd53iQ8Vx1ZXvZD9YkJVrP_J9NWJMuHv87jTqgvDL0FgoMQ0fAhKUuFvuwRN9_JsbSKwPgx-nkV0OyY4j2POnY-LBoBg5OlDTxucuafgAGW_yfR2muIQsU9FpX6D7Sov8rNKEQHM3bwXjMWdILnkC2ikyCHkIG7gL3rj5IAXNczobrs8wQ3rV8jMBYWmlFBTAluAwM_7gPxelWNYDwBq5nVMoOPdIbeezO; gulu_source_res=eyJwX2luIjoiN2M4YTc2ZWZiODlkOGY4ZWEzM2M0ZjM5OWM2MGY0Mjg5OTFmNmNmZjA5YTE5MmIxZTNhZjMzNzMxZjA2NDY3MSJ9; passport_auth_mix_state=92ktnzsg0i9vvdtbe9w06wrwz3k1a01r5da5n8mjuofqy93k; volume_info=%7B%22isUserMute%22%3Afalse%2C%22isMute%22%3Atrue%2C%22volume%22%3A0.5%7D; bd_ticket_guard_client_data=eyJiZC10aWNrZXQtZ3VhcmQtdmVyc2lvbiI6MiwiYmQtdGlja2V0LWd1YXJkLWl0ZXJhdGlvbi12ZXJzaW9uIjoxLCJiZC10aWNrZXQtZ3VhcmQtcmVlLXB1YmxpYy1rZXkiOiJCR1UzVXVIVWtwZ0JoU01DVXJ3K0NVZVg3R28wUXpoZmFDdmZvdDdoUDNjOEg1S1l2bHR2dE9WVjZaSFNGRjFGOGRZVEJOZWxPZzhYYy9jTHErblNjNUE9IiwiYmQtdGlja2V0LWd1YXJkLXdlYi12ZXJzaW9uIjoyfQ%3D%3D; home_can_add_dy_2_desktop=%221%22; odin_tt=752a8007f9aa9b1959168205229802a38ce8a1167929c1960c2ab8e733fa4beeed5475863dc1a7f18b5566113a43b02c76c6d08103c048068e73fa0e2ea1f049e402ceb7f7877a4afc245ca9e1903f98; biz_trace_id=33430f82; bd_ticket_guard_client_data_v2=eyJyZWVfcHVibGljX2tleSI6IkJHVTNVdUhVa3BnQmhTTUNVcncrQ1VlWDdHbzBRemhmYUN2Zm90N2hQM2M4SDVLWXZsdHZ0T1ZWNlpIU0ZGMUY4ZFlUQk5lbE9nOFhjL2NMcStuU2M1QT0iLCJ0c19zaWduIjoidHMuMi4xNmQzNWNkMzBmOWFjNDZjN2U1OTg5YmVhNmFiNjk2M2MyMzZlYzA5NDFjZDdiMzUxNTZlMjc3MDE4NTYxYTE0YzRmYmU4N2QyMzE5Y2YwNTMxODYyNGNlZGExNDkxMWNhNDA2ZGVkYmViZWRkYjJlMzBmY2U4ZDRmYTAyNTc1ZCIsInJlcV9jb250ZW50Ijoic2VjX3RzIiwicmVxX3NpZ24iOiJ1bWttMWwzUm5Zc1JJK29PdCtRUmNYbExDK2pXV1JpKysyVlI5WTQzODVBPSIsInNlY190cyI6IiNGYWdzMkRQWTFhdm5Fa2YybTRYL0RmendDaFppc2lKa3U3NXliNFpEUVVqcDVKeWpVNUFneXlBN081SjkifQ%3D%3D; IsDouyinActive=false"

OUTPUT_DIR = Path("downloads")
OUTPUT_DIR.mkdir(exist_ok=True)

# 是否使用有头浏览器（True=显示窗口，更容易过验证；False=无头）
HEADLESS = False

# ffmpeg 可执行文件路径（留空 "" 则自动从系统 PATH 查找）
FFMPEG_PATH = ""
# ================================================

# 抖音视频 CDN 域名特征（抖音用 MP4 直链，而非 m3u8）
DOUYIN_VIDEO_HOSTS = [
    "douyinvod.com",
    "byteaccdn.com",
    "pstatp.com",
    "amemv.com",
    "tiktokv.com",
    "tiktokcdn.com",
]

# 需要拦截响应的抖音 API 路径
# 注意：web 端 modal 实际调用 /aweme/v1/web/aweme/detail/（含 /web/ 前缀）
DOUYIN_API_PATHS = [
    "/aweme/v1/web/aweme/detail/",   # web modal 的真实端点
    "/aweme/v1/feed/",
    "/aweme/v2/feed/",
    "/aweme/v1/aweme/detail/",
    "/aweme/v1/web/comment/",
    "/api/item_info/",
    "/api/aweme/iteminfo/",
]


def sanitize_filename(name: str) -> str:
    name = name.split("?")[0]
    name = re.sub(r'[\\/:*?"<>|]', '_', name)
    name = name.strip(". ")
    return name or "video"


def parse_cookie_string(cookie_str: str) -> list[dict]:
    cookies = []
    domain = urlparse(TARGET_URL).netloc
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" in part:
            name, _, value = part.partition("=")
            cookies.append({
                "name": name.strip(),
                "value": value.strip(),
                "domain": domain,
                "path": "/",
            })
    return cookies


def extract_aweme_id(url: str) -> str | None:
    """从 URL 中提取视频 ID（modal_id 参数或路径中的数字 ID）"""
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    if "modal_id" in params:
        return params["modal_id"][0]
    m = re.search(r"/video/(\d+)", parsed.path)
    if m:
        return m.group(1)
    return None


def extract_urls_from_aweme_json(data) -> list[str]:
    """
    从抖音 API 返回的 aweme JSON 中提取视频播放 URL。
    抖音响应结构: data.aweme_list[].video.play_addr.url_list[]
    """
    urls = []

    def process_aweme(aweme: dict):
        if not isinstance(aweme, dict):
            return
        video = aweme.get("video", {})
        if not isinstance(video, dict):
            return
        # play_addr_h264 优先（无水印，H.264 兼容性好），其次 play_addr，最后 download_addr
        for addr_key in ("play_addr_h264", "play_addr", "download_addr"):
            addr = video.get(addr_key)
            if isinstance(addr, dict):
                for url in addr.get("url_list", []):
                    if url and isinstance(url, str) and url.startswith("http"):
                        urls.append(url)
                if urls:
                    return  # 找到第一个有效来源后停止

    def scan(obj):
        if isinstance(obj, dict):
            # 识别 aweme 对象（含 video 字段）
            if "video" in obj and isinstance(obj.get("video"), dict):
                process_aweme(obj)
            else:
                for v in obj.values():
                    scan(v)
        elif isinstance(obj, list):
            for item in obj:
                scan(item)

    scan(data)
    return urls


async def wait_for_verification(page, timeout=90):
    """
    等待验证通过。
    原脚本只等 Cloudflare，但抖音还有自己的 nocaptcha（rmc-nocaptcha）验证。
    """
    print("  ▶ 等待页面验证通过...")
    start = time.time()
    while time.time() - start < timeout:
        frames = page.frames
        challenge_frames = [
            f for f in frames if any(kw in f.url for kw in [
                "challenges.cloudflare.com",
                "nocaptcha",
                "verifycenter",
            ])
        ]
        if not challenge_frames:
            print("  ✓ 验证已通过")
            return True
        elapsed = int(time.time() - start)
        if elapsed % 5 == 0:
            print(f"  ▶ 等待验证 {elapsed}s... (验证帧: {len(challenge_frames)})")
        await asyncio.sleep(0.5)
    print("  ⚠ 验证超时，尝试继续...")
    return False


async def get_video_url():
    """
    主流程：启动浏览器 → 注入 Cookie → 过验证 → 提取视频直链。

    三层提取策略：
      1. 拦截 API 响应（/aweme/v1/feed/ 等），从 JSON 解析视频 URL（最可靠）
      2. 拦截视频 CDN 请求（douyinvod.com 等域名）
      3. 在页面内直接 fetch API（兜底）
    """
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=HEADLESS,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-web-security",
                "--disable-features=IsolateOrigins,site-per-process",
            ]
        )

        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
            java_script_enabled=True,
            extra_http_headers={
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            }
        )

        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
            Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN','zh','en']});
            window.chrome = {runtime: {}};
        """)

        if COOKIE_STRING.strip():
            cookies = parse_cookie_string(COOKIE_STRING)
            await context.add_cookies(cookies)
            print(f"  ✓ 已注入 {len(cookies)} 个 Cookie")
        else:
            print("  ⚠ 未配置 Cookie，以游客身份访问")

        page = await context.new_page()

        video_urls_from_api = []
        # 分阶段收集 CDN 请求：页面自动加载的预览片段在播放前出现，
        # 真正的完整视频 URL 在触发播放后才出现
        cdn_before_play: list[str] = []
        cdn_after_play:  list[str] = []
        play_triggered = False

        # 策略1: 拦截视频 CDN 请求（抖音用 MP4，不用 m3u8）
        def on_request(req):
            u = req.url
            if any(host in u for host in DOUYIN_VIDEO_HOSTS) \
                    and "challenge" not in u and "captcha" not in u:
                if play_triggered:
                    cdn_after_play.append(u)
                    print(f"  ✓ [CDN-播放后] {u[:100]}")
                else:
                    cdn_before_play.append(u)
                    print(f"  ℹ [CDN-预览] {u[:100]}")

        # 策略2: 拦截 API 响应，解析 JSON 中的视频 URL
        # 匹配规则：已知路径 OR douyin.com 下任何含 "aweme" 且含 "detail"/"feed" 的请求
        async def on_response(response):
            u = response.url
            try:
                is_aweme_api = any(path in u for path in DOUYIN_API_PATHS) or (
                    "douyin.com" in u and "aweme" in u
                    and any(kw in u for kw in ("detail", "feed"))
                )
                if not is_aweme_api:
                    return
                ct = response.headers.get("content-type", "")
                if "json" not in ct:
                    return
                data = await response.json()
                urls = extract_urls_from_aweme_json(data)
                if urls:
                    print(f"  ✓ [API响应] 从 {u[:80]} 提取到 {len(urls)} 个视频URL")
                    video_urls_from_api.extend(urls)
            except Exception:
                pass

        page.on("request", on_request)
        page.on("response", on_response)

        print(f"\n  ▶ 加载页面: {TARGET_URL}")
        try:
            await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            print(f"  ⚠ 页面加载警告: {e}")

        # 等待验证（Cloudflare + 抖音 nocaptcha）
        await wait_for_verification(page, timeout=90)
        await asyncio.sleep(3)

        # 触发视频播放，让浏览器发出完整视频的 CDN 请求
        print("  ▶ 触发视频播放...")
        play_triggered = True
        try:
            await page.evaluate("""
                () => {
                    const videos = document.querySelectorAll('video');
                    videos.forEach(v => { v.muted = true; v.play(); });
                }
            """)
            # 也尝试点击播放按钮（有些播放器需要交互事件）
            for sel in ("[class*='play']", ".xgplayer-play", ".video-player", "video"):
                try:
                    el = await page.query_selector(sel)
                    if el:
                        await el.click()
                        break
                except Exception:
                    pass
        except Exception:
            pass

        # 等待完整视频的 CDN 请求和 API 响应（比预览加载慢）
        await asyncio.sleep(8)

        all_cookies = await context.cookies()
        session_cookie_header = "; ".join(
            f"{c['name']}={c['value']}" for c in all_cookies
        )
        print(f"  ✓ 已提取 {len(all_cookies)} 个会话 Cookie")

        # 优先使用 API 响应中解析的 URL（质量最好，通常无水印）
        if video_urls_from_api:
            print(f"\n  ✓ API 响应提取到 {len(video_urls_from_api)} 个视频URL")
            await browser.close()
            return video_urls_from_api[0], session_cookie_header

        # 其次使用播放后捕获的 CDN URL（完整视频），忽略播放前的预览片段
        if cdn_after_play:
            print(f"\n  ✓ 播放后 CDN 捕获到 {len(cdn_after_play)} 个视频URL（完整视频）")
            await browser.close()
            return cdn_after_play[0], session_cookie_header

        if cdn_before_play:
            print(f"\n  ⚠ 只捕获到播放前的预览 URL（可能仅为片段），共 {len(cdn_before_play)} 个")
            await browser.close()
            return cdn_before_play[0], session_cookie_header

        # 策略3: 在页面内直接 fetch 抖音 API（兜底，依赖浏览器 Cookie 自动携带签名）
        aweme_id = extract_aweme_id(TARGET_URL)
        if aweme_id:
            print(f"  ▶ 兜底：页面内 fetch API，aweme_id={aweme_id}")
            # 同时尝试 web detail 端点和 feed 端点
            for api_url in [
                f"https://www.douyin.com/aweme/v1/web/aweme/detail/?aweme_id={aweme_id}&device_platform=webapp&aid=6383",
                f"https://www.douyin.com/aweme/v1/feed/?aweme_id={aweme_id}&version_code=170400&app_name=douyin_web",
            ]:
                try:
                    resp_text = await page.evaluate(f"""
                        async () => {{
                            try {{
                                const r = await fetch('{api_url}', {{
                                    credentials: 'include',
                                    headers: {{
                                        'Accept': 'application/json, text/plain, */*',
                                        'Referer': '{TARGET_URL}',
                                    }}
                                }});
                                return await r.text();
                            }} catch(e) {{ return ''; }}
                        }}
                    """)
                    if resp_text:
                        data = json.loads(resp_text)
                        urls = extract_urls_from_aweme_json(data)
                        if urls:
                            print(f"  ✓ 页面内 API 提取到 {len(urls)} 个视频URL")
                            await browser.close()
                            return urls[0], session_cookie_header
                except Exception as e:
                    print(f"  ⚠ 页面内 API 查询失败 ({api_url[:60]}): {e}")

        # 截图诊断
        shot_path = OUTPUT_DIR / "debug_screenshot.png"
        await page.screenshot(path=str(shot_path))
        print(f"  ⚠ 未找到视频URL，截图已保存: {shot_path}")
        for i, frame in enumerate(page.frames):
            print(f"  frame[{i}] url={frame.url}")

        await browser.close()
        return None, session_cookie_header


# ==================== 下载 ====================

def _resolve_ffmpeg() -> str | None:
    if FFMPEG_PATH:
        for candidate in [FFMPEG_PATH, FFMPEG_PATH + ".exe"]:
            if Path(candidate).exists():
                return candidate
        print(f"  ⚠ FFMPEG_PATH 路径不存在: {FFMPEG_PATH}")
    return shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")


def download_mp4_direct(url: str, output: Path, cookie_header: str = "") -> bool:
    """流式 HTTP 下载 MP4，带进度条"""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.douyin.com/",
    }
    if cookie_header:
        headers["Cookie"] = cookie_header

    try:
        r = requests.get(url, headers=headers, stream=True, timeout=30)
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        with open(output, "wb") as f:
            with tqdm(total=total, unit="B", unit_scale=True, desc=output.name) as pbar:
                for chunk in r.iter_content(chunk_size=65536):
                    f.write(chunk)
                    pbar.update(len(chunk))
        print(f"  ✓ 下载完成: {output}")
        return True
    except Exception as e:
        print(f"  ✗ 直接下载失败: {e}")
        return False


def download_via_ffmpeg(url: str, output: Path, ffmpeg_bin: str, cookie_header: str = "") -> bool:
    """使用 ffmpeg 下载（支持 m3u8 流 + MP4 直链）"""
    headers_arg = (
        "Referer: https://www.douyin.com/\r\n"
        "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36\r\n"
    )
    if cookie_header:
        headers_arg += f"Cookie: {cookie_header}\r\n"

    print(f"  ▶ ffmpeg 下载 → {output.name}")
    cmd = [ffmpeg_bin, "-y", "-headers", headers_arg, "-i", url, "-c", "copy"]
    if ".m3u8" in url:
        cmd += ["-bsf:a", "aac_adtstoasc"]
    cmd.append(str(output))

    result = subprocess.run(cmd)
    return result.returncode == 0


def download_video(video_url: str, output_name: str, cookie_header: str = "") -> Path:
    """
    下载视频。
    优先用 ffmpeg（同时支持 m3u8 和 MP4 直链）；
    ffmpeg 不可用时直接 HTTP 流式下载 MP4。
    """
    is_m3u8 = ".m3u8" in video_url
    # 从 URL 路径推断扩展名，默认 mp4
    url_path = video_url.split("?")[0]
    suffix = url_path.rsplit(".", 1)[-1] if "." in url_path.rsplit("/", 1)[-1] else "mp4"
    ext = "mp4" if is_m3u8 else suffix
    output = OUTPUT_DIR / f"{output_name}.{ext}"

    ffmpeg_bin = _resolve_ffmpeg()
    if ffmpeg_bin:
        print(f"  ✓ 使用 ffmpeg: {ffmpeg_bin}")
        if download_via_ffmpeg(video_url, output, ffmpeg_bin, cookie_header):
            return output
        print("  ✗ ffmpeg 下载失败，尝试直接 HTTP 下载...")

    if is_m3u8:
        print("  ✗ m3u8 流需要 ffmpeg，请安装: winget install ffmpeg  或  brew install ffmpeg")
    else:
        download_mp4_direct(video_url, output, cookie_header)

    return output


# ==================== 主程序 ====================

async def main():
    print("=" * 60)
    print("  抖音视频下载器 v2")
    print("=" * 60)

    video_url, cookie_header = await get_video_url()

    if not video_url:
        print("\n  ✗ 无法提取视频 URL")
        print("  建议：")
        print("  1. 设置 HEADLESS=False 手动通过验证")
        print("  2. 检查并更新 COOKIE_STRING")
        print("  3. 查看 downloads/debug_screenshot.png 截图")
        return

    print(f"\n  ✓ 视频 URL: {video_url[:120]}")

    aweme_id = extract_aweme_id(TARGET_URL)
    video_name = sanitize_filename(aweme_id or TARGET_URL.rstrip("/").split("/")[-1])
    print(f"  ✓ 输出文件名: {video_name}")

    output = download_video(video_url, video_name, cookie_header)
    print(f"\n  完成: {output}")


if __name__ == "__main__":
    asyncio.run(main())
