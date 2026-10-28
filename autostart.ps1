# Start the agent when you log in to Windows (per-user scheduled task, no admin needed).
#   Install: powershell -File autostart.ps1 -Server wss://your.domain -Id office-pc [-ViewOnly]
#   Remove:  powershell -File autostart.ps1 -Remove
#   Preview: add -DryRun to see what would be created without changing anything.
# The agent token is asked here (hidden) and kept in your user environment variable RS_AGENT_TOKEN.
# The agent opens in a minimized console window so you can always see it running; close it to stop.
param([string]$Server, [string]$Id, [switch]$ViewOnly, [switch]$Remove, [switch]$DryRun)
$ErrorActionPreference = 'Stop'
$task = 'RemoteScreenAgent'

if ($Remove) {
    if (-not $DryRun) {
        Unregister-ScheduledTask -TaskName $task -Confirm:$false -ErrorAction SilentlyContinue
        [Environment]::SetEnvironmentVariable('RS_AGENT_TOKEN', $null, 'User')
    }
    "Removed task '$task' and the saved token."
    return
}

if ($Server -notmatch '^wss?://[^/\s]+$' -or $Id -notmatch '^[A-Za-z0-9_-]{1,32}$') {
    throw "Usage: autostart.ps1 -Server wss://host[:port] -Id name   (name: letters, digits, _ or -, max 32)"
}
$python = (Get-Command python).Source
$agent = Join-Path $PSScriptRoot 'agent.py'
$url = "$Server/ws/agent?id=$Id"
$flags = if ($ViewOnly) { ' --view-only' } else { '' }
$cmd = "/c start `"Remote Screen Agent`" /min `"$python`" `"$agent`" --server `"$url`" --id $Id$flags"

if ($DryRun) { "Would run at logon: cmd.exe $cmd"; "Would ask for the agent token and save it as user variable RS_AGENT_TOKEN."; return }

$sec = Read-Host 'Agent token (same as RS_AGENT_TOKEN on the server)' -AsSecureString
$token = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
if ($token.Length -lt 12) { throw 'Token must be at least 12 characters.' }
[Environment]::SetEnvironmentVariable('RS_AGENT_TOKEN', $token, 'User')

Register-ScheduledTask -TaskName $task -Force -Description 'Remote Screen agent (starts at logon)' `
    -Action (New-ScheduledTaskAction -Execute 'cmd.exe' -Argument $cmd -WorkingDirectory $PSScriptRoot) `
    -Trigger (New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME) `
    -Settings (New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)) | Out-Null
"Done. The agent will start at your next logon. To start it now: Start-ScheduledTask -TaskName $task"
