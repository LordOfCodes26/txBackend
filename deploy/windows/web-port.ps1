<#
.SYNOPSIS
Change the web ports (HTTP 80 / HTTPS 443) offline, without reinstalling.

.DESCRIPTION
Run through C:\Management\change-web-port.bat (asks for administrator rights):

    C:\Management\change-web-port.bat 8080 8443     (HTTP port, HTTPS port)

or double-click it: it asks for both. It saves WEB_HTTP_PORT / WEB_HTTPS_PORT in
backend.env (upgrades keep them), rewrites the web server's configuration, moves the
firewall rule, restarts the web server (mgmt-caddy) and checks the site answers on the new
port. If it doesn't, the old ports are put back.
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)][int]$HttpPort = 0,
    [Parameter(Position = 1)][int]$HttpsPort = 0,
    [string]$Root = 'C:\Management'
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')

$Root = $Root.TrimEnd('\')
$EnvFile = "$Root\etc\backend.env"
$Logs = "$Root\logs"
if (-not (Test-Administrator)) { Stop-WithError 'Run as administrator (change-web-port.bat asks for it).' }
if (-not (Test-Path -LiteralPath $EnvFile)) { Stop-WithError "The system isn't installed in $Root." }

$old = Get-WebPorts $EnvFile
Write-Step "Web ports: now HTTP $($old.Http), HTTPS $($old.Https)"
if (-not $HttpPort -and -not $HttpsPort) {
    $a = Read-Answer 'New HTTPS port (what browsers and till programs use)' "$($old.Https)"
    $b = Read-Answer 'New HTTP port (only redirects to HTTPS)' "$($old.Http)"
    if ($a -notmatch '^\d+$' -or $b -notmatch '^\d+$') { Stop-WithError 'Ports are numbers, e.g. 8443 and 8080.' }
    $HttpsPort = [int]$a
    $HttpPort = [int]$b
} elseif (-not ($HttpPort -and $HttpsPort)) {
    Stop-WithError 'Give both ports, HTTP first: change-web-port.bat 8080 8443'
}
foreach ($p in @($HttpPort, $HttpsPort)) { if ($p -lt 1 -or $p -gt 65535) { Stop-WithError "Not a port: $p" } }
if ($HttpPort -eq $HttpsPort) { Stop-WithError 'The HTTP and HTTPS ports must differ.' }
if ($HttpPort -eq $old.Http -and $HttpsPort -eq $old.Https) { Write-Note 'Already these ports: nothing to change.'; return }
$taken = @(5432, 5433, (Get-DoorPort $EnvFile), $Ports.Garnet, $Ports.Web, $Ports.Internal, $Ports.Ws, $Ports.Frontend)
foreach ($p in @($HttpPort, $HttpsPort)) {
    if ($taken -contains $p) { Stop-WithError "Port $p is used by this system itself; choose another." }
    $owner = Get-PortOwner $p
    if ($owner -and $owner -ne 'caddy') { Stop-WithError "Port $p is already used by '$owner'; choose another." }
}

$oldCaddyfile = Read-TextFile "$Root\etc\Caddyfile"
function Set-WebPortsTo([int]$Http, [int]$Https) {
    [void](Set-EnvValue $EnvFile 'WEB_HTTP_PORT' "$Http")
    [void](Set-EnvValue $EnvFile 'WEB_HTTPS_PORT' "$Https")
    [void](Write-Caddyfile $Root $Http $Https)
    Set-WebFirewallRule $Http $Https
    Stop-ServiceSafely $ServiceIds.Caddy
    Start-ServiceChecked $ServiceIds.Caddy $Logs -Port $Https
    $code = Wait-HttpOk "$(Get-LocalHttpsUrl $Https)/health/" 30
    if ($code -notmatch '^(200|30[1278])$') { throw "The site doesn't answer on port $Https (HTTP $code)." }
}
try {
    Set-WebPortsTo $HttpPort $HttpsPort
} catch {
    Write-Warn "The new ports didn't work ($_): going back to $($old.Http)/$($old.Https)"
    [void](Set-EnvValue $EnvFile 'WEB_HTTP_PORT' "$($old.Http)")
    [void](Set-EnvValue $EnvFile 'WEB_HTTPS_PORT' "$($old.Https)")
    Write-TextFile "$Root\etc\Caddyfile" $oldCaddyfile
    Set-WebFirewallRule $old.Http $old.Https
    Stop-ServiceSafely $ServiceIds.Caddy
    Start-Service -Name $ServiceIds.Caddy -ErrorAction SilentlyContinue
    throw
}

$ip = (Read-EnvFile $EnvFile)['SERVER_IP']
$address = "https://$ip"
if ($HttpsPort -ne 443) { $address = "$($address):$HttpsPort" }
Write-Note "WEB_HTTP_PORT=$HttpPort, WEB_HTTPS_PORT=$HttpsPort in $EnvFile"
Write-Note "firewall: TCP $HttpPort, $HttpsPort allowed"
Write-Host ''
Write-Host "Done. The address is now $address/" -ForegroundColor Green
Write-Host 'Tell the users, and change it in every till program (they still use the old one).'
