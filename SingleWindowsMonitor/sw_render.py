#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
邮件 HTML 渲染模块
==================

只渲染两块内容：**最新动态**（通知公告）与 **新特性**，并尽可能展示正文详情 + 原文链接。

设计约束（为什么全是 table 和内联样式）
--------------------------------------
领导多半用 Outlook（桌面版）看邮件，它用 Word 排版引擎，不支持 flex / grid /
外部样式表，对 <style> 里的选择器支持也很有限。因此本模板遵循业界通用的
"bulletproof email" 写法，参考了 GitHub 上成熟的开源邮件框架：

  * Cerberus (TedGoas/Cerberus, MIT)        —— 单列/固定宽度骨架、mso 条件注释、
                                               role="presentation" 语义、
                                               max-width + 媒体查询的流式容器
  * Foundation for Emails (foundation/inky) —— 内容块（card）的内外边距节奏
  * HTML Email Boilerplate (seanpowell)     —— Outlook/Gmail/网易邮箱的兼容重置

具体做法：
  1. 布局一律 <table role="presentation">，不用 div 定位；
  2. 所有样式内联（<style> 只用于媒体查询和暗色模式微调，属于渐进增强）；
  3. 背景色同时给 bgcolor 属性和 style，Outlook 只认前者；
  4. 容器宽度 680px，外面套 <!--[if mso]> 的固定宽度表格；
  5. 行高统一加 mso-line-height-rule:exactly，避免 Outlook 行距被撑开；
  6. 不用 border-radius / box-shadow 做关键信息表达（Outlook 会忽略，降级即可）。
