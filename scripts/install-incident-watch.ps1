$ErrorActionPreference = 'Stop'
$incidentRoot = Split-Path -Parent $PSScriptRoot
$incidentPython = 'C:\Users\admin\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\pythonw.exe'
if (-not (Test-Path -LiteralPath $incidentPython -PathType Leaf)) { throw 'Python runtime unavailable' }
$incidentAction = New-ScheduledTaskAction -Execute $incidentPython -Argument ('-X utf8 "' + (Join-Path $PSScriptRoot 'incident-watch.py') + '" watch') -WorkingDirectory $incidentRoot
$incidentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$incidentTrigger = New-ScheduledTaskTrigger -AtLogOn -User $incidentUser
$incidentPrincipal = New-ScheduledTaskPrincipal -UserId $incidentUser -LogonType Interactive -RunLevel Limited
$incidentSettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName 'ClubOps Incident Bridge' -Action $incidentAction -Trigger $incidentTrigger -Principal $incidentPrincipal -Settings $incidentSettings -Description 'ClubOps file-event fault notifications to the existing Codex task. No periodic AI calls.' -Force | Out-Null
Start-ScheduledTask -TaskName 'ClubOps Incident Bridge'
Get-ScheduledTask -TaskName 'ClubOps Incident Bridge' | Select-Object TaskName,State
