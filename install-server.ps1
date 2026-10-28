# Install the Remote Screen server on this computer (run from the folder that holds RemoteScreenServer.exe).
#   Install: powershell -ExecutionPolicy Bypass -File install-server.ps1
#   Remove:  powershell -ExecutionPolicy Bypass -File install-server.ps1 -Remove
#   Preview: add -DryRun to print what would change without changing anything.
# Does: copy the exe to C:\RemoteScreen, ask you for the viewer user name, viewer password and agent token (passwords hidden),
# save them as your user environment variables, start the server at every logon (minimized window), and stop the PC
# from sleeping on mains power.
# The server listens on 127.0.0.1 only; put TLS in front (Cloudflare Tunnel or Caddy) to reach it from outside.
param([string]$Dir = 'C:\RemoteScreen', [switch]$Remove, [switch]$DryRun)
$ErrorActionPreference = 'Stop'
$task = 'RemoteScreenServer'

if ($Remove) {
    if (-not $DryRun) {
        Unregister-ScheduledTask -TaskName $task -Confirm:$false -ErrorAction SilentlyContinue
        Get-Process RemoteScreenServer -ErrorAction SilentlyContinue | Stop-Process -Force
        'RS_VIEWER_USER', 'RS_VIEWER_PASSWORD', 'RS_AGENT_TOKEN' | ForEach-Object { [Environment]::SetEnvironmentVariable($_, $null, 'User') }
    }
    "$(if ($DryRun) { 'Would remove' } else { 'Removed' }) task '$task', stop the server and delete the saved secrets. Files in $Dir stay in place."
    return
}

$src = Join-Path $PSScriptRoot 'RemoteScreenServer.exe'
if (-not (Test-Path $src)) { throw "RemoteScreenServer.exe not found next to this script ($PSScriptRoot)" }
$exe = Join-Path $Dir 'RemoteScreenServer.exe'
$cmd = "/c start `"Remote Screen Server`" /min `"$exe`" --port 8765"

if ($DryRun) {
    "Would copy $src to $exe"
    'Would ask for the viewer user name, viewer password (hidden) and agent token (hidden) and save them as user variables.'
    "Would register task '$task' at logon: cmd.exe $cmd"
    'Would set the AC sleep timeout to never (powercfg /change standby-timeout-ac 0) and start the server now.'
    return
}

function Ask($label) {
    $s = Read-Host $label -AsSecureString
    [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($s))
}
$user = Read-Host 'Viewer user name (what you type in the browser: letters, digits, _ or -)'
$pw = Ask 'Viewer password (what you type in the browser, 12+ characters)'
$tok = Ask 'Agent token (what the agents use, 12+ characters, different from the password)'
if ($user -notmatch '^[A-Za-z0-9_-]{1,32}$') { throw 'User name: letters, digits, _ or - only (max 32). Nothing was changed.' }
if ($pw.Length -lt 12 -or $tok.Length -lt 12 -or $pw -eq $tok) { throw 'Password and token need 12+ characters and must differ. Nothing was changed.' }

New-Item -ItemType Directory -Force $Dir | Out-Null
Copy-Item $src $exe -Force
[Environment]::SetEnvironmentVariable('RS_VIEWER_USER', $user, 'User')
[Environment]::SetEnvironmentVariable('RS_VIEWER_PASSWORD', $pw, 'User')
[Environment]::SetEnvironmentVariable('RS_AGENT_TOKEN', $tok, 'User')
Register-ScheduledTask -TaskName $task -Force -Description 'Remote Screen server (starts at logon)' `
    -Action (New-ScheduledTaskAction -Execute 'cmd.exe' -Argument $cmd -WorkingDirectory $Dir) `
    -Trigger (New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME) `
    -Settings (New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)) | Out-Null
powercfg /change standby-timeout-ac 0

# start now (the saved variables only reach new logons, so pass them to this one process)
$env:RS_VIEWER_USER = $user; $env:RS_VIEWER_PASSWORD = $pw; $env:RS_AGENT_TOKEN = $tok
Start-Process $exe -ArgumentList '--port', '8765' -WorkingDirectory $Dir -WindowStyle Minimized
Start-Sleep 5
if (Get-Process RemoteScreenServer -ErrorAction SilentlyContinue) { 'Server is running on 127.0.0.1:8765 and will start at every logon.' } else { throw 'The server did not start. Run RemoteScreenServer.exe by hand to see the error.' }
