#!/usr/bin/env python3
"""
supjav.com 视频下载器 — 最终版（整合17次迭代经验）

【17次迭代问题总结与解决方案】
────────────────────────────────────────────────────────────────
v1  问题：初版，CF bypass 仅用 stealth，成功率低
v2  问题：playwright-stealth v1/v2 API 不兼容（stealth_async vs Stealth 类）
        解决：同时兼容两套 API
v3  问题：CF cookie 失效后需手动操作，无法复用浏览器 F12 的 Cookie
        解决：增加 BROWSER_COOKIE_FILE 手动 Cookie 文件加载
v4  问题：Cookie 文件格式不统一（纯文本/JSON数组/脚本缓存三种）
        解决：自动识别多种格式
v5  问题：单个 Cookie 文件不够灵活
        解决：改为 BROWSER_COOKIE_FILES 列表，按优先级扫描
v6  问题：重复保存（v6=v7 内容相同，可能是误操作重存）
v8  问题：supjav.com 有多条视频线路（DS/TV/JPA/ST），默认线路无视频流
        解决：新增 VIDEO_SERVER 配置，在播放前点击指定线路按钮
        问题：ffmpeg 路径硬编码，跨平台不可用
        解决：新增 FFMPEG_PATH 配置项
v9  问题：无 ffmpeg 环境时完全失败
        解决：增加纯 Python m3u8 下载器（逐 ts 分片合并），不依赖 ffmpeg
v10 问题：ffmpeg 查找失败不给用户反馈
        解决：_find_ffmpeg() 自动搜索 PATH + Windows 常见路径
v11 问题：无扩展名的 m3u8 URL（某些 CDN）无法被正则捕获
        解决：检测响应 Content-Type 包含 mpegurl 也认定为 m3u8
v12 问题：FlareSolverr 返回空响应时 json() 崩溃
        解决：增加空响应检测和 JSONDecodeError 捕获
v13 问题：播放器 iframe（如 playmogo.com）有独立 CF 验证，脚本卡住
        解决：新增 PLAYER_CF_WAIT_SEC / BROWSER_HEADLESS 配置，
              专门等待播放器 CF 自动通过
v14 问题：click_server_button 选择器覆盖不足，DS 按钮点击失败
        解决：增加更多 CSS 选择器变体，增加调试输出
        问题：点击播放后才启动 m3u8 监听，错过 VideoJS 预加载请求
        解决：在点击服务器按钮之前重置 m3u8 监听 task
v15 问题：播放器 iframe URL 检测误选广告 iframe
        解决：增加 _AD_IFRAME_KEYWORDS 黑名单过滤
        问题：lk1.supremejav.com 是会话绑定包装页（非真正播放器），
              直接加载无法提取 m3u8
        解决：_find_player_iframe_url() 等待含 /e/ /embed 等路径的
              真正播放器 iframe 出现（最多15秒）
v16 问题：响应体为 #EXTM3U 但无 .m3u8 扩展名，网络拦截漏掉
        解决：对候选 URL 读取响应 body 检测 #EXTM3U 开头
        问题：CDN token 约7秒过期，ffmpeg 重新请求 manifest 失败
        解决：把 m3u8 内容写到本地文件（相对 URL 转绝对），传给 ffmpeg 读本地文件
v17 问题：有时已知播放器 URL（浏览器手动拿到），仍需走 CF+主页逻辑
        解决：新增 PLAYER_URL 快捷路径，填写后直接加载播放器跳过主页

【DS 服务器完整提取流程】
  1. 用 cf_clearance Cookie 打开 supjav.com/132824.html
  2. 点击 "DS" 线路按钮
  3. 等待 DS 播放器 iframe（playmogo.com/e/...）加载
  4. 若 playmogo.com 有 CF 验证，等待 stealth 自动通过
  5. 触发 video.play() 或点击播放按钮
  6. 从网络请求 / video.currentSrc / HTML 中提取 m3u8 URL
  7. ffmpeg 下载（或纯 Python 备用下载器）

依赖安装：
  pip install playwright playwright-stealth httpx
  playwright install chromium   # firefox 在受限环境可能下载失败，chromium 优先
  # ffmpeg: apt install ffmpeg  或  winget install ffmpeg
"""

import asyncio
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse, urljoin

import httpx
from playwright.async_api import async_playwright, Page, BrowserContext

# ╔══════════════════════════════════════════════════╗
# ║               用户配置区（只需改这里）              ║
# ╚══════════════════════════════════════════════════╝

TARGET_URL = "https://supjav.com/132824.html"
DOWNLOAD_DIR = Path("downloads")
COOKIE_CACHE_FILE = Path("cookie_cache.json")

# ── 快捷路径：如已从浏览器 F12 拿到播放器 embed URL，填在这里 ──────────────
# 例如: "https://playmogo.com/e/abcdef123456"
# 填写后跳过 Cookie 获取和主页逻辑，直接加载播放器
PLAYER_URL = ""

# ── Cookie 文件（按顺序扫描，找到第一个含 cf_clearance 的即使用）────────────
BROWSER_COOKIE_FILES = [
    "cf_cookies.json",    # 浏览器 F12 手动导出（最高优先级）
    "cookie_cache.json",  # 脚本自动缓存
    "cookies.txt",        # 备用纯文本
]

# ── 视频服务器选择 ────────────────────────────────────────────────────────────
# supjav.com 页面上有多个线路按钮：DS / TV / JPA / ST 等
# DS 服务器使用 playmogo.com 播放器，质量最稳定
# 留空 "" 使用页面默认线路（通常是第一个）
VIDEO_SERVER = "DS"

