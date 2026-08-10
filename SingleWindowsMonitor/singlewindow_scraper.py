#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
中国国际贸易单一窗口 - 每日监控简报
====================================
只抓取并展示两个栏目：**最新动态（通知公告）** 与 **新特性**，
每条都带完整正文详情 + 原文链接，输出为可直接发给领导的邮件 HTML。

数据获取策略（两级兜底）
------------------------
  1) 官网 JSON 接口（默认，秒级返回，不依赖浏览器）
        列表：POST /access/ui/SW-SITE-FRONT/Article001  {catalogid, pageNo, rowsPerPage}
        详情：POST /access/ui/SW-SITE-FRONT/Article002  {articleid}   -> bodytext 全文
  2) 接口不可用时，回退到 Playwright 渲染首页抓取（只有摘要，没有全文）

【一次性安装】
  pip install playwright                  # 仅回退方案需要，接口正常时可不装
  python -m playwright install chromium

【运行方式】
  python singlewindow_scraper.py                 # 抓取 + 生成 HTML + 发邮件
  python singlewindow_scraper.py --no-mail       # 只生成 HTML，不发邮件（调试用）
  python singlewindow_scraper.py --notices 8 --features 8
  python singlewindow_scraper.py --detail-chars 1500
  python singlewindow_scraper.py --to a@x.com --to b@x.com
  python singlewindow_scraper.py --browser-only  # 强制走 Playwright 回退方案
  python singlewindow_scraper.py --show          # 回退方案下显示浏览器窗口

【输出】
  singlewindow_output.html
