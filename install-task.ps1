$ErrorActionPreference = "Stop"
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$py = (Get-Command py -ErrorAction Stop).Source
$taskName = "GLaDOS-Checkin"

$action = New-ScheduledTaskAction `
    -Execute $py `
    -Argument "-3 `"$dir\checkin.py`"" `
    -WorkingDirectory $dir
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

# 四档：16:00 <80 | 17:00 <85 | 18:00 <90 | 19:00 anyscore
$times = @("16:00", "17:00", "18:00", "19:00")
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
    -Description "GLaDOS 4-tier IQI check-in" `
    -Force | Out-Null

Write-Host "Registered scheduled task $taskName with $($times.Count) daily triggers."
Write-Host "  16:00 Index<80 | 17:00 Index<85 | 18:00 Index<90 | 19:00 anyscore"
Write-Host "  Wake: enabled"
Write-Host "Run now: Start-ScheduledTask -TaskName $taskName"
Write-Host "Log file: $dir\checkin.log"
