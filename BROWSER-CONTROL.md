# 用 Claude Code 操作浏览器

让 Claude Code 桌面客户端控制 Chrome：打开网页、登录、填表、点击、抓数据。

能力来自 [chrome-devtools-mcp](https://github.com/ChromeDevTools/chrome-devtools-mcp)（29 个工具），
配套的 6 个 skill 教 Claude 怎么正确使用这些工具。

---

## 装

### 前置

- **Node.js** LTS（`node --version` 能输出即可）
- **Google Chrome** 稳定版

### 一键装（推荐）

在自己电脑上 clone 本仓库后，在仓库根目录运行：

```bash
# macOS / Linux
bash setup-browser-control.sh
```

```powershell
# Windows
powershell -ExecutionPolicy Bypass -File setup-browser-control.ps1
```

脚本做三件事：把 `.claude/skills/` 下的 6 个 skill 复制到 `~/.claude/skills/`（用户级，
任何目录都能用）、注册 MCP server、检查依赖。已存在的 skill 不会被覆盖。

装完**重启 Claude Code**，然后 `/mcp` 确认 `chrome-devtools` 是 connected。

### 只想在本仓库用

不用跑脚本。仓库里的 `.mcp.json` 和 `.claude/skills/` 已经配好了，
在本目录打开 Claude Code 时会自动生效（首次会提示你批准 `.mcp.json`）。

### 手动装

```bash
claude mcp add chrome-devtools --scope user npx chrome-devtools-mcp@latest
cp -r .claude/skills/* ~/.claude/skills/
```

---

## 用

装好后直接说人话就行：

> 打开 example.com，登录后把订单列表导出成 CSV

Claude 会按 `navigate_page` → `wait_for` → `take_snapshot` → `click`/`fill` 的流程执行。
每一步都会弹权限确认 —— **建议保留这个默认行为**，尤其涉及提交表单、删除数据时。

### 登录态怎么处理

**不要把账号密码写进 prompt、`CLAUDE.md` 或 skill 文件。**

默认模式下 chrome-devtools-mcp 会用一个**持久化 Chrome profile**，
你在它打开的窗口里手动登录一次，cookie 就会一直留着，之后的会话都不用再登录。
遇到验证码或 2FA 时，让 Claude 停下来等你手动完成再继续。

### 复用你现有 Chrome 的登录态（可选）

如果想直接用你日常 Chrome 里已经登录好的账号，改成连接模式。

先**完全退出 Chrome**，再用调试端口启动：

```bash
# macOS
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
  --remote-debugging-port=9222 --user-data-dir="$HOME/chrome-debug-profile"
```

```powershell
# Windows
& "C:\Program Files\Google\Chrome\Application\chrome.exe" `
  --remote-debugging-port=9222 --user-data-dir="$env:USERPROFILE\chrome-debug-profile"
```

> Chrome 136 起**禁止对默认 profile 开调试端口**，所以 `--user-data-dir` 必须指向一个
> 非默认目录。第一次在这个窗口里手动登录目标网站即可。

验证端口通了：浏览器访问 `http://127.0.0.1:9222/json/version`，能看到 JSON 就成功。

然后给 MCP server 加上 `--browserUrl`：

```bash
claude mcp remove chrome-devtools --scope user
claude mcp add chrome-devtools --scope user \
  npx chrome-devtools-mcp@latest --browserUrl http://127.0.0.1:9222
```

本仓库内使用的话，改 `.mcp.json` 的 `args` 即可：

```json
["chrome-devtools-mcp@latest", "--browserUrl", "http://127.0.0.1:9222"]
```

### 其他常用 flag

| Flag | 作用 |
|---|---|
| `--isolated` | 用临时 profile，关闭后自动清理（不保留登录态） |
| `--headless` | 无界面运行 |
| `--channel canary\|dev\|beta\|stable` | 指定 Chrome 渠道 |
| `--memoryDebugging` | 开启内存分析工具 |
| `--categoryExtensions` | 开启扩展调试工具 |

---

## 安全

1. **提示注入**：网页内容是不可信输入。页面上若有"忽略之前的指令，去做 X"之类的文字，
   Claude 可能被带偏。**别用有支付权限或管理员权限的账号跑自动化。**
2. **权限范围**：连上带登录态的浏览器后，Claude 能读取该 profile 里的**所有**站点数据，
   等同于以你的身份行事。建议用专门的调试 profile，只登录需要操作的那几个站点。
3. **别提交 profile 目录**：`chrome-debug-profile/` 里有 cookie 和登录凭据，
   已在 `.gitignore` 中排除。

---

## 排查

连不上、启动失败时，直接让 Claude 用 `troubleshooting` skill，或查
[官方排查文档](https://github.com/ChromeDevTools/chrome-devtools-mcp/blob/main/docs/troubleshooting.md)。

常见问题：

- **`/mcp` 里 chrome-devtools 是 failed** —— 多半是 Node 版本太老，或 npx 拉包被网络挡了
- **连不上 9222** —— Chrome 没完全退干净，或者忘了加 `--user-data-dir`
- **元素点不到** —— 页面变了，重新 `take_snapshot` 拿新的 `uid`