"""

# ── Windows GBK 控制台编码修复（必须在所有 import 之前）──────────────
import sys

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        import io

        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import argparse
import json
import os
import re
import shutil
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import email_sender
import sw_render

# ══════════════════════════════════════════════════════════
#  配置
# ══════════════════════════════════════════════════════════
CN_TZ = timezone(timedelta(hours=8))     # 官网时间一律按北京时间解读

BASE_URL    = "https://www.singlewindow.cn"
API_BASE    = BASE_URL + "/access/ui/SW-SITE-FRONT/"
NOTICE_URL  = BASE_URL + "/#/notice"     # 最新动态 - 通知公告
FEATURE_URL = BASE_URL + "/#/features"   # 新特性

# 官网栏目 ID 与详情页面包屑编号
CATALOG_NOTICE,  BREAD_NOTICE  = "tzgg", "bc12"   # 通知公告
CATALOG_FEATURE, BREAD_FEATURE = "xtx",  "bc13"   # 新特性

NOTICE_COUNT      = 5      # 最新动态展示条数
FEATURE_COUNT     = 5      # 新特性展示条数
DETAIL_MAX_CHARS  = 900    # 每条正文最多展示多少字（0 = 不截断）
NEW_DAYS          = 3      # 几天内发布的标 NEW

API_TIMEOUT     = 20       # 秒
API_RETRY       = 3        # 单个接口最多试几次
API_RETRY_WAIT  = 2        # 秒，重试间隔（按次数递增）
PAGE_TIMEOUT = 30_000      # ms，Playwright 回退用
RENDER_WAIT  = 5           # 秒，等待 Vue 渲染
OUTPUT_FILE  = Path(__file__).parent / "singlewindow_output.html"

# 邮件
SMTP_SERVER   = "smtp.cn.dhl.com"
SMTP_PORT     = 25
SENDER_EMAIL  = "singlewindow@dhl.com"
RECEIVERS     = ["kexue.guo@dhl.com"]
CC            = []

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0")


# ══════════════════════════════════════════════════════════
#  方案一：官网 JSON 接口
# ══════════════════════════════════════════════════════════
def _api_post(path, payload):
    req = urllib.request.Request(
        API_BASE + path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json;charset=UTF-8",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "User-Agent": UA,
            "Referer": BASE_URL + "/",
            "Origin": BASE_URL,
        },
        method="POST",
    )
    # 每天无人值守跑，偶发的网络抖动不该直接把整封邮件打回摘要版，重试几次
    last_err = None
    for attempt in range(1, API_RETRY + 1):
        try:
            with urllib.request.urlopen(req, timeout=API_TIMEOUT) as resp:
                body = json.loads(resp.read().decode("utf-8", "replace"))
            if body.get("status") != "success":
                raise RuntimeError("接口返回失败：{}".format(body.get("message") or body))
            return body.get("data")
        except (urllib.error.URLError, TimeoutError, ValueError, RuntimeError, OSError) as exc:
            last_err = exc
            if attempt < API_RETRY:
                time.sleep(API_RETRY_WAIT * attempt)
    raise last_err


def _ts_to_dt(value):
    """
    接口返回的是 13 位毫秒时间戳。

    统一按北京时间（UTC+8）换算，而不是跟着服务器本地时区走 —— 否则跑在
    非 +08:00 时区的机器上，发布日期会整体差一天。
    """
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    if n > 10 ** 12:
        n //= 1000
    try:
        return datetime.fromtimestamp(n, CN_TZ).replace(tzinfo=None)
    except (OverflowError, OSError, ValueError):
        return None


def _detail_url(article_id, bread_num):
    return "{}/#/detail?breadNum={}&articleId={}".format(BASE_URL, bread_num, article_id)


def fetch_via_api(catalog_id, bread_num, count, with_body=True):
    data = _api_post("Article001", {"catalogid": catalog_id, "pageNo": 1, "rowsPerPage": count})
    rows = (data or {}).get("data") or []

    items = []
    for row in rows[:count]:
        article_id = row.get("articleid") or ""
        item = {
            "id":      article_id,
            "title":   sw_render.clean_title(row.get("title")),
            "url":     _detail_url(article_id, bread_num) if article_id else NOTICE_URL,
            "dt":      _ts_to_dt(row.get("releasetime")),
            "summary": (row.get("summary") or "").strip(),
            "source":  (row.get("source") or "").strip(),
            "body":    row.get("bodytext") or "",
        }
        # 列表接口不返回全文，逐条取详情
        if with_body and article_id and not item["body"]:
            try:
                detail = _api_post("Article002", {"articleid": article_id}) or {}
                item["body"] = detail.get("bodytext") or ""
                item["source"] = (detail.get("source") or item["source"] or "").strip()
            except Exception as exc:  # 单条失败不影响整体
                print("        [WARN] 取详情失败 {}：{}".format(article_id, exc))
        items.append(item)
    return items


# ══════════════════════════════════════════════════════════
#  方案二：Playwright 回退（接口不可用时）
# ══════════════════════════════════════════════════════════
_LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-blink-features=AutomationControlled",
    "--disable-infobars",
    "--lang=zh-CN,zh",
    "--window-size=1920,1080",
]

_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver',  { get: () => undefined });
Object.defineProperty(navigator, 'plugins',    { get: () => [1,2,3,4,5] });
Object.defineProperty(navigator, 'languages',  { get: () => ['zh-CN','zh','en'] });
window.chrome = { runtime: {}, loadTimes: function(){}, csi: function(){} };
"""


def _win_env(*keys):
    return [v for k in keys for v in [os.environ.get(k, "")] if v]


def _edge_candidates():
    paths = []
    if sys.platform == "win32":
        for base in _win_env("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA", "PROGRAMW6432"):
            paths += [
                r"{}\Microsoft\Edge\Application\msedge.exe".format(base),
                r"{}\Microsoft\Edge Beta\Application\msedge.exe".format(base),
                r"{}\Microsoft\Edge Dev\Application\msedge.exe".format(base),
            ]
        for drive in ("C", "D", "E"):
            paths += [
                r"{}:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe".format(drive),
                r"{}:\Program Files\Microsoft\Edge\Application\msedge.exe".format(drive),
            ]
    elif sys.platform == "darwin":
        paths = ["/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"]
    else:
        for name in ("microsoft-edge", "microsoft-edge-stable", "msedge"):
            found = shutil.which(name)
            if found:
                paths.append(found)
    return paths


