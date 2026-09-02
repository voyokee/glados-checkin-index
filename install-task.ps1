$ErrorActionPreference = "Stop"
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$py = (Get-Command py -ErrorAction Stop).Source
$taskName = "GLaDOS-Checkin"

$action = New-ScheduledTaskAction `
    -Execute $py `
    -Argument "-3 `"$dir\checkin.py`"" `
    -WorkingDirectory $dir
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

$trigger = New-ScheduledTaskTrigger -Daily -At 09:17
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "GLaDOS daily check-in (glados.rocks)" `
    -Force | Out-Null

Write-Host "Registered scheduled task $taskName (daily 09:17)."
Write-Host "Run now: Start-ScheduledTask -TaskName $taskName"
Write-Host "Log file: $dir\checkin.log"
