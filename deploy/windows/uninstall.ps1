<#
.SYNOPSIS
Remove the management system's services, firewall rules and backup task from this PC.

.DESCRIPTION
Keeps the data (database, uploaded files), settings and backups in C:\Management unless
-RemoveData is given. Developers' copies (backend-dev, frontend-dev) are never touched.
In an administrator PowerShell:

    powershell -ExecutionPolicy Bypass -File uninstall.ps1
    powershell -ExecutionPolicy Bypass -File uninstall.ps1 -RemoveData -Yes   # everything
#>
[CmdletBinding()]
param(
    [string]$Root = 'C:\Management',
    [switch]$RemoveData,
    [switch]$Yes
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
if (-not (Test-Administrator)) { throw 'Run as administrator.' }
$Root = $Root.TrimEnd('\')

$what = 'services, firewall rules and the backup task (data, settings and backups are KEPT)'
if ($RemoveData) { $what = "EVERYTHING in $Root, including the database and all backups" }
Write-Step "Removing $what"
if (-not $Yes -and (Read-Answer 'Type yes to continue' 'no') -ne 'yes') { throw 'Cancelled.' }

foreach ($svc in Get-Service -Name 'mgmt-*' -ErrorAction SilentlyContinue) {
    Stop-ServiceSafely $svc.Name
    $winsw = "$Root\services\$($svc.Name)\$($svc.Name).exe"
    if (Test-Path -LiteralPath $winsw) {
        Invoke-Native $winsw @('uninstall') -Quiet -AllowFailure
    } else {
        # PostgreSQL's services are registered by pg_ctl, not WinSW.
        Invoke-Native "$env:SystemRoot\System32\sc.exe" @('delete', $svc.Name) -Quiet -AllowFailure
    }
    Write-Note "service $($svc.Name) removed"
}
Get-NetFirewallRule -Group $FirewallGroup -ErrorAction SilentlyContinue | Remove-NetFirewallRule
Write-Note 'firewall rules removed'
Unregister-ScheduledTask -TaskName $BackupTaskName -Confirm:$false -ErrorAction SilentlyContinue
Write-Note 'backup task removed'
Get-ChildItem Cert:\LocalMachine\Root | Where-Object { $_.Subject -like '*Caddy Local Authority*' } | Remove-Item -ErrorAction SilentlyContinue
Remove-MachinePath @("$Root\runtime\node", "$Root\runtime\python", "$Root\runtime\python\Scripts")

if ($RemoveData) {
    Remove-Tree $Root
    Write-Note "$Root removed"
} else {
    Write-Note "kept: $Root (data, settings, backups). Re-run install.cmd to install again."
}