def _chrome_candidates():
    paths = []
    if sys.platform == "win32":
        for base in _win_env("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA", "PROGRAMW6432"):
            paths += [
                r"{}\Google\Chrome\Application\chrome.exe".format(base),
                r"{}\Google\Chrome Beta\Application\chrome.exe".format(base),
            ]
        for drive in ("C", "D"):
            paths += [
                r"{}:\Program Files\Google\Chrome\Application\chrome.exe".format(drive),
                r"{}:\Program Files (x86)\Google\Chrome\Application\chrome.exe".format(drive),
            ]
    elif sys.platform == "darwin":
        paths = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
    else:
        for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
            found = shutil.which(name)
            if found:
                paths.append(found)
    return paths


def find_browser():
    for path_list, label in [(_edge_candidates(), "Microsoft Edge"),
                             (_chrome_candidates(), "Google Chrome")]:
        for path in path_list:
            if Path(path).exists():
                return path, label
    return None, "Playwright 内置 Chromium"


_MONTH_MAP = {"一": "01", "二": "02", "三": "03", "四": "04", "五": "05", "六": "06",
              "七": "07", "八": "08", "九": "09", "十": "10", "十一": "11", "十二": "12"}


def _parse_home_cards(html_text):
    """从首页 DOM 里解析"最新动态""新特性"两张卡片（无正文，只有摘要）。"""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html_text, "html.parser")
    seen = set()

    def extract(keyword):
        result = []
        for title_node in soup.find_all(string=re.compile(keyword)):
            card = title_node.find_parent(
                lambda t: t.name in ("div", "section", "article")
                and t.find("a") and len(t.get_text()) > 80
            )
            if not card:
                continue
            for row in card.find_all(True):
                a = row.find("a")
                if not a:
                    continue
                title = sw_render.clean_title(a.get_text(strip=True))
                if len(title) < 6 or title in seen:
                    continue
                row_text = row.get_text(" ", strip=True)
                m_cn = re.search(r"([一二三四五六七八九十]+)月", row_text)
                if not m_cn:
                    continue
                d_cn = re.search(r"\b(\d{1,2})\b", row_text)
                month = _MONTH_MAP.get(m_cn.group(1), "01")
                day = d_cn.group(1).zfill(2) if d_cn else "01"
                try:
                    dt = datetime.strptime("{}-{}-{}".format(datetime.now().year, month, day),
                                           "%Y-%m-%d")
                except ValueError:
                    dt = None
                href = a.get("href", "") or ""
                if href and not href.startswith("http"):
                    href = BASE_URL + "/" + href.lstrip("/")
                summary = re.sub(r"\s{2,}", " ",
                                 row_text.replace(title, "").replace(m_cn.group(0), "")
                                 ).strip("·—|>0123456789 ")
                result.append({
                    "id": "", "title": title, "url": href or NOTICE_URL,
                    "dt": dt, "summary": summary, "source": "", "body": "",
                })
                seen.add(title)
            if result:
                break
        return result

    return extract("最新动态"), extract("新特性")


def fetch_via_browser(headless=True, force_browser="auto"):
    from playwright.sync_api import sync_playwright

    if force_browser == "edge":
        exe = next((p for p in _edge_candidates() if Path(p).exists()), None)
        label = "Microsoft Edge" if exe else "Playwright 内置 Chromium"
    elif force_browser == "chrome":
        exe = next((p for p in _chrome_candidates() if Path(p).exists()), None)
        label = "Google Chrome" if exe else "Playwright 内置 Chromium"
    elif force_browser == "builtin":
        exe, label = None, "Playwright 内置 Chromium"
    else:
        exe, label = find_browser()

    print("        浏览器：{}{}".format(label, "  ({})".format(exe) if exe else ""))

    kwargs = dict(headless=headless, args=_LAUNCH_ARGS)
    if exe:
        kwargs["executable_path"] = exe

    with sync_playwright() as pw:
        browser = pw.chromium.launch(**kwargs)
        ctx = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            user_agent=UA,
            extra_http_headers={"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
        )
        ctx.add_init_script(_STEALTH_JS)
        page = ctx.new_page()
        page.set_default_timeout(PAGE_TIMEOUT)
        page.goto(BASE_URL + "/#/", wait_until="networkidle")
        time.sleep(RENDER_WAIT)
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(1)
        content = page.content()
        ctx.close()
        browser.close()

    notices, features = _parse_home_cards(content)
    return notices, features, label


