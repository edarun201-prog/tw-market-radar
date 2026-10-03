# 註冊 Windows 工作排程器：每天自動執行 `python -m radar daily`（可重複執行，會覆蓋同名工作）。
#
#   powershell -ExecutionPolicy Bypass -File scripts\windows\register-daily-task.ps1
#
# 觸發：平日 18:30，以及每次登入時（補上電腦關機、睡眠錯過的日子）。
# 用 pythonw.exe 執行：不會跳出主控台視窗（關掉視窗會中斷流程）。每次跑完就結束，平常不占記憶體；
# 紀錄由程式寫在 data\logs\daily.log。登入觸發延後 2 分鐘，讓 PostgreSQL 等服務先啟動。
# 移除：Unregister-ScheduledTask -TaskName "tw-market-radar daily" -Confirm:$false

$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$pipeline = Join-Path $root "services\pipeline"
$python = Join-Path $pipeline ".venv\Scripts\pythonw.exe"
$logDir = Join-Path $root "data\logs"
if (-not (Test-Path $python)) { throw "找不到 $python，請先照 README 建立 .venv" }
New-Item -ItemType Directory -Force $logDir | Out-Null

$action = New-ScheduledTaskAction -Execute $python -Argument "-m radar --log-file `"$logDir\daily.log`" daily" `
    -WorkingDirectory $pipeline
$logon = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$logon.Delay = "PT2M"
$triggers = @(
    (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At "18:30"),
    $logon
)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 4) -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName "tw-market-radar daily" -Action $action -Trigger $triggers -Settings $settings `
    -Description "台股市場雷達：抓證交所盤後資料、計算特徵與訊號、產生 AI 摘要（沒有金鑰就略過）" -Force | Out-Null
Write-Host "已註冊「tw-market-radar daily」。紀錄：$logDir\daily.log"
