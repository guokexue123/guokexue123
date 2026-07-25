#!/usr/bin/env bash
# 把 Chrome DevTools skills + MCP server 装到用户级（~/.claude），
# 这样在任何目录下打开 Claude Code 都能用，不限于本仓库。
#
#   bash setup-browser-control.sh
#
# macOS / Linux 用。Windows 见 setup-browser-control.ps1。

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$REPO_DIR/.claude/skills"
DEST="$HOME/.claude/skills"

if [ ! -d "$SRC" ]; then
  echo "找不到 $SRC —— 请在仓库根目录运行这个脚本。" >&2
  exit 1
fi

echo "==> 1/3 安装 skills 到 $DEST"
mkdir -p "$DEST"
for d in "$SRC"/*/; do
  name="$(basename "$d")"
  if [ -e "$DEST/$name" ]; then
    echo "    跳过 $name（已存在，不覆盖）"
  else
    cp -r "$d" "$DEST/$name"
    echo "    装上 $name"
  fi
done

echo "==> 2/3 注册 MCP server（user scope）"
# 两个浏览器 server 工具高度重叠，装完建议只留一个：
#   claude mcp remove playwright --scope user
if ! command -v claude >/dev/null 2>&1; then
  echo "    找不到 claude CLI，跳过。装好后手动运行："
  echo "    claude mcp add chrome-devtools --scope user -- npx chrome-devtools-mcp@latest"
  echo "    claude mcp add playwright      --scope user -- npx @playwright/mcp@latest"
else
  add_server() {
    local name="$1"; shift
    if claude mcp get "$name" >/dev/null 2>&1; then
      echo "    $name 已注册，跳过"
    else
      claude mcp add "$name" --scope user -- "$@"
      echo "    注册 $name"
    fi
  }
  add_server chrome-devtools npx chrome-devtools-mcp@latest
  add_server playwright      npx @playwright/mcp@latest
fi

echo "==> 3/3 检查依赖"
command -v node >/dev/null 2>&1 \
  && echo "    node $(node --version)" \
  || echo "    ⚠ 没装 Node.js，需要 LTS 版本：https://nodejs.org"

if [ "$(uname)" = "Darwin" ]; then
  CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
  [ -x "$CHROME" ] && echo "    Chrome $("$CHROME" --version 2>/dev/null)" \
                   || echo "    ⚠ 没找到 Google Chrome"
fi

echo
echo "完成。重启 Claude Code，然后用 /mcp 确认 chrome-devtools 是 connected。"
