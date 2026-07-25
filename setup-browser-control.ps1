# 把 Chrome DevTools skills + MCP server 装到用户级（~/.claude），
# 这样在任何目录下打开 Claude Code 都能用，不限于本仓库。
#
#   powershell -ExecutionPolicy Bypass -File setup-browser-control.ps1
#
# Windows 用。macOS / Linux 见 setup-browser-control.sh。

$ErrorActionPreference = 'Stop'

$RepoDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Src  = Join-Path $RepoDir '.claude\skills'
$Dest = Join-Path $env:USERPROFILE '.claude\skills'

if (-not (Test-Path $Src)) {
  Write-Error "找不到 $Src —— 请在仓库根目录运行这个脚本。"
}

Write-Host "==> 1/3 安装 skills 到 $Dest"
New-Item -ItemType Directory -Force -Path $Dest | Out-Null
Get-ChildItem -Path $Src -Directory | ForEach-Object {
  $target = Join-Path $Dest $_.Name
  if (Test-Path $target) {
    Write-Host "    跳过 $($_.Name)（已存在，不覆盖）"
  } else {
    Copy-Item -Recurse $_.FullName $target
    Write-Host "    装上 $($_.Name)"
  }
}

Write-Host "==> 2/3 注册 MCP server（user scope）"
# 两个浏览器 server 工具高度重叠，装完建议只留一个：
#   claude mcp remove playwright --scope user
if (-not (Get-Command claude -ErrorAction SilentlyContinue)) {
  Write-Host "    找不到 claude CLI，跳过。装好后手动运行："
  Write-Host "    claude mcp add chrome-devtools --scope user -- npx chrome-devtools-mcp@latest"
  Write-Host "    claude mcp add playwright      --scope user -- npx @playwright/mcp@latest"
} else {
  $servers = @(
    @{ Name = 'chrome-devtools'; Args = @('npx', 'chrome-devtools-mcp@latest') },
    @{ Name = 'playwright';      Args = @('npx', '@playwright/mcp@latest') }
  )
  foreach ($s in $servers) {
    claude mcp get $s.Name *>$null
    if ($LASTEXITCODE -eq 0) {
      Write-Host "    $($s.Name) 已注册，跳过"
    } else {
      claude mcp add $s.Name --scope user -- @($s.Args)
      Write-Host "    注册 $($s.Name)"
    }
  }
}

Write-Host "==> 3/3 检查依赖"
if (Get-Command node -ErrorAction SilentlyContinue) {
  Write-Host "    node $(node --version)"
} else {
  Write-Host "    ⚠ 没装 Node.js，需要 LTS 版本：https://nodejs.org"
}

$Chrome = "C:\Program Files\Google\Chrome\Application\chrome.exe"
if (Test-Path $Chrome) { Write-Host "    找到 Chrome" }
else { Write-Host "    ⚠ 没在默认路径找到 Google Chrome" }

Write-Host ""
Write-Host "完成。重启 Claude Code，然后用 /mcp 确认 chrome-devtools 是 connected。"