# ── Cloudflare 绕过方案 A ─────────────────────────────────────────────────────
STEALTH_HEADLESS = True     # False = 显示浏览器窗口（调试用）
STEALTH_WAIT_SEC = 25       # 等待 CF 自动放行的秒数

# ── 播放器 CF 验证 ────────────────────────────────────────────────────────────
PLAYER_CF_WAIT_SEC = 30     # 播放器 iframe CF 验证等待秒数
BROWSER_HEADLESS = True     # 主浏览器无头模式（False = 可见窗口，方便手动过 CF）

# ── FlareSolverr（方案 B，需本地 Docker）────────────────────────────────────
# 启动: docker run -d -p 8191:8191 ghcr.io/flaresolverr/flaresolverr:latest
FLARESOLVERR_URL = "http://localhost:8191/v1"
FLARESOLVERR_TIMEOUT = 60

# ── CapSolver（方案 C，付费 API，最可靠）────────────────────────────────────
CAPSOLVER_API_KEY = ""

# ── ffmpeg 路径 ───────────────────────────────────────────────────────────────
# 留空 "" = 自动查找 PATH
# Windows 示例: r"D:\Tool\ffmpeg\bin\ffmpeg.exe"
# Linux/Mac:    "/usr/bin/ffmpeg"
FFMPEG_PATH = ""

# ── 通用 ──────────────────────────────────────────────────────────────────────
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) Gecko/20100101 Firefox/135.0"
M3U8_TIMEOUT = 60

# ╔══════════════════════════════════════════════════╗
# ║                 配置结束                          ║
# ╚══════════════════════════════════════════════════╝

M3U8_RE = re.compile(r'https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*', re.IGNORECASE)
DOMAIN = urlparse(TARGET_URL).netloc

# 广告/追踪 iframe 黑名单（防止误点广告视频）
_AD_IFRAME_KW = (
    "mayzaent.", "googlesyndication.", "doubleclick.", "adnxs.",
    "amazon-adsystem.", "adtech.", "advertising.", "tracker.",
    "tracking.", "analytics.", "metrics.", "pixel.", "campaign",
    "banner", "widget",
)


# ─────────────────────────────────────────────────────────────────────────────
# playwright-stealth 兼容加载（v1: stealth_async / v2: Stealth 类）
# ─────────────────────────────────────────────────────────────────────────────

def _load_stealth_fn():
    """返回 stealth 异步应用函数，不可用返回 None。v1/v2 均兼容。"""
    try:
        from playwright_stealth import stealth_async
        return stealth_async
    except ImportError:
        pass
    try:
        from playwright_stealth import Stealth
        s = Stealth()
        for method in ["apply_stealth_async", "use_async", "async_stealth", "__call__"]:
            fn = getattr(s, method, None)
            if callable(fn):
                return fn
    except ImportError:
        pass
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Cookie 工具
# ─────────────────────────────────────────────────────────────────────────────

def _parse_cookie_str(s: str, domain: str) -> list[dict]:
    """'name=val; name2=val2' → Cookie 对象列表"""
    result = []
    for part in s.split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        name, _, value = part.partition("=")
        result.append({
            "name": name.strip(), "value": value.strip(),
            "domain": domain, "path": "/",
            "httpOnly": False, "secure": True, "sameSite": "None",
        })
    return result


def _normalize_cookies(raw: list[dict], domain: str) -> list[dict]:
    """统一 Cookie 格式（Playwright context.add_cookies 接受的格式）"""
    result = []
    for c in raw:
        result.append({
            "name": c.get("name", ""),
            "value": c.get("value", ""),
            "domain": c.get("domain", domain).lstrip("."),
            "path": c.get("path", "/"),
            "httpOnly": c.get("httpOnly", False),
            "secure": c.get("secure", True),
            "sameSite": c.get("sameSite", "None"),
        })
    return result


def has_cf_clearance(cookies: list[dict]) -> bool:
    return any(c.get("name") == "cf_clearance" for c in cookies)


def load_cookies_from_file(path) -> list[dict] | None:
    """
    自动识别四种 Cookie 文件格式：
      1. 纯文本: cf_clearance=xxx; _cfuvid=xxx
      2. JSON 字符串（带外层引号）: "cf_clearance=xxx; ..."
      3. JSON 数组: [{"name":"cf_clearance","value":"xxx",...}]
      4. 脚本缓存: {"saved_at":..., "cookies":[...]}
    """
    p = Path(path)
    if not p.exists():
        return None
    content = p.read_text(encoding="utf-8").strip()
    if not content:
        return None

    try:
        data = json.loads(content)
        if isinstance(data, str):
            # 格式 2：剥掉外层引号后当纯文本处理
            content = data
        elif isinstance(data, dict) and "cookies" in data:
            # 格式 4：脚本缓存，检查过期
            remaining = 3000 - (time.time() - data.get("saved_at", 0))
            if remaining <= 0:
                print(f"  ⚠ {p.name} 缓存已过期")
                return None
            cookies = data["cookies"]
            print(f"  ✓ 从 {p.name} 加载 {len(cookies)} 个 Cookie（缓存，剩余 {int(remaining/60)} 分钟）")
            return cookies
        elif isinstance(data, list):
            # 格式 3：JSON 数组
            cookies = _normalize_cookies(data, DOMAIN)
            print(f"  ✓ 从 {p.name} 加载 {len(cookies)} 个 Cookie（JSON 数组）")
            return cookies
    except json.JSONDecodeError:
        pass

    # 格式 1/2：纯文本
    if "=" in content:
        cookies = _parse_cookie_str(content, DOMAIN)
        if cookies:
            print(f"  ✓ 从 {p.name} 加载 {len(cookies)} 个 Cookie（纯文本）")
            return cookies

    print(f"  ⚠ {p.name} 格式无法识别: {content[:80]}")
    return None


