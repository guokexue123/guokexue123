# Chrome DevTools Skills

来源：<https://github.com/ChromeDevTools/chrome-devtools-mcp>（Apache-2.0，见 `LICENSE`）

这些 skill 本身不提供浏览器控制能力，它们是**使用说明书** —— 真正的能力来自
`chrome-devtools` MCP server（在仓库根目录的 `.mcp.json` 里配置）。两者要一起装才有用。

| Skill | 作用 |
|---|---|
| `chrome-devtools` | 主 skill：浏览器自动化的核心工作流（navigate → wait → snapshot → interact） |
| `troubleshooting` | 连不上 Chrome、启动失败时的排查步骤 |
| `chrome-devtools-cli` | 用 CLI 方式管理后台浏览器进程 |
| `a11y-debugging` | 无障碍树调试 |
| `debug-optimize-lcp` | LCP / 加载性能分析 |
| `memory-leak-debugging` | 内存泄漏排查（需 `--memoryDebugging` flag） |

后三个是网页性能调试用的，跟"登录网站做操作"无关，但体积很小，留着不影响。
不需要可以直接删对应目录。

## 关键工作流（摘自 `chrome-devtools/SKILL.md`）

1. `navigate_page` 打开页面
2. `wait_for` 等待目标内容加载
3. `take_snapshot` 拿到页面结构和每个元素的 `uid`
4. 用 `uid` 执行 `click` / `fill` 等操作

元素找不到时**重新 snapshot** —— 页面可能已经变了。
