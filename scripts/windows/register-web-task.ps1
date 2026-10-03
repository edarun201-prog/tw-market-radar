# 註冊 Windows 工作排程器：每次登入時自動啟動網站（http://127.0.0.1:8000/），可重複執行，會覆蓋同名工作。
#
#   powershell -ExecutionPolicy Bypass -File scripts\windows\register-web-task.ps1
#
# 網站開著才看得到盤中即時；平常約占 85 MB 記憶體。只接受本機連線（127.0.0.1）。
# 用 pythonw.exe 執行，不會跳出主控台視窗；紀錄寫在 data\logs\web.log。登入後延後 1 分鐘，讓 PostgreSQL 先啟動；
# 意外結束時每分鐘重試，最多 3 次。
# 立刻啟動：Start-ScheduledTask -TaskName "tw-market-radar web"
# 停止：    Stop-ScheduledTask -TaskName "tw-market-radar web"
# 移除：    Unregister-ScheduledTask -TaskName "tw-market-radar web" -Confirm:$false

$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$pipeline = Join-Path $root "services\pipeline"
$python = Join-Path $pipeline ".venv\Scripts\pythonw.exe"
$logDir = Join-Path $root "data\logs"
if (-not (Test-Path $python)) { throw "找不到 $python，請先照 README 建立 .venv" }
New-Item -ItemType Directory -Force $logDir | Out-Null

$action = New-ScheduledTaskAction -Execute $python `
    -Argument "-m radar --log-file `"$logDir\web.log`" web --host 127.0.0.1 --port 8000" -WorkingDirectory $pipeline
$logon = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$logon.Delay = "PT1M"
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -MultipleInstances IgnoreNew `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName "tw-market-radar web" -Action $action -Trigger $logon -Settings $settings `
    -Description "台股市場雷達：登入時啟動網站（http://127.0.0.1:8000/），含盤中即時" -Force | Out-Null
Write-Host "已註冊「tw-market-radar web」。網站：http://127.0.0.1:8000/  紀錄：$logDir\web.log"
