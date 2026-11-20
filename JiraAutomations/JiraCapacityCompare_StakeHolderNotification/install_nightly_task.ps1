$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$runTime = if ($args[0]) { $args[0] } else { "11:30" }

function Register-WeekdaySync {
    param(
        [string]$TaskName,
        [string]$ScriptName,
        [string]$Description
    )
    $script = Join-Path $root $ScriptName
    $action = New-ScheduledTaskAction `
        -Execute "powershell.exe" `
        -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$script`"" `
        -WorkingDirectory $root
    $trigger = New-ScheduledTaskTrigger `
        -Weekly `
        -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday `
        -At $runTime
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Description $Description `
        -Force | Out-Null
    Write-Output "Scheduled '$TaskName' Monday-Friday at $runTime"
    Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo | Select-Object TaskName, NextRunTime
}

Register-WeekdaySync `
    -TaskName "GSIS IN Verification Confluence Sync" `
    -ScriptName "run_in_verification_nightly.ps1" `
    -Description "Weekdays 11:30: refresh Confluence page 1261831310 from Jira IN Verification tickets"

Register-WeekdaySync `
    -TaskName "GSIS Blocked Confluence Sync" `
    -ScriptName "run_blocked_nightly.ps1" `
    -Description "Weekdays 11:30: refresh Confluence page 1303283178 from Jira Blocker tickets"
