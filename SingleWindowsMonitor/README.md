# 单一窗口每日监控简报

抓取「中国国际贸易单一窗口」官网的 **最新动态（通知公告）** 与 **新特性** 两个栏目，
生成一封排版整洁、可直接发给领导的 HTML 邮件。

## 这次优化了什么

| | 之前 | 现在 |
|---|---|---|
| 展示内容 | 首页两张卡片 + 下方整页「通知公告」列表 | 只保留 **最新动态** 和 **新特性** 两块 |
| 正文 | 只有 120 字摘要，还常混进页面上的搜索框文字 | 调详情接口取 **正文全文**，默认展示 900 字 |
| 链接 | `https://www.singlewindow.cn#/detail?...`（少一个 `/`） | 规范的 `https://www.singlewindow.cn/#/detail?...`，正文里的引用链接也一并保留 |
| 取数方式 | Playwright 起浏览器渲染 + 解析 DOM（约 40 秒，易受渲染变化影响） | 直接调官网 JSON 接口（约 3 秒），浏览器方案降级为兜底 |
| 页面骨架 | 仿官网整站（顶栏、导航、搜索框、页脚），div + flex + 外部 CSS | 邮件专用模板：table 布局 + 全内联样式，Outlook / 手机都不会散版 |
| 日期 | 跟随服务器时区，可能差一天 | 统一按北京时间换算 |
| 邮件标题 | 未做 RFC 2047 编码 | 中文主题正确编码，并补上 `Date` / `Message-ID` |
| 邮件格式 | 只有 HTML 一份 | HTML + 纯文本双份（`multipart/alternative`），降低被网关判垃圾的概率 |
| 抓取失败 | 静默发一封空邮件，退出码仍是 0 | 接口自动重试 3 次；两个栏目都空时退出码 1，计划任务里能看到失败 |
| 排错 | 只能等第二天的邮件 | `--self-test` 一次性检查接口 / 依赖 / SMTP / 目录权限 |

## 文件说明

| 文件 | 作用 |
|---|---|
| `singlewindow_scraper.py` | 入口：取数（接口优先，浏览器兜底）→ 生成 HTML → 发邮件 |
| `sw_render.py` | 邮件 UI 模板。**想改配色、间距、字号，只改这个文件** |
| `email_sender.py` | SMTP 发送 |
| `singlewindow_output.html` | 每次运行生成的邮件正文（本地预览用） |

## 安装

```bash
pip install -r requirements.txt
# 只有走浏览器兜底方案时才需要，接口正常时可以不装：
python -m playwright install chromium
```

Python 3.8+。接口方案只用标准库（`urllib` + `json`），不依赖第三方包。

## 运行

```bash
python singlewindow_scraper.py                  # 抓取 + 生成 HTML + 发邮件（生产用法）
python singlewindow_scraper.py --self-test      # 部署到新机器后先跑这个（不抓取、不发信）
python singlewindow_scraper.py --no-mail        # 只生成 HTML，不发邮件（调样式时用）
python singlewindow_scraper.py --notices 8 --features 8
python singlewindow_scraper.py --detail-chars 1500     # 正文展示更长；0 = 不截断
python singlewindow_scraper.py --new-days 7            # 7 天内发布的标 NEW
python singlewindow_scraper.py --to a@dhl.com --to b@dhl.com
python singlewindow_scraper.py --browser-only          # 强制走浏览器兜底方案
```

Windows 计划任务原来怎么配就还怎么配，入口文件名和输出文件名都没变。

### 退出码

| 值 | 含义 |
|---|---|
| 0 | 正常，至少抓到一条内容 |
| 1 | 两个栏目都没抓到（或自检有关键项没过）——计划任务会显示为失败 |

### 部署到新机器 / 出问题时