"""

import html
import re
from datetime import datetime

# ══════════════════════════════════════════════════════════
#  配色（改这里就能整体换肤）
# ══════════════════════════════════════════════════════════
C_BG        = "#eef1f5"   # 页面底色
C_CARD      = "#ffffff"   # 卡片底色
C_INK       = "#10233b"   # 正文主色
C_DEEP      = "#0b3d78"   # 品牌深蓝（标题）
C_PRIMARY   = "#1a5fa8"   # 品牌蓝（链接/日期）
C_ACCENT    = "#c8102e"   # 品牌红（栏目标识）
C_MUTED     = "#6b7c93"   # 次要文字
C_LINE      = "#e3e8ef"   # 分隔线/边框
C_CHIP_BG   = "#eef4fc"   # 日期块底色
C_CHIP_LINE = "#d3e0f5"   # 日期块边框

FONT = ('-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",'
        '"PingFang SC","Hiragino Sans GB",SimSun,Arial,sans-serif')

_MONTH_CN = ["一月", "二月", "三月", "四月", "五月", "六月",
             "七月", "八月", "九月", "十月", "十一月", "十二月"]
_WEEK_CN = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]


# ══════════════════════════════════════════════════════════
#  正文清洗：富文本 HTML → 干净段落（保留超链接）
# ══════════════════════════════════════════════════════════
_RE_DROP_BLOCK = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.I | re.S)
_RE_BR         = re.compile(r"<br\s*/?>", re.I)
_RE_BLOCK_END  = re.compile(r"</(p|div|tr|li|h[1-6]|table|blockquote)\s*>", re.I)
_RE_LI         = re.compile(r"<li[^>]*>", re.I)
_RE_ANCHOR     = re.compile(r"<a\b[^>]*?href\s*=\s*([\"'])(.*?)\1[^>]*>(.*?)</a>", re.I | re.S)
_RE_TAG        = re.compile(r"<[^>]+>")
_RE_SPACES     = re.compile(r"[ \t　\xa0]+")

_LINK_TOKEN = "\x00L{}\x00"
_RE_LINK_TOKEN = re.compile(r"\x00L(\d+)\x00")


def _shorten_url_label(label):
    """
    正文里常常直接贴一条长网址当链接文字。Outlook 不支持 word-break，
    整条网址会把表格撑破，所以显示时缩短（href 仍然是完整地址）。
    """
    if not re.match(r"^https?://", label, re.I) or len(label) <= 42:
        return label
    rest = re.sub(r"^https?://", "", label, flags=re.I)
    host, _, path = rest.partition("/")
    if not path:
        return host
    first = path.split("/")[0]
    short = "{}/{}".format(host, first) if first else host
    return (short + "/…") if len(short) + 2 < len(rest) else rest


def _abs_url(url, base):
    """把正文里的相对链接补全成绝对链接。"""
    url = (url or "").strip()
    if not url:
        return ""
    if url.startswith(("http://", "https://", "mailto:", "tel:")):
        return url
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("#"):
        return base.rstrip("/") + "/" + url
    return base.rstrip("/") + "/" + url.lstrip("/")


# 公文末尾的套话/落款，对领导阅读没有信息量，且会挤占正文字数额度
_RE_SIG_BOILER = re.compile(r"^(特此(通知|公告|函告)[。.]?|谢谢[配合支持]*[。.]?)$")
_RE_SIG_DATE   = re.compile(r"^\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日$")
_RE_SIG_ORG    = re.compile(r"^.{2,20}(中心|总署|海关|银行|管理局|办公室|委员会|数据中心|有限公司)$")


def _strip_signature(lines):
    """从尾部剥掉"特此通知 / 落款单位 / 落款日期"这类固定尾巴。"""
    end = len(lines)
    while end > 0:
        last = lines[end - 1]
        if (_RE_SIG_BOILER.match(last) or _RE_SIG_DATE.match(last)
                or (_RE_SIG_ORG.match(last) and len(last) <= 20)):
            end -= 1
        else:
            break
    # 全是落款说明判断过头了，那就原样返回
    return lines[:end] if end else lines


def body_to_paragraphs(raw, base_url, max_chars=0):
    """
    把接口返回的 bodytext（可能是富文本，也可能是纯文本）转成段落列表。

    返回 (paragraphs, truncated)：
      paragraphs —— 每项是可直接放进邮件的 HTML 片段（已转义，仅保留 <a>）
      truncated  —— 是否因为超长被截断
    """
    if not raw:
        return [], False

    text = _RE_DROP_BLOCK.sub(" ", str(raw))

    # 先把超链接抠出来占位，避免后面被"去标签"一并干掉
    links = []

    def _keep(m):
        url = _abs_url(html.unescape(m.group(2)), base_url)
        label = _RE_SPACES.sub(" ", _RE_TAG.sub("", m.group(3))).strip()
        label = _shorten_url_label(html.unescape(label) or url)
        if not url:
            return label
        links.append((url, label))
        return _LINK_TOKEN.format(len(links) - 1)

    text = _RE_ANCHOR.sub(_keep, text)

    # 块级标签 → 换行
    text = _RE_BR.sub("\n", text)
    text = _RE_LI.sub("\n· ", text)
    text = _RE_BLOCK_END.sub("\n", text)
    text = _RE_TAG.sub("", text)
    text = html.unescape(text)

    # 归一化空白：正文里大量 &nbsp; 缩进，直接压掉
    lines = []
    for line in text.split("\n"):
        line = _RE_SPACES.sub(" ", line).strip()
        if line and line not in ("·",):
            lines.append(line)

    # 去掉相邻重复行（部分文章的富文本会重复渲染）
    deduped = []
    for line in lines:
        if not deduped or deduped[-1] != line:
            deduped.append(line)

    deduped = _strip_signature(deduped)

    # 按可见字数截断
    truncated = False
    if max_chars and max_chars > 0:
        kept, used = [], 0
        for line in deduped:
            plain = _RE_LINK_TOKEN.sub(lambda m: links[int(m.group(1))][1], line)
            if used + len(plain) <= max_chars:
                kept.append(line)
                used += len(plain)
            else:
                room = max_chars - used
                if room >= 20:
                    kept.append(plain[:room].rstrip() + "……")
                truncated = True
                break
        deduped = kept

    # 转义 + 还原超链接
    out = []
    for line in deduped:
        safe = html.escape(line, quote=False)
        safe = _RE_LINK_TOKEN.sub(
            lambda m: ('<a href="{}" target="_blank" style="color:{};text-decoration:underline;'
                       'word-break:break-all;">{}</a>').format(
                html.escape(links[int(m.group(1))][0], quote=True),
                C_PRIMARY,
                html.escape(links[int(m.group(1))][1], quote=False),
            ),
            safe,
        )
        out.append(safe)
    return out, truncated


def clean_title(raw):
    """
    标题里常带换行和大段空格（如"货物申报\\n    2026年07月29日版本"）。
    顺手去掉可能混进来的标签 —— HTML 版会转义，纯文本版没有转义可依赖。
    """
    text = _RE_TAG.sub("", str(raw or "").replace("\n", " "))
    return _RE_SPACES.sub(" ", html.unescape(text)).strip()


# ══════════════════════════════════════════════════════════
#  小组件
# ══════════════════════════════════════════════════════════
def _date_chip(dt):
    """左侧月/日日期块。"""
    month = _MONTH_CN[dt.month - 1] if dt else "--"
    day = "{:02d}".format(dt.day) if dt else "--"
    return (
        '<table role="presentation" border="0" cellpadding="0" cellspacing="0" width="54" '
        'bgcolor="{chip}" style="width:54px;background:{chip};border:1px solid {line};'
        'border-radius:6px;">'
        '<tr><td align="center" style="padding:7px 2px 1px;font-family:{font};font-size:12px;'
        'color:{primary};line-height:14px;mso-line-height-rule:exactly;">{month}</td></tr>'
        '<tr><td align="center" style="padding:0 2px 8px;font-family:{font};font-size:21px;'
        'font-weight:bold;color:{primary};line-height:22px;mso-line-height-rule:exactly;">{day}</td></tr>'
        '</table>'
    ).format(chip=C_CHIP_BG, line=C_CHIP_LINE, font=FONT,
             primary=C_PRIMARY, month=month, day=day)


def _badge(text, bg, color, border):
    return (
        '<span style="display:inline-block;padding:1px 7px;margin-left:6px;font-size:11px;'
        'line-height:17px;mso-line-height-rule:exactly;color:{color};background:{bg};'
        'border:1px solid {border};border-radius:9px;white-space:nowrap;">{text}</span>'
    ).format(text=html.escape(text), bg=bg, color=color, border=border)


def _section_header(title, subtitle, more_url, accent):
    return (
        '<table role="presentation" border="0" cellpadding="0" cellspacing="0" width="100%" '
        'style="width:100%;">'
        '<tr>'
        '<td valign="middle" style="padding:0 0 12px;">'
        '<table role="presentation" border="0" cellpadding="0" cellspacing="0">'
        '<tr>'
        '<td width="4" bgcolor="{accent}" style="width:4px;background:{accent};font-size:0;'
        'line-height:0;border-radius:2px;">&nbsp;</td>'
        '<td style="padding-left:10px;font-family:{font};font-size:17px;font-weight:bold;'
        'color:{deep};line-height:22px;mso-line-height-rule:exactly;white-space:nowrap;">{title}'
        '<span style="font-weight:normal;font-size:12px;color:{muted};padding-left:8px;">{sub}</span>'
        '</td>'
        '</tr></table>'
        '</td>'
        '<td align="right" valign="middle" style="padding:0 0 12px;font-family:{font};font-size:12px;">'
        '<a href="{more}" target="_blank" style="color:{primary};text-decoration:none;">查看全部 &rsaquo;&rsaquo;</a>'
        '</td>'
        '</tr></table>'
    ).format(accent=accent, font=FONT, deep=C_DEEP, muted=C_MUTED, primary=C_PRIMARY,
             title=html.escape(title), sub=html.escape(subtitle), more=html.escape(more_url, quote=True))


def _article_card(item, base_url, max_chars, is_new):
    title = clean_title(item.get("title"))
    url = item.get("url") or base_url
    dt = item.get("dt")
    date_txt = dt.strftime("%Y-%m-%d") if dt else ""
    source = (item.get("source") or "").strip()

    paragraphs, truncated = body_to_paragraphs(item.get("body"), base_url, max_chars)
    if not paragraphs:
        summary = clean_title(item.get("summary"))
        if summary:
            paragraphs = [html.escape(summary, quote=False)]

    meta_bits = []
    if date_txt:
        meta_bits.append("发布日期：" + date_txt)
    if source:
        meta_bits.append("来源：" + html.escape(source))
    meta_line = "&nbsp;&nbsp;|&nbsp;&nbsp;".join(meta_bits)

    body_html = "".join(
        '<p style="margin:0 0 8px;font-family:{font};font-size:13px;color:{ink};'
        'line-height:22px;mso-line-height-rule:exactly;word-wrap:break-word;'
        'word-break:break-word;">{p}</p>'.format(font=FONT, ink=C_INK, p=p)
        for p in paragraphs
    ) or ('<p style="margin:0 0 8px;font-family:{font};font-size:13px;color:{muted};'
          'line-height:22px;">（正文详情请点击下方链接查看）</p>'.format(font=FONT, muted=C_MUTED))

    new_badge = _badge("NEW", "#fff1f0", C_ACCENT, "#ffccc7") if is_new else ""
    more_label = "阅读全文" if truncated else "查看原文"

    return (
        '<table role="presentation" border="0" cellpadding="0" cellspacing="0" width="100%" '
        'bgcolor="{card}" style="width:100%;background:{card};border:1px solid {line};'
        'border-radius:8px;margin:0 0 12px;">'
        '<tr>'
        '<td width="86" valign="top" class="chip-cell" style="width:86px;padding:18px 0 18px 18px;">{chip}</td>'
        '<td valign="top" style="padding:18px 18px 14px 14px;">'
        '<a href="{url}" target="_blank" style="font-family:{font};font-size:15px;font-weight:bold;'
        'color:{deep};text-decoration:none;line-height:24px;mso-line-height-rule:exactly;'
        'word-wrap:break-word;word-break:break-word;">{title}</a>{badge}'
        '<div style="font-family:{font};font-size:12px;color:{muted};padding:6px 0 10px;'
        'line-height:18px;mso-line-height-rule:exactly;">{meta}</div>'
        '{body}'
        '<div style="padding:2px 0 0;font-family:{font};font-size:12px;">'
        '<a href="{url}" target="_blank" style="color:{primary};text-decoration:none;">{more} &rarr;</a>'
        '</div>'
        '</td>'
        '</tr></table>'
    ).format(card=C_CARD, line=C_LINE, chip=_date_chip(dt), font=FONT, deep=C_DEEP,
             muted=C_MUTED, primary=C_PRIMARY,
             url=html.escape(url, quote=True), title=html.escape(title),
             badge=new_badge, meta=meta_line, body=body_html, more=more_label)


def _section(title, subtitle, more_url, accent, items, base_url, max_chars, new_days_ts):
    cards = "".join(
        _article_card(it, base_url, max_chars,
                      is_new=bool(it.get("dt") and it["dt"].timestamp() >= new_days_ts))
        for it in items
    )
    if not cards:
        cards = (
            '<table role="presentation" border="0" cellpadding="0" cellspacing="0" width="100%" '
            'bgcolor="{card}" style="width:100%;background:{card};border:1px dashed {line};'
            'border-radius:8px;"><tr><td align="center" style="padding:28px;font-family:{font};'
            'font-size:13px;color:{muted};">本次未获取到数据</td></tr></table>'
        ).format(card=C_CARD, line=C_LINE, font=FONT, muted=C_MUTED)
    return ('<tr><td style="padding:22px 24px 4px;" class="pad">{head}{cards}</td></tr>'
            .format(head=_section_header(title, subtitle, more_url, accent), cards=cards))


def _stat_cell(label, value, color):
    return (
        '<td align="center" valign="middle" width="33%" style="padding:2px 6px;">'
        '<div style="font-family:{font};font-size:22px;font-weight:bold;color:{color};'
        'line-height:28px;mso-line-height-rule:exactly;">{value}</div>'
        '<div style="font-family:{font};font-size:12px;color:{muted};line-height:18px;'
        'mso-line-height-rule:exactly;">{label}</div>'
        '</td>'
    ).format(font=FONT, color=color, muted=C_MUTED,
             value=html.escape(str(value)), label=html.escape(label))


# ══════════════════════════════════════════════════════════
#  纯文本版（multipart/alternative 的另一半）
# ══════════════════════════════════════════════════════════
_RE_FRAG_ANCHOR = re.compile(r'<a\b[^>]*?href="(.*?)"[^>]*>(.*?)</a>', re.I | re.S)


def _frag_to_text(fragment):
    """
    把渲染好的段落片段转回纯文本。链接要还原成完整地址 ——
    HTML 版靠 href 兜底所以可以缩短显示，纯文本版没有 href 可点。
    """
    def _unwrap(m):
        url = html.unescape(m.group(1))
        label = html.unescape(_RE_TAG.sub("", m.group(2)))
        if not label or label.rstrip("…/") in url:
            return url
        return "{}（{}）".format(label, url)

    return html.unescape(_RE_TAG.sub("", _RE_FRAG_ANCHOR.sub(_unwrap, fragment)))


def build_plain_text(notices, features, ctx):
    """
    只发 HTML 的邮件在部分网关会被判定为垃圾邮件，同时带一份纯文本更稳妥。
    支持 HTML 的客户端（Outlook / 手机）仍然只会显示上面那份 HTML。
    """
    now = ctx.get("fetch_time") or datetime.now()
    base_url = ctx.get("base_url", "https://www.singlewindow.cn")
    max_chars = int(ctx.get("detail_max_chars", 900))

    out = ["中国国际贸易单一窗口 · 每日监控简报",
           "{}年{}月{}日 {}　抓取于 {}".format(now.year, now.month, now.day,
                                          _WEEK_CN[now.weekday()],
                                          now.strftime("%Y-%m-%d %H:%M")),
           "=" * 46]

    for name, items in (("最新动态（通知公告）", notices), ("新特性", features)):
        out += ["", "【{}】共 {} 条".format(name, len(items)), "-" * 46]
        if not items:
            out.append("  本次未获取到数据")
            continue
        for idx, item in enumerate(items, 1):
            dt = item.get("dt")
            out.append("{}. {}{}".format(
                idx, clean_title(item.get("title")),
                "（{}）".format(dt.strftime("%Y-%m-%d")) if dt else ""))
            paragraphs, _ = body_to_paragraphs(item.get("body"), base_url, max_chars)
            if not paragraphs:
                summary = clean_title(item.get("summary"))
                paragraphs = [html.escape(summary, quote=False)] if summary else []
            for para in paragraphs:
                out.append("   " + _frag_to_text(para))
            out.append("   原文：" + (item.get("url") or base_url))
            out.append("")

    out += ["=" * 46,
            "本邮件由监控脚本自动抓取 {} 生成，内容以官网原文为准。".format(base_url),
            "数据来源：{}".format(ctx.get("source_label", "")),
            "生成时间：{}".format(now.strftime("%Y-%m-%d %H:%M:%S"))]
    return "\n".join(out)


# ══════════════════════════════════════════════════════════
#  主入口
# ══════════════════════════════════════════════════════════
def build_email_html(notices, features, ctx):
    """
    notices / features: [{title, url, dt, summary, body, source}, ...]
    ctx: {base_url, notice_more_url, feature_more_url, fetch_time(datetime),
          source_label, detail_max_chars, new_days}
    """
    now = ctx.get("fetch_time") or datetime.now()
    base_url = ctx.get("base_url", "https://www.singlewindow.cn")
    max_chars = int(ctx.get("detail_max_chars", 900))
    new_days = int(ctx.get("new_days", 3))
    new_ts = now.timestamp() - new_days * 86400

    date_line = "{}年{}月{}日 {}".format(now.year, now.month, now.day, _WEEK_CN[now.weekday()])
    fetch_line = now.strftime("%Y-%m-%d %H:%M")
    new_count = sum(1 for it in list(notices) + list(features)
                    if it.get("dt") and it["dt"].timestamp() >= new_ts)

    preheader = "最新动态 {} 条 · 新特性 {} 条 · 近{}天新增 {} 条 · 抓取于 {}".format(
        len(notices), len(features), new_days, new_count, fetch_line)

    sections = (
        _section("最新动态", "通知公告", ctx.get("notice_more_url", base_url),
                 C_ACCENT, notices, base_url, max_chars, new_ts)
        + _section("新特性", "系统更新", ctx.get("feature_more_url", base_url),
                   C_PRIMARY, features, base_url, max_chars, new_ts)
    )

    return """<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN" "http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd">
