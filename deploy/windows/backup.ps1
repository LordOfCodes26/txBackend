<#
.SYNOPSIS
Nightly backup on Windows: database dump + media archive, verified by restoring it into a
scratch database, old ones pruned, copied to OFFSITE_DIR. Run every night at 02:30 by the
scheduled task "Management nightly backup" (as SYSTEM), and by the installer before an
upgrade. Status for GET /health/backup/ goes to BACKUP_STATUS_FILE.

.EXAMPLE
powershell -ExecutionPolicy Bypass -File backup.ps1                  # a backup now
powershell -ExecutionPolicy Bypass -File backup.ps1 -VerifyOnly C:\Management\backups\db\backend-20261003-023000.dump
#>
[CmdletBinding()]
param(
    [string]$Root = 'C:\Management',
    [string]$VerifyOnly = ''
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
. (Join-Path $PSScriptRoot 'common.ps1')

$Root = $Root.TrimEnd('\')
$conf = Read-EnvFile "$Root\etc\backup.conf"
$envValues = Read-EnvFile "$Root\etc\backend.env"
if (-not $conf.Contains('BACKUP_DIR')) { throw "Missing $Root\etc\backup.conf" }
$backupDir = $conf['BACKUP_DIR'] -replace '/', '\'
$keepDays = 14
if ($conf.Contains('KEEP_DAILY_DAYS') -and $conf['KEEP_DAILY_DAYS']) { $keepDays = [int]$conf['KEEP_DAILY_DAYS'] }
$statusFile = $envValues['BACKUP_STATUS_FILE'] -replace '/', '\'
$mediaRoot = $envValues['MEDIA_ROOT'] -replace '/', '\'
# postgres://user:password@host:port/name
if ($envValues['DATABASE_URL'] -notmatch '^postgres(ql)?://([^:]+):[^@]*@[^:/]+:(\d+)/(.+)$') { throw 'Cannot read DATABASE_URL' }
$pgPort = $Matches[3]; $dbName = $Matches[4]
$pgBin = "$Root\runtime\pgsql\bin"
$pgEnv = @{ PGPASSFILE = "$Root\etc\private\pgpass.conf"; PGHOST = 'localhost'; PGPORT = $pgPort; PGUSER = 'postgres' }

function Get-NowIso { return (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ') }

function Update-Status([hashtable]$Values) {
    # Merge into the JSON status file (all values are strings, as /health/backup/ expects).
    $data = [ordered]@{}
    if (Test-Path -LiteralPath $statusFile) {
        try {
            $old = (Read-TextFile $statusFile) | ConvertFrom-Json
            foreach ($p in $old.PSObject.Properties) { $data[$p.Name] = [string]$p.Value }
        } catch { $data = [ordered]@{} }
    }
    foreach ($k in $Values.Keys) { $data[$k] = [string]$Values[$k] }
    $tmp = "$statusFile.tmp"
    Write-TextFile $tmp ((New-Object PSObject -Property $data) | ConvertTo-Json)
    Move-Item -LiteralPath $tmp -Destination $statusFile -Force
}

function Test-Dump([string]$Dump) {
    # Restore into a scratch database and check it's complete and consistent: restore
    # succeeds, migrations are there, ledgers and stock reconcile. Drops the scratch DB.
    if ((Test-Sha256File $Dump) -ne $true) { throw "Checksum mismatch (or no .sha256) for $Dump" }
    $scratch = "$($dbName)_verify"
    Invoke-Native "$pgBin\dropdb.exe" @('--if-exists', $scratch) -Environment $pgEnv -Quiet
    Invoke-Native "$pgBin\createdb.exe" @($scratch) -Environment $pgEnv -Quiet
    try {
        Invoke-Native "$pgBin\pg_restore.exe" @('--no-owner', '--exit-on-error', "--dbname=$scratch", $Dump) -Environment $pgEnv -Quiet
        $sql = Join-Path $env:TEMP ("verify-" + [guid]::NewGuid().ToString('N') + '.sql')
        Write-TextFile $sql @'
SELECT json_build_object(
  'developers',          (SELECT count(*) FROM developers_developer),
  'transactions',        (SELECT count(*) FROM finance_accounttransaction),
  'attendance_records',  (SELECT count(*) FROM attendance_attendancerecord),
  'migrations',          (SELECT count(*) FROM django_migrations),
  'developer_ledger_mismatches', (
     SELECT count(*) FROM finance_developeraccount a
     WHERE a.balance <> COALESCE((SELECT sum(amount) FROM finance_accounttransaction t
                                  WHERE t.account_id = a.id), 0)),
  'seller_ledger_mismatches', (
     SELECT count(*) FROM seller_finance_selleraccount a
     WHERE a.balance <> COALESCE((SELECT sum(amount) FROM seller_finance_sellertransaction t
                                  WHERE t.account_id = a.id), 0)),
  'stock_mismatches', (
     SELECT count(*) FROM goods_good g
     WHERE g.track_stock AND g.quantity <> COALESCE((SELECT sum(quantity_delta)
            FROM goods_inventorymovement m WHERE m.good_id = g.id), 0))
);
'@
        try {
            $out = Invoke-Native "$pgBin\psql.exe" @('-d', $scratch, '-tA', '-v', 'ON_ERROR_STOP=1', '-f', $sql) -Environment $pgEnv -PassThru
        } finally { Remove-Item -LiteralPath $sql -Force -ErrorAction SilentlyContinue }
        $json = ($out | Where-Object { $_ -like '{*' } | Select-Object -Last 1)
        Write-Host "verify: $json"
        $r = $json | ConvertFrom-Json
        $problems = @()
        foreach ($k in @('developer_ledger_mismatches', 'seller_ledger_mismatches', 'stock_mismatches')) {
            if ([int]$r.$k -ne 0) { $problems += $k }
        }
        if ([int]$r.migrations -eq 0) { $problems += 'no migrations table rows' }
        if ($problems.Count) { throw "verify failed: $($problems -join ', ')" }
        return $json
    } finally {
        Invoke-Native "$pgBin\dropdb.exe" @('--if-exists', $scratch) -Environment $pgEnv -Quiet -AllowFailure
    }
}

if ($VerifyOnly) {
    [void](Test-Dump $VerifyOnly)
    Write-Host "OK: $VerifyOnly restores cleanly"
    return
}

$log = "$Root\logs\backup.log"
Start-Transcript -Path $log -Append | Out-Null
try {
    foreach ($d in @("$backupDir\db", "$backupDir\media")) { New-Item -ItemType Directory -Path $d -Force | Out-Null }
    $stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMdd-HHmmss')

    Write-Host "==> Dumping database $dbName"
    $dump = "$backupDir\db\$dbName-$stamp.dump"
    Invoke-Native "$pgBin\pg_dump.exe" @('--format=custom', '--compress=6', "--file=$dump.partial", $dbName) -Environment $pgEnv -Quiet
    Move-Item -LiteralPath "$dump.partial" -Destination $dump
    Write-Sha256File $dump
    Update-Status @{ last_dump_at = Get-NowIso; last_dump_file = (Split-Path -Leaf $dump); last_dump_bytes = (Get-Item -LiteralPath $dump).Length }

    if ($mediaRoot -and (Test-Path -LiteralPath $mediaRoot) -and (Get-ChildItem -LiteralPath $mediaRoot -Force | Select-Object -First 1)) {
        Write-Host '==> Archiving media'
        Invoke-Native (Get-TarExe) @('-czf', "$backupDir\media\media-$stamp.tar.gz", '-C', $mediaRoot, '.') -Quiet
    }

    Write-Host '==> Verifying the dump by restoring it'
    try {
        $counts = Test-Dump $dump
        Update-Status @{ last_verify_at = Get-NowIso; last_verify_ok = 'true'; last_verify_file = (Split-Path -Leaf $dump); last_verify_counts = $counts }
    } catch {
        Update-Status @{ last_verify_at = Get-NowIso; last_verify_ok = 'false'; last_verify_file = (Split-Path -Leaf $dump) }
        throw "VERIFY FAILED for $($dump): $_"
    }

    Write-Host "==> Pruning backups older than $keepDays days"
    $limit = (Get-Date).AddDays(-$keepDays)
    Get-ChildItem -LiteralPath "$backupDir\db", "$backupDir\media" -File |
        Where-Object { $_.LastWriteTime -lt $limit } | Remove-Item -Force

    Write-Host '==> Copying off-site'
    $offsite = ''
    if ($conf.Contains('OFFSITE_DIR')) { $offsite = $conf['OFFSITE_DIR'] -replace '/', '\' }
    if ($offsite) {
        Copy-Tree $backupDir $offsite -Mirror
        Update-Status @{ offsite_copy_at = Get-NowIso; offsite_target = $offsite }
    } else {
        Write-Warning 'no OFFSITE_DIR set: backups exist on this disk only.'
        Update-Status @{ offsite_target = '' }
    }
    Write-Host "Done: $dump"
} finally {
    Stop-Transcript | Out-Null
}
