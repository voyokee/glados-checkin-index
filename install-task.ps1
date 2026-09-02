$ErrorActionPreference = "Stop"
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$py = (Get-Command py -ErrorAction Stop).Source
$taskName = "GLaDOS-Checkin"

$action = New-ScheduledTaskAction `
    -Execute $py `
    -Argument "-3 `"$dir\checkin.py`"" `
    -WorkingDirectory $dir
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

# 14:00–19:30 每 30 分钟轮询 IQI；20:00 无条件兜底
$triggerPoll = New-ScheduledTaskTrigger -Daily -At 14:00
$triggerPoll.Repetition.Interval = "PT30M"
$triggerPoll.Repetition.Duration = "PT5H30M"
$triggerPoll.Repetition.StopAtDurationEnd = $false

$triggerForce = New-ScheduledTaskTrigger -Daily -At 20:00

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger @($triggerPoll, $triggerForce) `
    -Settings $settings `
    -Principal $principal `
    -Description "GLaDOS IQI check-in (14:00-20:00, glados.rocks)" `
    -Force | Out-Null

Write-Host "Registered scheduled task $taskName."
Write-Host "  Poll: daily 14:00-19:30 every 30 min"
Write-Host "  Force: daily 20:00"
Write-Host "Run now: Start-ScheduledTask -TaskName $taskName"
Write-Host "Log file: $dir\checkin.log"