<html xmlns="http://www.w3.org/1999/xhtml" lang="zh-CN">
<head>
<meta charset="UTF-8"/>
<meta http-equiv="Content-Type" content="text/html; charset=UTF-8"/>
<meta http-equiv="X-UA-Compatible" content="IE=edge"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<meta name="format-detection" content="telephone=no,date=no,address=no,email=no"/>
<meta name="color-scheme" content="light"/>
<meta name="supported-color-schemes" content="light"/>
<title>中国国际贸易单一窗口 · 每日监控</title>
<!--[if mso]>
<xml><o:OfficeDocumentSettings><o:PixelsPerInch>96</o:PixelsPerInch>
<o:AllowPNG/></o:OfficeDocumentSettings></xml>
<style>table,td,div,p,a{{font-family:"Microsoft YaHei",SimSun,Arial,sans-serif !important;}}</style>
<![endif]-->
<style type="text/css">
  html,body{{margin:0 !important;padding:0 !important;width:100% !important;}}
  body{{-webkit-text-size-adjust:100%;-ms-text-size-adjust:100%;}}
  table{{border-collapse:collapse !important;mso-table-lspace:0pt;mso-table-rspace:0pt;}}
  img{{border:0;outline:none;line-height:100%;-ms-interpolation-mode:bicubic;}}
  a{{text-decoration:none;}}
  a:hover{{text-decoration:underline !important;}}
  /* 部分客户端会把日期/数字自动变成蓝色链接，这里统一压回来 */
  a[x-apple-data-detectors]{{color:inherit !important;text-decoration:none !important;
    font-size:inherit !important;font-family:inherit !important;font-weight:inherit !important;}}
  u+#body a{{color:inherit;text-decoration:none;}}
  @media only screen and (max-width:620px) {{
    .container{{width:100% !important;max-width:100% !important;}}
    .pad{{padding-left:12px !important;padding-right:12px !important;}}
    .chip-cell{{width:66px !important;padding-left:12px !important;}}
    .hide-sm{{display:none !important;}}
  }}
