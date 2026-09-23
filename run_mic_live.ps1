# 边说边出字：从麦克风实时识别（自动增益，麦克风音量不用调）
# 直接跑这个文件即可：右键"使用 PowerShell 运行"，或在终端里 .\run_mic_live.ps1
param(
    [double]$Seconds = 60,
    [int]$Device = 1
)
$ErrorActionPreference = 'Continue'
$root = $PSScriptRoot
$py = Join-Path $root 'venv\Scripts\python.exe'
if (-not (Test-Path $py)) {
    Write-Host "找不到 venv 的 python：$py" -ForegroundColor Red
    Write-Host "不要用系统全局的 python——它没装 funasr/torch。" -ForegroundColor Yellow
    exit 1
}
Write-Host "麦克风实时识别（$Seconds 秒，设备 $Device）。开始说话就行。" -ForegroundColor Cyan
& $py (Join-Path $root 'scripts\mic_live.py') --seconds $Seconds --device $Device