def save_cookies(cookies: list[dict]):
    payload = {"saved_at": time.time(), "cookies": cookies}
    COOKIE_CACHE_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    print(f"  ✓ Cookie 已缓存到 {COOKIE_CACHE_FILE}")


# ─────────────────────────────────────────────────────────────────────────────
# 方案 A: playwright-stealth（免费，无需外部服务）
# ─────────────────────────────────────────────────────────────────────────────

async def _stealth_attempt(apply_stealth, engine_attr: str, extra_args: list, url: str) -> list[dict] | None:
    import random
    async with async_playwright() as p:
        engine = getattr(p, engine_attr)
        launch_kw: dict = {"headless": STEALTH_HEADLESS}
        if extra_args:
            launch_kw["args"] = extra_args
        browser = await engine.launch(**launch_kw)
        context = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1920, "height": 1080},
            locale="zh-CN",
        )
        page = await context.new_page()
        try:
            await apply_stealth(page)
        except Exception as e:
            print(f"  ⚠ stealth 应用失败（继续）: {e}")

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception:
            pass

        # 模拟人类鼠标行为，有助于通过行为检测
        try:
            for _ in range(5):
                await page.mouse.move(
                    random.randint(200, 1600), random.randint(100, 800),
                    steps=random.randint(5, 15),
                )
                await asyncio.sleep(random.uniform(0.3, 0.8))
        except Exception:
            pass

        print(f"  ▶ 等待 CF 验证最多 {STEALTH_WAIT_SEC} 秒...")
        for i in range(STEALTH_WAIT_SEC):
            await asyncio.sleep(1)
            raw_now = await context.cookies()
            if any(c.get("name") == "cf_clearance" for c in raw_now):
                print(f"  ✓ 第 {i+1} 秒获得 cf_clearance")
                break
            if i % 5 == 4:
                try:
                    await page.mouse.move(random.randint(300, 1500), random.randint(200, 700), steps=10)
                except Exception:
                    pass

        raw = await context.cookies()
        await browser.close()

    cookies = _normalize_cookies(raw, DOMAIN)
    if has_cf_clearance(cookies):
        print(f"  ✓ 方案A ({engine_attr}) 获取 cf_clearance 成功")
        return cookies
    return None


async def get_cookies_via_stealth(url: str) -> list[dict] | None:
    print("\n  [方案A] playwright-stealth 尝试获取 Cookie...")
    apply_stealth = _load_stealth_fn()
    if not apply_stealth:
        print("  ✗ playwright-stealth 未安装，跳过")
        print("    安装: pip install playwright-stealth")
        return None
    print("  ✓ playwright-stealth 已加载")

    # 先尝试 Chromium（Firefox 在某些受限环境无法下载）
    for engine, extra in [
        ("chromium", ["--no-sandbox", "--disable-blink-features=AutomationControlled",
                      "--disable-dev-shm-usage", "--disable-automation",
                      "--window-size=1920,1080"]),
        ("firefox", []),
    ]:
        print(f"  ▶ 尝试 {engine}...")
        try:
            result = await _stealth_attempt(apply_stealth, engine, extra, url)
            if result:
                return result
        except Exception as e:
            print(f"  ⚠ {engine} 失败: {e}")

    print("  ✗ 方案A 未获得 cf_clearance")
    return None


# ─────────────────────────────────────────────────────────────────────────────
# 方案 B: FlareSolverr（需本地 Docker）
# ─────────────────────────────────────────────────────────────────────────────

async def get_cookies_via_flaresolverr(url: str) -> list[dict] | None:
    print(f"\n  [方案B] FlareSolverr ({FLARESOLVERR_URL}) 尝试获取 Cookie...")
    try:
        async with httpx.AsyncClient(timeout=FLARESOLVERR_TIMEOUT + 10) as client:
            resp = await client.post(
                FLARESOLVERR_URL,
                json={"cmd": "request.get", "url": url,
                      "maxTimeout": FLARESOLVERR_TIMEOUT * 1000},
            )
        raw_text = resp.text.strip()
        if not raw_text:
            print(f"  ✗ FlareSolverr 返回空响应 (HTTP {resp.status_code})")
            return None
        data = resp.json()
    except httpx.ConnectError:
        print(f"  ✗ 无法连接 FlareSolverr（8191 端口未监听）")
        print(f"    启动: docker run -d -p 8191:8191 ghcr.io/flaresolverr/flaresolverr:latest")
        return None
    except json.JSONDecodeError as e:
        print(f"  ✗ FlareSolverr 响应非 JSON: {e}  原始(前200): {resp.text[:200]!r}")
        return None
    except Exception as e:
        print(f"  ✗ FlareSolverr 异常: {type(e).__name__}: {e}")
        return None

    if data.get("status") != "ok":
        print(f"  ✗ FlareSolverr 错误: {data.get('message', data)}")
        return None

    raw_cookies = data.get("solution", {}).get("cookies", [])
    cookies = _normalize_cookies(raw_cookies, DOMAIN)
    if has_cf_clearance(cookies):
        print("  ✓ 方案B 成功获取 cf_clearance")
        return cookies
    print("  ✗ 方案B 未获得 cf_clearance")
    return None


# ─────────────────────────────────────────────────────────────────────────────
# 方案 C: CapSolver（付费 API，最可靠）
# ─────────────────────────────────────────────────────────────────────────────