# ══════════════════════════════════════════════════════════
#  抓取调度
# ══════════════════════════════════════════════════════════
def collect(args):
    """返回 (notices, features, source_label)。"""
    if not args.browser_only:
        try:
            print("  [1/2] 通过官网接口获取「最新动态」...")
            notices = fetch_via_api(CATALOG_NOTICE, BREAD_NOTICE, args.notices)
            print("        取得 {} 条".format(len(notices)))

            print("  [2/2] 通过官网接口获取「新特性」...")
            features = fetch_via_api(CATALOG_FEATURE, BREAD_FEATURE, args.features)
            print("        取得 {} 条".format(len(features)))

            if notices or features:
                return notices, features, "官网接口（含正文全文）"
            print("  [WARN] 接口返回空，转用浏览器抓取")
        except Exception as exc:
            print("  [WARN] 接口不可用（{}），转用浏览器抓取".format(exc))

    try:
        print("  [回退] 使用 Playwright 渲染首页抓取...")
        notices, features, label = fetch_via_browser(
            headless=not args.show, force_browser=args.browser)
        print("        最新动态 {} 条 | 新特性 {} 条".format(len(notices), len(features)))
        return (notices[:args.notices], features[:args.features],
                "浏览器抓取 · {}（仅摘要）".format(label))
    except Exception as exc:
        print("  [ERROR] 浏览器抓取同样失败：{}".format(exc))
        return [], [], "抓取失败"


# ══════════════════════════════════════════════════════════
#  自检：部署到新机器 / 排查故障时先跑这个
# ══════════════════════════════════════════════════════════
def self_test(args):
    """逐项检查依赖，不发信、不写文件。全部通过返回 0。"""
    checks = []

    def record(name, ok, detail=""):
        checks.append((name, ok, detail))
        print("  [{}] {}{}".format("OK  " if ok else "FAIL", name,
                                   "  —— " + detail if detail else ""))

    print("\n  ── 自检开始 ──")

    # 1. 列表接口
    notices = []
    try:
        notices = fetch_via_api(CATALOG_NOTICE, BREAD_NOTICE, 1, with_body=False)
        record("官网列表接口（最新动态）", bool(notices),
               "取到 {} 条".format(len(notices)) if notices else "返回空")
    except Exception as exc:
        record("官网列表接口（最新动态）", False, str(exc))

    # 2. 详情接口（正文全文靠它）
    if notices and notices[0].get("id"):
        try:
            detail = _api_post("Article002", {"articleid": notices[0]["id"]}) or {}
            body = detail.get("bodytext") or ""
            record("官网详情接口（正文全文）", bool(body), "正文 {} 字".format(len(body)))
        except Exception as exc:
            record("官网详情接口（正文全文）", False, str(exc))
    else:
        record("官网详情接口（正文全文）", False, "上一步没拿到文章 ID，跳过")

    # 3. 兜底方案的依赖（缺了不影响日常运行，只是没了保险）
    for mod, why in (("playwright", "接口不可用时用它渲染首页"),
                     ("bs4", "解析首页 DOM")):
        try:
            __import__(mod)
            record("兜底依赖 {}".format(mod), True, why)
        except ImportError:
            record("兜底依赖 {}".format(mod), False, "未安装（{}）".format(why))

    # 4. SMTP 只连不发
    try:
        import smtplib
        smtp = smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=15)
        smtp.ehlo()
        smtp.quit()
        record("SMTP 连通性 {}:{}".format(SMTP_SERVER, SMTP_PORT), True, "只握手，未发信")
    except Exception as exc:
        record("SMTP 连通性 {}:{}".format(SMTP_SERVER, SMTP_PORT), False, str(exc))

    # 5. 输出目录可写
    try:
        probe = OUTPUT_FILE.parent / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        record("输出目录可写 {}".format(OUTPUT_FILE.parent), True)
    except Exception as exc:
        record("输出目录可写 {}".format(OUTPUT_FILE.parent), False, str(exc))

    # 兜底依赖缺失不算致命：接口通就能正常出报
    fatal = [n for n, ok, _ in checks if not ok and not n.startswith("兜底依赖")]
    print("  ── 自检结束 ──\n")
    if fatal:
        print("  [FAIL] 以下项目需要处理：{}".format("、".join(fatal)))
        return 1
    if any(not ok for _, ok, _ in checks):
        print("  [OK] 关键项全部通过（兜底依赖未装，接口异常时将没有备用方案）")
    else:
        print("  [OK] 全部通过")
    return 0


