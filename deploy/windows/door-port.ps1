<#
.SYNOPSIS
Change the TCP port the door and reader devices send to (default 9100), offline, without
reinstalling.

.DESCRIPTION
Run through C:\Management\change-door-port.bat (asks for administrator rights):

    C:\Management\change-door-port.bat 9200      (or double-click it: it asks for the port)

It sets RFID_TCP_PORT in backend.env, moves the Windows Firewall rule to the new port
(keeping who may use it), restarts the door listener (mgmt-tcp) and checks it answers there.
If the listener doesn't come up, the old port is put back. Afterwards, set the devices to
the new port.
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)][int]$Port = 0,
    [string]$Root = 'C:\Management'
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')

$Root = $Root.TrimEnd('\')
$EnvFile = "$Root\etc\backend.env"
$Logs = "$Root\logs"
if (-not (Test-Administrator)) { Stop-WithError 'Run as administrator (change-door-port.bat asks for it).' }
if (-not (Test-Path -LiteralPath $EnvFile)) { Stop-WithError "The system isn't installed in $Root." }

$old = Get-DoorPort $EnvFile
Write-Step "Door port: now $old"
if (-not $Port) {
    $answer = Read-Answer 'New port for the door and reader devices' "$old"
    if ($answer -notmatch '^\d+$') { Stop-WithError "Not a port: $answer" }
    $Port = [int]$answer
}
if ($Port -lt 1 -or $Port -gt 65535) { Stop-WithError "Not a port: $Port" }
if ($Port -eq $old) { Write-Note "Already ${Port}: nothing to change."; return }
# Also the development copies' ports (backend 8100, frontend 3000, door listener 9101): they
# are free until a developer starts those servers, then they would clash.
$taken = @(80, 443, 5432, 5433, $Ports.Garnet, $Ports.Web, $Ports.Internal, $Ports.Ws, $Ports.Frontend, 8100, 3000, 9101)
if ($taken -contains $Port) { Stop-WithError "Port $Port is used by this system itself; choose another (e.g. 9200)." }
$owner = Get-PortOwner $Port
if ($owner) { Stop-WithError "Port $Port is already used by '$owner'; choose another." }

$remote = @(Get-DoorRemoteAddress)
function Set-DoorPortTo([int]$NewPort) {
    [void](Set-EnvValue $EnvFile 'RFID_TCP_PORT' "$NewPort")
    Set-DoorFirewallRule $NewPort $remote
    Stop-ServiceSafely $ServiceIds.Tcp
    Start-ServiceChecked $ServiceIds.Tcp $Logs -Port $NewPort
}
try {
    Set-DoorPortTo $Port
} catch {
    Write-Warn "The door listener didn't come up on ${Port}: going back to $old"
    Set-DoorPortTo $old
    throw
}

$ip = (Read-EnvFile $EnvFile)['SERVER_IP']
Write-Note "RFID_TCP_PORT=$Port in $EnvFile"
Write-Note "firewall: TCP $Port allowed from $($remote -join ', ')"
Write-Note "door listener (mgmt-tcp) answers on port $Port"
Write-Host ''
Write-Host "Done. Now set every door and reader device to send to $($ip):$Port" -ForegroundColor Green
Write-Host '(until then they still send to the old port and are not heard).'