async def _extract_turnstile_sitekey(url: str) -> str | None:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(user_agent=USER_AGENT)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
        except Exception:
            pass
        await asyncio.sleep(3)
        sitekey = await page.evaluate("""() => {
            const el = document.querySelector('[data-sitekey]');
            if (el) return el.getAttribute('data-sitekey');
            for (const f of document.querySelectorAll('iframe')) {
                const m = (f.src || '').match(/sitekey=([^&]+)/);
                if (m) return m[1];
            }
            try { const p = window.__CF$cv$params; if (p && p.k) return p.k; } catch(e) {}
            return null;
        }""")
        if not sitekey:
            html = await page.content()
            m = re.search(r'sitekey["\s:=\']+([0-9a-zA-Z_-]{20,})', html)
            if m:
                sitekey = m.group(1)
        await browser.close()
    return sitekey


async def get_cookies_via_capsolver(url: str) -> list[dict] | None:
    if not CAPSOLVER_API_KEY:
        print("\n  [方案C] 未配置 CAPSOLVER_API_KEY，跳过")
        return None
    print(f"\n  [方案C] CapSolver API 尝试获取 Cookie...")
    sitekey = await _extract_turnstile_sitekey(url)
    if not sitekey:
        print("  ✗ 未找到 Turnstile sitekey")
        return None
    print(f"  ✓ sitekey: {sitekey}")

    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post("https://api.capsolver.com/createTask", json={
            "clientKey": CAPSOLVER_API_KEY,
            "task": {"type": "AntiTurnstileTaskProxyLess",
                     "websiteURL": url, "websiteKey": sitekey},
        })
        res = r.json()
        if res.get("errorId"):
            print(f"  ✗ 创建任务失败: {res.get('errorDescription')}")
            return None
        task_id = res["taskId"]
        print(f"  ▶ 任务 {task_id}，轮询中...")
        for _ in range(30):
            await asyncio.sleep(3)
            poll = (await client.post("https://api.capsolver.com/getTaskResult",
                                      json={"clientKey": CAPSOLVER_API_KEY, "taskId": task_id})).json()
            if poll.get("status") == "ready":
                token = poll["solution"]["token"]
                break
            if poll.get("status") == "failed" or poll.get("errorId"):
                print(f"  ✗ 任务失败: {poll.get('errorDescription')}")
                return None
        else:
            print("  ✗ CapSolver 超时")
            return None

    # 提交 token 获取 cf_clearance
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent=USER_AGENT)
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
        except Exception:
            pass
        await page.evaluate("""(t) => {
            document.querySelectorAll('input[name="cf-turnstile-response"],input[name="g-recaptcha-response"]')
                    .forEach(i => i.value = t);
            const f = document.querySelector('form#challenge-form');
            if (f) f.submit();
        }""", token)
        await asyncio.sleep(5)
        raw = await context.cookies()
        await browser.close()

    cookies = _normalize_cookies(raw, DOMAIN)
    if has_cf_clearance(cookies):
        print("  ✓ 方案C 成功获取 cf_clearance")
        return cookies
    print("  ✗ 方案C 未获得 cf_clearance")
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Cookie 获取主入口
# ─────────────────────────────────────────────────────────────────────────────

async def acquire_cookies() -> list[dict]:
    """
    优先级：
      0. BROWSER_COOKIE_FILES 中的手动/缓存文件
      A. playwright-stealth 自动获取
      B. FlareSolverr 本地服务
      C. CapSolver 付费 API
    """
    # 0. 文件扫描
    for fname in BROWSER_COOKIE_FILES:
        c = load_cookies_from_file(fname)
        if c and has_cf_clearance(c):
            print(f"  ✓ 使用 {fname} 中的 Cookie")
            return c
        elif c:
            print(f"  ⚠ {fname} 无 cf_clearance，继续...")

    print("\n  ℹ 开始自动获取 Cookie...")

    for fn in (get_cookies_via_stealth, get_cookies_via_flaresolverr, get_cookies_via_capsolver):
        c = await fn(TARGET_URL)
        if c:
            save_cookies(c)
            return c

    print("\n  ✗ 三种方案均失败")
    print("  解决方法：")
    print("  1. 浏览器访问目标页 → F12 → 网络 → 复制 Cookie 行 → 存入 cf_cookies.json")
    print("  2. 启动 FlareSolverr:  docker run -d -p 8191:8191 ghcr.io/flaresolverr/flaresolverr:latest")
    print("  3. 配置 CAPSOLVER_API_KEY（付费，最可靠）")
    sys.exit(1)


# ─────────────────────────────────────────────────────────────────────────────
# 播放器工具函数
# ─────────────────────────────────────────────────────────────────────────────

async def wait_for_player_cf(page: Page, timeout: int = PLAYER_CF_WAIT_SEC) -> bool:
    """等待播放器 iframe 内 CF 验证自动通过。"""
    cf_frames = [f for f in page.frames if "challenges.cloudflare.com" in (f.url or "")]
    if not cf_frames:
        return True
    print(f"\n  ⚠ 播放器检测到 CF 验证，等待最多 {timeout} 秒...")
    print(f"    （如长时间卡住，设置 BROWSER_HEADLESS=False 手动操作）")
    for i in range(timeout):
        await asyncio.sleep(1)
        if not any("challenges.cloudflare.com" in (f.url or "") for f in page.frames):
            print(f"  ✓ 第 {i+1} 秒 CF 验证通过")
            return True
        if (i + 1) % 5 == 0:
            print(f"  ⏳ {i+1}/{timeout} 秒...")
    print(f"  ✗ CF 验证 {timeout} 秒内未通过")
    return False


