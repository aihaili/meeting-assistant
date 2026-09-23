# 启动实时会议助理（界面 + 音频 + 检索，单进程）
#
# 用法：
#   .\run_meeting.ps1                     # 默认：新会话，本机麦克风
#   .\run_meeting.ps1 -Session data\sessions\demo.json   # 打开已有会话
#   .\run_meeting.ps1 -Port 8500          # 换 HTTP 端口（默认 8510）
#   .\run_meeting.ps1 -NoLlm              # 只用规则分类线索（更快、可离线）
#   .\run_meeting.ps1 -NoAsr              # 只看界面，不起音频接收
#
# 没有麦克风时：起服务后点界面右上角「试听回放」，填一个 WAV 路径即可。

param(
    [int]$Port = 8510,
    [string]$Session = '',
    [string]$Title = '',
    # 索引默认用合成会议纪要库（meet.db），因为演示音频讲的就是这些内容。
    [string]$Db = '',
    [string]$Kb = '',
    [int]$TopK = 3,
    [switch]$NoLlm,
    [switch]$NoAsr,
    [switch]$Fresh
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$py = Join-Path $root 'venv\Scripts\python.exe'
if (-not (Test-Path $py)) { throw "找不到 venv 解释器: $py" }

# 端口占用先检查。这个项目里 8500 常年被旧的 asst\server.py 占着，
# 直接启动会得到一个 EADDRINUSE，看起来像代码问题。
$busy = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($busy) {
    $owner = ($busy.OwningProcess | Select-Object -First 1)
    $pname = (Get-Process -Id $owner -ErrorAction SilentlyContinue).ProcessName
    Write-Host "端口 $Port 已被 PID $owner ($pname) 占用。" -ForegroundColor Yellow
    $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId=$owner" -ErrorAction SilentlyContinue).CommandLine
    if ($cmd) { Write-Host "  $cmd" -ForegroundColor DarkGray }
    Write-Host "换端口：.\run_meeting.ps1 -Port 8511   或先停掉它。" -ForegroundColor Yellow
    exit 1
}

$args = @('-u', '-m', 'meeting.server', '--port', "$Port", '--top-k', "$TopK")
if ($Session) { $args += @('--session', (Resolve-Path $Session).Path) }
if ($Fresh)   { $args += '--fresh' }
if ($Title)   { $args += @('--title', $Title) }
if ($NoLlm)   { $args += '--no-llm' }
if ($NoAsr)   { $args += '--no-asr' }
if ($Db) { $args += @('--db', (Resolve-Path $Db).Path) }
if ($Kb) { $args += @('--kb', (Resolve-Path $Kb).Path) }

$env:PYTHONIOENCODING = 'utf-8'
Push-Location (Join-Path $root 'scripts')
try {
    Write-Host "→ http://127.0.0.1:$Port/" -ForegroundColor Cyan
    & $py @args
} finally {
    Pop-Location
}
