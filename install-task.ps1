$ErrorActionPreference = "Stop"
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$py = (Get-Command py -ErrorAction Stop).Source
$taskName = "GLaDOS-Checkin"

$action = New-ScheduledTaskAction `
    -Execute $py `
    -Argument "-3 `"$dir\checkin.py`"" `
    -WorkingDirectory $dir
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

# 00:01 首次 | 00:31 第一次补签 | 01:01 第二次补签 | 01:31 最后一次补签
$times = @("00:01", "00:31", "01:01", "01:31")
$triggers = $times | ForEach-Object { New-ScheduledTaskTrigger -Daily -At $_ }

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -WakeToRun

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $triggers `
    -Settings $settings `
    -Principal $principal `
    -Description "GLaDOS 4-tier check-in 00:01-01:31" `
    -Force | Out-Null

Write-Host "Registered scheduled task $taskName with $($times.Count) daily triggers."
Write-Host "  00:01 Index<90 | 00:31 Index<95 | 01:01 Index<100 | 01:31 unconditional"
Write-Host "  Wake: enabled"
Write-Host "Run now: Start-ScheduledTask -TaskName $taskName"
Write-Host "Log file: $dir\checkin.log"