```
> python singlewindow_scraper.py --self-test

  [OK  ] 官网列表接口（最新动态）  —— 取到 1 条
  [OK  ] 官网详情接口（正文全文）  —— 正文 129 字
  [OK  ] 兜底依赖 playwright  —— 接口不可用时用它渲染首页
  [OK  ] 兜底依赖 bs4  —— 解析首页 DOM
  [OK  ] SMTP 连通性 smtp.cn.dhl.com:25  —— 只握手，未发信
  [OK  ] 输出目录可写 D:\Monitor\SingleWindowsMonitor
```

兜底依赖没装不算致命（接口通就能正常出报），其余任意一项 FAIL 都要先处理。

## 常用配置

改 `singlewindow_scraper.py` 顶部的常量即可：

```python
NOTICE_COUNT     = 5      # 最新动态展示条数
FEATURE_COUNT    = 5      # 新特性展示条数
DETAIL_MAX_CHARS = 900    # 每条正文最多展示多少字（0 = 不截断）
NEW_DAYS         = 3      # 几天内发布的标 NEW

SMTP_SERVER  = "smtp.cn.dhl.com"
SMTP_PORT    = 25
SENDER_EMAIL = "singlewindow@dhl.com"
RECEIVERS    = ["kexue.guo@dhl.com"]
CC           = []
```

配色在 `sw_render.py` 顶部（`C_DEEP` / `C_ACCENT` / `C_PRIMARY` …），换一套值就能整体换肤。

## 数据来源

官网自己的 JSON 接口，前端页面用的也是这几个：

| 用途 | 接口 | 入参 |
|---|---|---|
| 栏目列表 | `POST /access/ui/SW-SITE-FRONT/Article001` | `{"catalogid":"tzgg","pageNo":1,"rowsPerPage":5}` |
| 文章详情 | `POST /access/ui/SW-SITE-FRONT/Article002` | `{"articleid":"..."}` → `bodytext` 为正文全文 |

栏目 ID：`tzgg` = 通知公告（最新动态），`xtx` = 新特性。
详情页链接：`https://www.singlewindow.cn/#/detail?breadNum=bc12&articleId=xxx`（新特性用 `bc13`）。

接口调不通时（超时、改版、被拦），脚本会自动降级为 Playwright 渲染首页抓取，
此时只有摘要没有全文，邮件页脚的「数据来源」会写明当次用的是哪种方式。

## 邮件模板为什么全是 `<table>`

领导多半用 Outlook 桌面版看邮件，它用 Word 排版引擎：不支持 flex / grid，
外部样式表和 `<style>` 里的大部分选择器都会被丢掉。所以模板遵循业界通用的
"bulletproof email" 写法，参考了 GitHub 上几个成熟的开源邮件框架：

- [**Cerberus**](https://github.com/TedGoas/Cerberus)（MIT）—— 单列骨架、`<!--[if mso]>` 条件注释包固定宽度表格、`role="presentation"`、`max-width` + 媒体查询的流式容器
- [**Foundation for Emails**](https://github.com/foundation/foundation-emails) —— 内容块（card）的内外边距节奏
- [**HTML Email Boilerplate**](https://github.com/seanpowell/Email-Boilerplate) —— Outlook / Gmail / 国内邮箱的兼容性重置

落到代码上是这几条规矩：

1. 布局一律 `<table role="presentation">`，不用 div 定位；
2. 样式全部内联，`<style>` 只放媒体查询（属于渐进增强，丢了也不影响可读）；
3. 背景色同时给 `bgcolor` 属性和 `style`，Outlook 只认前者；
4. 容器 680px，外层套 `<!--[if mso]>` 固定宽度表格；
5. 行高统一加 `mso-line-height-rule:exactly`；
6. 正文里的长网址会缩短显示（`href` 保持完整），避免 Outlook 里撑破表格；
7. 圆角、阴影只做锦上添花，Outlook 忽略后降级仍然好看。

已验证：Chromium 桌面宽度 760px 与手机宽度 400px 下均无横向滚动。