# ══════════════════════════════════════════════════════════
#  入口
# ══════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser(description="中国国际贸易单一窗口 每日监控简报")
    parser.add_argument("--notices", type=int, default=NOTICE_COUNT, help="最新动态条数")
    parser.add_argument("--features", type=int, default=FEATURE_COUNT, help="新特性条数")
    parser.add_argument("--detail-chars", type=int, default=DETAIL_MAX_CHARS,
                        help="每条正文最多展示字数，0 表示不截断")
    parser.add_argument("--new-days", type=int, default=NEW_DAYS, help="几天内发布的标 NEW")
    parser.add_argument("--no-mail", action="store_true", help="只生成 HTML，不发邮件")
    parser.add_argument("--to", action="append", default=None, help="收件人（可多次指定）")
    parser.add_argument("--browser-only", action="store_true", help="跳过接口，直接用浏览器抓取")
    parser.add_argument("--show", action="store_true", help="回退到浏览器时显示窗口")
    parser.add_argument("--browser", choices=["auto", "edge", "chrome", "builtin"],
                        default="auto", help="回退方案使用的浏览器")
    parser.add_argument("--self-test", action="store_true",
                        help="只做环境自检（接口/依赖/SMTP/目录），不抓取也不发信")
    args = parser.parse_args()

    print("=" * 52)
    print("  中国国际贸易单一窗口  每日监控简报")
    print("=" * 52)

    if args.self_test:
        return self_test(args)

    notices, features, source_label = collect(args)

    now = datetime.now(CN_TZ).replace(tzinfo=None)
    ctx = {
        "base_url": BASE_URL,
        "notice_more_url": NOTICE_URL,
        "feature_more_url": FEATURE_URL,
        "fetch_time": now,
        "source_label": source_label,
        "detail_max_chars": args.detail_chars,
        "new_days": args.new_days,
    }
    html_text = sw_render.build_email_html(notices, features, ctx)
    OUTPUT_FILE.write_text(html_text, encoding="utf-8")

    if not args.no_mail:
        receivers = args.to or RECEIVERS
        sender = email_sender.HtmlEmailSender(
            smtp_server=SMTP_SERVER, smtp_port=SMTP_PORT, sender_email=SENDER_EMAIL)
        sender.send_html_file(
            html_file=str(OUTPUT_FILE),
            subject="单一窗口每日监控 · 最新动态与新特性（{:%Y-%m-%d}）".format(now),
            receivers=receivers,
            cc=CC,
            text_content=sw_render.build_plain_text(notices, features, ctx),
        )

    ok = bool(notices or features)
    print("\n" + "=" * 52)
    print("[{}] 输出文件 : {}".format("OK" if ok else "WARN", OUTPUT_FILE))
    print("     数据来源 : {}".format(source_label))
    print("     最新动态 : {} 条".format(len(notices)))
    print("     新特性   : {} 条".format(len(features)))
    if not ok:
        # 返回非 0，Windows 计划任务里会显示为失败，不至于一直发空邮件没人发现
        print("     两个栏目都没抓到内容，请检查网络或运行 --self-test")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
