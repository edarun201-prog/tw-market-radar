# 註冊 Windows 工作排程器：平日 09:35 執行 `python -m radar morning`（可重複執行，會覆蓋同名工作）。
#
#   powershell -ExecutionPolicy Bypass -File scripts\windows\register-morning-task.ps1
#
# 內容：開盤快照（只存進資料庫給系統判斷，不對外顯示）→ 新聞 → 匯出 HTML 檔 → 發布公開網頁（.env 有 PAGES_REPO 才發布）。
# 公開網頁仍然是前一天的盤後資料，只多了最新的新聞，沒有盤中股價（即時行情對外提供要先取得證交所授權）。
# 用 pythonw.exe 執行，不會跳出視窗；紀錄在 data\logs\morning.log。休市日程式自己會略過開盤快照。
# 移除：Unregister-ScheduledTask -TaskName "tw-market-radar morning" -Confirm:$false

$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$pipeline = Join-Path $root "services\pipeline"
$python = Join-Path $pipeline ".venv\Scripts\pythonw.exe"
$logDir = Join-Path $root "data\logs"
if (-not (Test-Path $python)) { throw "找不到 $python，請先照 README 建立 .venv" }
New-Item -ItemType Directory -Force $logDir | Out-Null

$action = New-ScheduledTaskAction -Execute $python -Argument "-m radar --log-file `"$logDir\morning.log`" morning" `
    -WorkingDirectory $pipeline
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At "09:35"
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName "tw-market-radar morning" -Action $action -Trigger $trigger -Settings $settings `
    -Description "台股市場雷達：開盤快照（內部用）、新聞、更新公開網頁" -Force | Out-Null
Write-Host "已註冊「tw-market-radar morning」。紀錄：$logDir\morning.log"
