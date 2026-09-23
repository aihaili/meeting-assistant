# 启动早期独立助手（默认 8500 端口；不是实时会议助理，那个走 run_meeting.ps1:8510）
# 用法: .\run_asst.ps1 [-Port 8500] [-Db <index.db>] [-Kb <corpus dir>]
#
# 服务本身是常驻的：RAG 索引 + ONNX int8 嵌入器启动时加载一次。
# 实测 冷启动 0.96s / 热查询 5-16ms —— 每次请求现起进程会丢掉这个优势。
param(
    [int]$Port = 8500,
    [string]$Db = "E:\markdown\meeting-assistant\data\ar.db",
    # 之前这里没声明 $Kb 却在下面用了 → 命令行实际变成 `--kb --port 8500`，argparse 报错。
    [string]$Kb = "",
    [switch]$NoLlm
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$py = Join-Path $root "venv\Scripts\python.exe"
if (-not (Test-Path $py)) { Write-Error "venv 不存在: $py" }

# 端口冲突检查：本机跑着多个 llama.cpp，先看目标端口是否被占
$busy = (Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue).LocalPort
if ($busy -contains $Port) {
    Write-Error "端口 $Port 已被占用。用 -Port 换一个（空闲参考: 8500 8510 8520 8600 8800 8900 18000）"
}

# 嵌入模型走 HF 缓存，离线加载（huggingface.co 在本机被 DNS 劫持）
$env:HF_HUB_OFFLINE = "1"
$env:TRANSFORMERS_OFFLINE = "1"
$env:HF_HUB_DISABLE_SYMLINKS_WARNING = "1"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$extra = @()
if ($NoLlm) { $extra += "--no-llm" }
# --kb 只在真的给了语料目录时才传：空串会让 argparse 把它当成一个"空路径"参数。
$kbArgs = @()
if ($Kb) { $kbArgs = @("--kb", (Resolve-Path $Kb).Path) }

& $py -u (Join-Path $root "scripts\asst\server.py") --db $Db @kbArgs --port $Port @extra
