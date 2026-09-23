# 打包实时会议助理启动器为 exe（PyInstaller）
#
# 用法：
#     .\build_launcher.ps1             # 最终交付：**无控制台**（双击只有界面窗口）
#     .\build_launcher.ps1 -Console    # 调试用：保留控制台，能看到日志/报错
#
# 产物：launcher\dist\MeetingLauncher\MeetingLauncher.exe
# 说明：exe 为“薄启动器”——只打包 pywebview 编排层，不含 torch；
#       运行时用旁边仓库的 venv python 子进程拉起 meeting.server（需仓库结构完整），
#       子进程以 CREATE_NO_WINDOW 启动，所以不会再冒出命令行窗口。
param([switch]$Console)

$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot
$root = Split-Path -Parent $here
$py   = Join-Path $root 'venv\Scripts\python.exe'
$work = Join-Path $here 'build'
$dist = Join-Path $here 'dist'

$pyi = @(
    '-m', 'PyInstaller', '--noconfirm', '--clean',
    '--name', 'MeetingLauncher',
    "--distpath", $dist,
    "--workpath", $work,
    "--specpath", $here,
    '--collect-all', 'webview',
    '--collect-all', 'pythonnet',
    '--collect-all', 'clr_loader',
    "--add-data", "$here\launcher.html;.",
    "$here\app.py"
)
if (-not $Console) { $pyi += '--windowed' }

Write-Host "PyInstaller -> $dist  ($(if ($Console) { '带控制台（调试）' } else { '无控制台' }))" -ForegroundColor Cyan
# PyInstaller 把 INFO 日志写到 stderr。Windows PowerShell 在调用方重定向输出时会把那些行
# 当成错误记录，配合 $ErrorActionPreference='Stop' 会让脚本在这里直接中断（构建其实没跑完）。
# 所以这一步单独放成 Continue，只看退出码。
$prevEap = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
& $py @pyi
$code = $LASTEXITCODE
$ErrorActionPreference = $prevEap
if ($code -ne 0) { throw "PyInstaller failed ($code)" }

$exe = Join-Path $dist 'MeetingLauncher\MeetingLauncher.exe'
# 打包时把仓库根写进 launcher.json：启动器据此定位 venv（--clean 会清空 dist，
# 手放的那份会丢，所以这里由构建脚本生成。换机器时改这个文件即可）。
$cfg = Join-Path $dist 'MeetingLauncher\launcher.json'
# 用无 BOM 的 UTF-8 写：PowerShell 5.1 的 `Set-Content -Encoding UTF8` 会带 BOM，
# 而 BOM 会让启动器读 JSON 时解析失败（已让启动器兼容 BOM，但写的时候就不该有）。
$json = @{ repo_root = $root } | ConvertTo-Json
[System.IO.File]::WriteAllText($cfg, $json, (New-Object System.Text.UTF8Encoding($false)))
Write-Host ""
Write-Host "构建完成：" -ForegroundColor Green
Write-Host "  $exe"
Write-Host "  $cfg  ->  $root"
Write-Host ""
Write-Host "运行：把 dist\MeetingLauncher\ 整个目录放到仓库根（meeting-assistant\）旁，或就地运行；"
Write-Host "      双击即用——窗口先隐藏，服务就绪后第一眼就是会议界面。" -ForegroundColor DarkGray