</style>
</head>
<body id="body" bgcolor="{bg}" style="margin:0;padding:0;background:{bg};">
<div style="display:none;font-size:1px;line-height:1px;max-height:0;max-width:0;opacity:0;overflow:hidden;mso-hide:all;">{preheader}</div>
<div style="display:none;font-size:1px;line-height:1px;max-height:0;max-width:0;opacity:0;overflow:hidden;mso-hide:all;">&#847;&zwnj;&nbsp;&#847;&zwnj;&nbsp;&#847;&zwnj;&nbsp;&#847;&zwnj;&nbsp;</div>

<table role="presentation" border="0" cellpadding="0" cellspacing="0" width="100%" bgcolor="{bg}" style="width:100%;background:{bg};">
<tr><td align="center" style="padding:24px 10px 32px;">

<!--[if mso]><table role="presentation" border="0" cellpadding="0" cellspacing="0" width="680" align="center"><tr><td><![endif]-->
<table role="presentation" border="0" cellpadding="0" cellspacing="0" width="680" class="container" style="width:680px;max-width:680px;">

  <!-- 页眉 -->
  <tr><td bgcolor="{deep}" style="background:{deep};padding:22px 24px;border-radius:8px 8px 0 0;" class="pad">
    <table role="presentation" border="0" cellpadding="0" cellspacing="0" width="100%" style="width:100%;">
    <tr>
      <td valign="middle">
        <div style="font-family:{font};font-size:19px;font-weight:bold;color:#ffffff;line-height:28px;mso-line-height-rule:exactly;">中国国际贸易单一窗口</div>
        <div style="font-family:{font};font-size:12px;color:#a9c4e4;line-height:20px;mso-line-height-rule:exactly;padding-top:2px;">最新动态 &amp; 新特性 · 每日监控简报</div>
      </td>
      <td valign="middle" align="right" class="hide-sm">
        <div style="font-family:{font};font-size:13px;color:#ffffff;line-height:20px;mso-line-height-rule:exactly;">{date_line}</div>
        <div style="font-family:{font};font-size:12px;color:#a9c4e4;line-height:18px;mso-line-height-rule:exactly;">抓取于 {fetch_line}</div>
      </td>
    </tr>
    </table>
  </td></tr>

  <!-- 数据概览 -->
  <tr><td bgcolor="{card}" style="background:{card};padding:16px 24px;border-left:1px solid {line};border-right:1px solid {line};border-bottom:1px solid {line};" class="pad">
    <table role="presentation" border="0" cellpadding="0" cellspacing="0" width="100%" style="width:100%;">
    <tr>
      {stat_notice}
      {stat_feature}
      {stat_new}
    </tr>
    </table>
  </td></tr>

  {sections}

  <!-- 页脚 -->
  <tr><td style="padding:14px 24px 0;" class="pad">
    <table role="presentation" border="0" cellpadding="0" cellspacing="0" width="100%" bgcolor="#f6f8fb" style="width:100%;background:#f6f8fb;border:1px solid {line};border-radius:8px;">
    <tr><td style="padding:14px 18px;font-family:{font};font-size:12px;color:{muted};line-height:20px;mso-line-height-rule:exactly;">
      本邮件由监控脚本自动抓取
      <a href="{base_url}" target="_blank" style="color:{primary};text-decoration:none;">中国国际贸易单一窗口官网</a>
      生成，正文摘自官网原文，如有出入请以
      <a href="{notice_more}" target="_blank" style="color:{primary};text-decoration:none;">官网公告</a>
      为准。<br/>
      数据来源：{source_label} &nbsp;|&nbsp; 生成时间：{fetch_full}
    </td></tr>
    </table>
  </td></tr>

</table>
<!--[if mso]></td></tr></table><![endif]-->

</td></tr>
</table>
</body>
</html>""".format(
        bg=C_BG, card=C_CARD, deep=C_DEEP, line=C_LINE, muted=C_MUTED,
        primary=C_PRIMARY, font=FONT,
        preheader=html.escape(preheader),
        date_line=html.escape(date_line),
        fetch_line=html.escape(fetch_line),
        fetch_full=html.escape(now.strftime("%Y-%m-%d %H:%M:%S")),
        stat_notice=_stat_cell("最新动态", len(notices), C_ACCENT),
        stat_feature=_stat_cell("新特性", len(features), C_PRIMARY),
        stat_new=_stat_cell("近{}天新增".format(new_days), new_count, "#389e0d"),
        sections=sections,
        base_url=html.escape(base_url, quote=True),
        notice_more=html.escape(ctx.get("notice_more_url", base_url), quote=True),
        source_label=html.escape(str(ctx.get("source_label", ""))),
    )