async def click_server_button(page: Page, server_name: str) -> bool:
    """点击视频服务器切换按钮（DS/TV/ST/JPA 等）。"""
    if not server_name:
        return False
    selectors = [
        f"button:text-is('{server_name}')",
        f"a:text-is('{server_name}')",
        f"li:text-is('{server_name}')",
        f"span:text-is('{server_name}')",
        f"div:text-is('{server_name}')",
        f"[class*='server']:text-is('{server_name}')",
        f"[class*='source']:text-is('{server_name}')",
        f":text('{server_name}')",
    ]
    for sel in selectors:
        try:
            el = page.locator(sel).first
            if await el.count() > 0:
                await el.click(timeout=3000)
                print(f"  ✓ 已切换到服务器: {server_name}")
                await asyncio.sleep(5)   # 等待播放器 iframe 重载
                return True
        except Exception:
            continue

    try:
        texts = await page.evaluate("""() => {
            return [...document.querySelectorAll('button,a,li,[class*="server"],[class*="source"]')]
                   .map(e => e.textContent.trim()).filter(t => t && t.length < 20);
        }""")
        print(f"  ⚠ 未找到 '{server_name}' 按钮，页面按钮: {list(dict.fromkeys(texts))[:20]}")
    except Exception:
        print(f"  ⚠ 未找到 '{server_name}' 按钮")
    return False


# ── /e/ /embed 模式 iframe 识别（避免误选 lk1 包装页 或 广告 iframe）────────
_PLAYER_PATTERNS = ("/e/", "/embed", "/player/", "/hls/", "stream.")

async def _find_player_iframe_url(page: Page, timeout: int = 15) -> str | None:
    def _candidates():
        urls = []
        for frame in page.frames:
            url = frame.url or ""
            if not url or not url.startswith("http"):
                continue
            try:
                host = urlparse(url).netloc
            except Exception:
                continue
            if host == DOMAIN or host.endswith("." + DOMAIN):
                continue
            if "challenges.cloudflare.com" in url:
                continue
            if any(kw in url.lower() for kw in _AD_IFRAME_KW):
                continue
            urls.append(url)
        return urls

    fallback = None
    for i in range(timeout):
        cands = _candidates()
        player = [u for u in cands if any(p in u.lower() for p in _PLAYER_PATTERNS)]
        if player:
            if i > 0:
                print(f"    （等待 {i+1} 秒后出现）")
            return player[0]
        if cands and fallback is None:
            fallback = cands[0]
        if i < timeout - 1:
            await asyncio.sleep(1)

    if fallback:
        print(f"  ⚠ 未找到 embed 播放器，兜底 URL: {fallback[:80]}")
    return fallback


# ─────────────────────────────────────────────────────────────────────────────
# m3u8 提取
# ─────────────────────────────────────────────────────────────────────────────

