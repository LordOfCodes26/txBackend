<#
.SYNOPSIS
Restore the live database from a nightly dump (Windows).

.DESCRIPTION
Checks the dump restores cleanly first, stops the backend services, keeps the CURRENT
database under a new name (never deleted by this script), restores the dump as the live
database and starts the services again. In an administrator PowerShell:

    powershell -ExecutionPolicy Bypass -File restore.ps1 -Dump C:\Management\backups\db\backend-20261003-023000.dump -Yes
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Dump,
    [string]$Root = 'C:\Management',
    [switch]$Yes
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')

if (-not $Yes) { throw 'Refusing without -Yes (this replaces the live database).' }
if (-not (Test-Administrator)) { throw 'Run as administrator.' }
$Root = $Root.TrimEnd('\')
$envValues = Read-EnvFile "$Root\etc\backend.env"
if ($envValues['DATABASE_URL'] -notmatch '^postgres(ql)?://([^:]+):[^@]*@[^:/]+:(\d+)/(.+)$') { throw 'Cannot read DATABASE_URL' }
$dbOwner = $Matches[2]; $pgPort = $Matches[3]; $dbName = $Matches[4]
$pgBin = "$Root\runtime\pgsql\bin"
$pgEnv = @{ PGPASSFILE = "$Root\etc\private\pgpass.conf"; PGHOST = 'localhost'; PGPORT = $pgPort; PGUSER = 'postgres' }

Write-Step 'Checking the dump restores cleanly before touching the live database'
& (Join-Path $PSScriptRoot 'backup.ps1') -Root $Root -VerifyOnly $Dump

$kept = "$($dbName)_before_restore_$((Get-Date).ToUniversalTime().ToString('yyyyMMdd_HHmmss'))"
Write-Step "Stopping: $($AppServices -join ', ')"
foreach ($svc in $AppServices) { Stop-ServiceSafely $svc }
try {
    Write-Step "Keeping the current database as $kept"
    Invoke-Native "$pgBin\psql.exe" @('-d', 'postgres', '-v', 'ON_ERROR_STOP=1', '-c',
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '$dbName' AND pid <> pg_backend_pid();") -Environment $pgEnv -Quiet
    Invoke-Native "$pgBin\psql.exe" @('-d', 'postgres', '-v', 'ON_ERROR_STOP=1', '-c', "ALTER DATABASE $dbName RENAME TO $kept;") -Environment $pgEnv -Quiet
    Write-Step "Restoring $Dump"
    Invoke-Native "$pgBin\createdb.exe" @("--owner=$dbOwner", $dbName) -Environment $pgEnv -Quiet
    Invoke-Native "$pgBin\pg_restore.exe" @('--no-owner', "--role=$dbOwner", '--exit-on-error', "--dbname=$dbName", $Dump) -Environment $pgEnv -Quiet
} finally {
    Write-Step "Starting: $($AppServices -join ', ')"
    foreach ($svc in $AppServices) { Start-Service -Name $svc -ErrorAction SilentlyContinue }
}
Write-Host ''
Write-Host "Restored. The previous database is kept as '$kept'; drop it once you're satisfied:"
Write-Host "  $pgBin\dropdb.exe -h localhost -p $pgPort -U postgres $kept   (password: $Root\etc\private\pgpass.conf)"