async def find_m3u8_via_network(page: Page, timeout: int) -> str | None:
    """
    网络拦截 m3u8/HLS 流 URL，支持三种检测方式：
      1. URL 含 .m3u8
      2. Content-Type 含 mpegurl
      3. 响应体以 #EXTM3U 开头（无扩展名 CDN）→ 保存到本地文件规避 token 过期
    """
    found = asyncio.Event()
    result: list[str] = []

    _SKIP_CT = ("image/", "font/", "text/javascript", "text/css",
                "text/html", "application/javascript", "application/json")
    _SKIP_EXT = ('.js', '.css', '.html', '.htm', '.json', '.png', '.jpg',
                 '.jpeg', '.gif', '.svg', '.woff', '.woff2', '.ttf', '.eot', '.ico')

    def _accept_by_url_ct(url: str, ct: str = "") -> bool:
        if ".m3u8" in url.lower():
            return True
        ct = ct.lower()
        return "mpegurl" in ct or "x-mpegurl" in ct

    def _is_media_candidate(url: str, ct: str) -> bool:
        ct_low = ct.lower()
        if any(ct_low.startswith(t) for t in _SKIP_CT):
            return False
        return not any(url.lower().split("?")[0].endswith(e) for e in _SKIP_EXT)

    def _cache_playlist(url: str, body: str) -> str:
        """将 m3u8 内容保存本地，相对 URL 转绝对，规避 CDN token 7 秒过期。"""
        base = url.rsplit("/", 1)[0] + "/"
        lines = []
        for line in body.splitlines():
            s = line.strip()
            if s and not s.startswith("#") and not s.startswith("http"):
                line = urljoin(base, s)
            lines.append(line)
        local = DOWNLOAD_DIR / "playlist_cache.m3u8"
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_text("\n".join(lines), encoding="utf-8")
        return str(local)

    def on_request(r):
        if not result and _accept_by_url_ct(r.url):
            result.append(r.url)
            found.set()

    async def on_response(r):
        if result:
            return
        ct = ""
        try:
            ct = r.headers.get("content-type", "")
        except Exception:
            pass
        if _accept_by_url_ct(r.url, ct):
            if not result:
                result.append(r.url)
                found.set()
            return
        if not _is_media_candidate(r.url, ct):
            return
        try:
            body = await r.text()
            if body.strip().startswith("#EXTM3U") and not result:
                local = _cache_playlist(r.url, body)
                print(f"\n  ✓ 捕获无扩展名 HLS，已缓存到本地: {local}")
                result.append(local)
                found.set()
        except Exception:
            pass

    def on_frame(f):
        if not result and _accept_by_url_ct(f.url):
            result.append(f.url)
            found.set()

    page.on("request", on_request)
    page.on("response", on_response)
    page.on("framenavigated", on_frame)

    try:
        await asyncio.wait_for(found.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        pass

    return result[0] if result else None


async def try_click_play(page: Page):
    """在主页面和所有非广告 iframe 中触发播放。"""
    # 滚动到视频区域
    try:
        await page.evaluate("document.querySelector('video')?.scrollIntoView({behavior:'instant',block:'center'})")
    except Exception:
        pass

    dom_selectors = ["video", ".vjs-big-play-button", ".jw-display-icon-container",
                     ".jw-icon-display", ".play-btn", ".btn-play",
                     "[class*='play']", "[aria-label*='play' i]", "#player"]
    for sel in dom_selectors:
        try:
            el = page.locator(sel).first
            if await el.count() > 0:
                try:
                    await el.scroll_into_view_if_needed()
                except Exception:
                    pass
                await el.click(timeout=3000)
                print(f"  ✓ 点击播放: {sel}")
                return
        except Exception:
            pass

    # iframe 中点击
    for frame in page.frames:
        furl = frame.url or ""
        if not furl or "cloudflare.com" in furl:
            continue
        if any(kw in furl.lower() for kw in _AD_IFRAME_KW):
            continue
        for sel in ["video", ".vjs-big-play-button", ".jw-display-icon-container", ".play-btn"]:
            try:
                el = frame.locator(sel).first
                if await el.count() > 0:
                    await el.click(timeout=3000)
                    print(f"  ✓ iframe 中点击播放: {sel}")
                    return
            except Exception:
                pass

    # JS 强制播放
    print("  ▶ DOM 点击未命中，尝试 JS 强制播放...")
    for ctx in [page] + list(page.frames):
        ctx_url = getattr(ctx, "url", "") or ""
        if "cloudflare.com" in ctx_url or any(kw in ctx_url.lower() for kw in _AD_IFRAME_KW):
            continue
        try:
            count = await ctx.evaluate("""() => {
                const vs = document.querySelectorAll('video');
                vs.forEach(v => { try { v.play(); } catch(e) {} });
                return vs.length;
            }""")
            if count:
                print(f"  ✓ JS video.play() 触发 {count} 个视频")
                return
        except Exception:
            pass

    # 播放器 API
    for js, label in [
        ("try{jwplayer().play();return true}catch(e){return false}", "jwplayer().play()"),
        ("try{videojs(document.querySelector('.video-js')).play();return true}catch(e){return false}",
         "videojs().play()"),
    ]:
        try:
            if await page.evaluate(f"(()=>{{ {js} }})()"):
                print(f"  ✓ {label}")
                return
        except Exception:
            pass

    print("  ⚠ 未找到可触发的播放元素，等待自动加载...")


async def extract_m3u8_fallback(page: Page) -> str | None:
    """DOM + JS 备用提取，优先从 video.currentSrc 读取。"""
    ctxs = [page] + [
        f for f in page.frames
        if f.url and "cloudflare.com" not in f.url
        and not any(kw in f.url.lower() for kw in _AD_IFRAME_KW)
    ]
    # 1. 播放器 API
    for ctx in ctxs:
        for script in [
            "(() => { const v = document.querySelector('video'); return v && (v.currentSrc || v.src) || null; })()",
            "(() => { try { return jwplayer().getPlaylistItem().file || null; } catch(e) { return null; } })()",
        ]:
            try:
                r = await ctx.evaluate(script)
                if r and isinstance(r, str) and r.startswith("http"):
                    print(f"  ✓ 播放器 API: {r[:80]}")
                    return r
            except Exception:
                pass

    # 2. HTML 正则搜索
    for ctx in ctxs:
        try:
            html = await ctx.content()
            m = M3U8_RE.search(html)
            if m:
                return m.group(0)
        except Exception:
            pass

    # 3. script 标签搜索
    try:
        r = await page.evaluate("""() => {
            for (const s of document.querySelectorAll('script')) {
                const m = s.textContent.match(/https?:\\/\\/[^\\s"'<>]+\\.m3u8/i);
                if (m) return m[0];
            }
            return null;
        }""")
        if r:
            return r
    except Exception:
        pass
    return None


# ─────────────────────────────────────────────────────────────────────────────
# 播放器 URL 直接提取模式（PLAYER_URL 快捷路径）
# ─────────────────────────────────────────────────────────────────────────────

async def extract_from_player_url(player_url: str) -> str | None:
    """直接加载播放器页面（如 playmogo.com/e/...），跳过 Cookie/主页逻辑。"""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=BROWSER_HEADLESS,
                                          args=["--no-sandbox", "--disable-dev-shm-usage"])
        context = await browser.new_context(user_agent=USER_AGENT,
                                            viewport={"width": 1920, "height": 1080})
        page = await context.new_page()

        stealth = _load_stealth_fn()
        if stealth:
            try:
                await stealth(page)
                print("  ✓ stealth 已应用")
            except Exception:
                pass

        m3u8_task = asyncio.create_task(find_m3u8_via_network(page, M3U8_TIMEOUT))

        print(f"  ▶ 加载播放器: {player_url[:80]}")
        try:
            await page.goto(player_url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            print(f"  ⚠ 加载超时（继续）: {e}")

        await wait_for_player_cf(page)
        await asyncio.sleep(2)

        m3u8_url: str | None = None
        if not m3u8_task.done():
            print("  ▶ 触发播放...")
            await try_click_play(page)
            if await wait_for_player_cf(page):
                await asyncio.sleep(2)
                m3u8_url = await extract_m3u8_fallback(page)

        if not m3u8_url:
            if m3u8_task.done():
                try:
                    m3u8_url = m3u8_task.result()
                except Exception:
                    pass
            else:
                try:
                    m3u8_url = await asyncio.wait_for(asyncio.shield(m3u8_task), timeout=30)
                except asyncio.TimeoutError:
                    pass

        if not m3u8_url:
            m3u8_url = await extract_m3u8_fallback(page)

        if not m3u8_url:
            shot = DOWNLOAD_DIR / "debug_player.png"
            await page.screenshot(path=str(shot), full_page=True)
            print(f"  截图: {shot}")

        await browser.close()
    return m3u8_url


# ─────────────────────────────────────────────────────────────────────────────
# 下载
# ─────────────────────────────────────────────────────────────────────────────

def _find_ffmpeg() -> str | None:
    import shutil
    if FFMPEG_PATH:
        if Path(FFMPEG_PATH).exists():
            return FFMPEG_PATH
        print(f"  ⚠ FFMPEG_PATH 不存在: {FFMPEG_PATH}")
    found = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
    if found:
        return found
    for p in [r"C:\ffmpeg\bin\ffmpeg.exe", r"C:\Program Files\ffmpeg\bin\ffmpeg.exe"]:
        if Path(p).exists():
            return p
    return None


def download_with_ffmpeg(m3u8_url: str, output: Path, ffmpeg_bin: str) -> bool:
    is_local = not m3u8_url.startswith("http") and Path(m3u8_url).exists()
    if is_local:
        print(f"  使用本地缓存播放列表: {m3u8_url}")
    cmd = [
        ffmpeg_bin, "-y",
        "-allowed_extensions", "ALL",
        "-headers", f"Referer: {TARGET_URL}\r\nUser-Agent: {USER_AGENT}\r\n",
        "-i", m3u8_url,
        "-c", "copy",
        "-bsf:a", "aac_adtstoasc",
        str(output),
    ]
    print(f"  ffmpeg: {ffmpeg_bin}")
    return subprocess.run(cmd).returncode == 0


async def download_with_python(m3u8_url: str, output: Path) -> bool:
    """纯 Python 备用下载器（不支持 AES 加密流）。"""
    import urllib.parse
    headers = {"Referer": TARGET_URL, "User-Agent": USER_AGENT}
    base_url = m3u8_url.rsplit("/", 1)[0] + "/"
    print("  使用内置 Python 下载器（无需 ffmpeg）")
    async with httpx.AsyncClient(headers=headers, timeout=30, follow_redirects=True) as client:
        playlist = (await client.get(m3u8_url)).text
        if "#EXT-X-KEY" in playlist:
            print("  ⚠ 加密流（AES-128），Python 下载器不支持，请安装 ffmpeg")
            return False
        segments = []
        for line in playlist.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                segments.append(line if line.startswith("http") else urllib.parse.urljoin(base_url, line))
        if not segments:
            print("  ✗ m3u8 无分片")
            return False

        print(f"  ✓ {len(segments)} 个分片，开始下载...")
        output.parent.mkdir(parents=True, exist_ok=True)
        ts_out = output.with_suffix(".ts")
        with open(ts_out, "wb") as f:
            for i, seg in enumerate(segments, 1):
                for attempt in range(3):
                    try:
                        f.write((await client.get(seg, timeout=20)).content)
                        break
                    except Exception as e:
                        if attempt == 2:
                            print(f"\n  ✗ 分片 {i} 失败: {e}")
                            return False
                        await asyncio.sleep(1)
                if i % 20 == 0 or i == len(segments):
                    print(f"  ▶ {i}/{len(segments)} ({i*100//len(segments)}%)", end="\r")

        print(f"\n  ✓ 完成: {ts_out}")
        print(f"  ℹ 转 mp4: ffmpeg -i \"{ts_out}\" -c copy \"{output}\"")
        return True


async def download_m3u8(m3u8_url: str, output: Path) -> bool:
    output.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = _find_ffmpeg()
    if ffmpeg:
        return download_with_ffmpeg(m3u8_url, output, ffmpeg)
    print("  ⚠ 未找到 ffmpeg，使用内置 Python 下载器")
    print("    安装 ffmpeg: apt install ffmpeg  /  winget install ffmpeg")
    return await download_with_python(m3u8_url, output)


# ─────────────────────────────────────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────────────────────────────────────

async def main():
    print("supjav.com 视频下载器（最终版 - 整合17次迭代经验）")
    print("=" * 64)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    # ── PLAYER_URL 快捷路径 ──────────────────────────────────────────────────
    if PLAYER_URL:
        print(f"  ▶ 直接播放器模式: {PLAYER_URL[:80]}")
        m3u8_url = await extract_from_player_url(PLAYER_URL)
        if not m3u8_url:
            print("\n  ✗ 无法从播放器 URL 提取视频流")
            sys.exit(1)
        print(f"\n  ✓ 视频流: {m3u8_url}")
        output = DOWNLOAD_DIR / "video.mp4"
        if not await download_m3u8(m3u8_url, output):
            print(f'\n  ✗ 下载失败，手动: ffmpeg -i "{m3u8_url}" -c copy output.mp4')
            sys.exit(1)
        print(f"\n  ✓ 下载完成: {output}")
        return

    # ── 第一步：获取 CF Cookie ────────────────────────────────────────────────
    print("\n[1/3] 获取 Cloudflare Cookie...")
    cookies = await acquire_cookies()
    cf = next((c for c in cookies if c["name"] == "cf_clearance"), None)
    print(f"  ✓ cf_clearance: {cf['value'][:40]}..." if cf else "  ⚠ 无 cf_clearance")

    # ── 第二步：加载页面，提取 m3u8 ─────────────────────────────────────────
    print(f"\n[2/3] 加载目标页面，提取视频流...")
    m3u8_url: str | None = None
    _player_url: str | None = None

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=BROWSER_HEADLESS,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled",
                  "--disable-dev-shm-usage"],
        )
        context: BrowserContext = await browser.new_context(
            user_agent=USER_AGENT, viewport={"width": 1920, "height": 1080},
        )
        await context.add_cookies(cookies)
        page = await context.new_page()

        # stealth 伪装——有助于播放器 CF 自动通过
        stealth = _load_stealth_fn()
        if stealth:
            try:
                await stealth(page)
                print("  ✓ stealth 伪装已应用（有助于播放器 CF 自动通过）")
            except Exception as e:
                print(f"  ⚠ stealth 失败（继续）: {e}")

        # 启动网络监听（在 goto 之前，避免错过早期请求）
        m3u8_task = asyncio.create_task(find_m3u8_via_network(page, M3U8_TIMEOUT))

        print(f"  ▶ 加载页面: {TARGET_URL}")
        try:
            await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            print(f"  ⚠ 加载超时（继续）: {e}")

        # 等待主页 CF 消失
        for _ in range(15):
            if not any("challenges.cloudflare.com" in f.url for f in page.frames):
                break
            await asyncio.sleep(1)
        await asyncio.sleep(3)

        # 检测是否仍被 CF 拦截
        try:
            snippet = (await page.content())[:2000].lower()
            title = await page.title()
            if any(x in snippet for x in ("just a moment", "enable javascript and cookies",
                                           "performing security verification",
                                           "cf-browser-verification")) \
                    or "just a moment" in title.lower():
                shot = DOWNLOAD_DIR / "debug_cf.png"
                await page.screenshot(path=str(shot))
                await browser.close()
                print(f"\n  ✗ Cloudflare 仍在拦截（截图: {shot}）")
                print("  cf_clearance 与 IP 绑定，约1小时失效。")
                print("  解决：浏览器访问页面 → F12 → 复制 Cookie → 存入 cf_cookies.json")
                if COOKIE_CACHE_FILE.exists():
                    COOKIE_CACHE_FILE.unlink()
                sys.exit(1)
        except Exception:
            pass

        # 切换视频服务器（DS/TV/ST/JPA）
        if VIDEO_SERVER:
            print(f"\n  ▶ 切换到 {VIDEO_SERVER} 服务器...")
            # 关键：在点击按钮前重置监听，捕获 VideoJS 预加载请求
            if not m3u8_task.done():
                m3u8_task.cancel()
            m3u8_task = asyncio.create_task(find_m3u8_via_network(page, M3U8_TIMEOUT))

            await click_server_button(page, VIDEO_SERVER)
            # click_server_button 内部已等待5秒让播放器 iframe 加载

            # 等待播放器 CF（playmogo.com 有独立 CF 验证）
            await wait_for_player_cf(page)

            # 打印 frame 列表（调试）
            print("  ▶ 切换后 frames:")
            for i, f in enumerate(page.frames):
                print(f"    [{i}] {f.url}")

            # 检测播放器 embed iframe URL
            print("  ▶ 等待播放器 iframe...")
            _player_url = await _find_player_iframe_url(page)
            if _player_url:
                print(f"  ✓ 播放器 URL: {_player_url[:80]}")
        else:
            print("  ▶ 页面 frames:")
            for i, f in enumerate(page.frames):
                print(f"    [{i}] {f.url}")
            _player_url = await _find_player_iframe_url(page)
            if _player_url:
                print(f"  ✓ 播放器 URL: {_player_url[:80]}")

        # 触发播放（若 m3u8 尚未被预加载捕获）
        if not m3u8_task.done():
            print("\n  ▶ 触发视频播放...")
            await try_click_play(page)
            if await wait_for_player_cf(page):
                await asyncio.sleep(2)
                m3u8_url = await extract_m3u8_fallback(page)
                if m3u8_url:
                    print(f"  ✓ CF 通过后从播放器 API 获取 URL")

        # 等待网络拦截结果
        if not m3u8_url:
            if m3u8_task.done():
                try:
                    m3u8_url = m3u8_task.result()
                except Exception:
                    pass
            else:
                try:
                    m3u8_url = await asyncio.wait_for(asyncio.shield(m3u8_task), timeout=30)
                except asyncio.TimeoutError:
                    pass

        # 最终备用：DOM/JS 全量搜索
        if not m3u8_url:
            print("  ▶ 网络拦截无结果，尝试 DOM/JS 搜索...")
            m3u8_url = await extract_m3u8_fallback(page)

        if not m3u8_url:
            if _player_url:
                print(f"\n  ▶ 常规提取失败，尝试直接加载播放器: {_player_url[:80]}")
                await browser.close()
            else:
                shot = DOWNLOAD_DIR / "debug.png"
                await page.screenshot(path=str(shot), full_page=True)
                print(f"\n  ✗ 未检测到视频流（截图: {shot}）")
                await browser.close()
                sys.exit(1)
        else:
            print(f"\n  ✓ 视频流 URL:\n    {m3u8_url}")
            await browser.close()

    # 若常规流程失败但找到了播放器 iframe URL
    if not m3u8_url and _player_url:
        m3u8_url = await extract_from_player_url(_player_url)
        if not m3u8_url:
            print("\n  ✗ 无法从播放器 URL 提取视频流")
            sys.exit(1)
        print(f"\n  ✓ 视频流 URL:\n    {m3u8_url}")

    # ── 第三步：下载 ─────────────────────────────────────────────────────────
    print(f"\n[3/3] 开始下载...")
    output = DOWNLOAD_DIR / "video.mp4"
    print(f"  目标: {output}")
    ok = await download_m3u8(m3u8_url, output)
    if ok:
        print(f"\n  ✓ 下载完成: {output}")
    else:
        print(f"\n  ✗ 下载失败，手动执行:")
        print(f'  ffmpeg -allowed_extensions ALL -i "{m3u8_url}" -c copy downloads/video.mp4')
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
