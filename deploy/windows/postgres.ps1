# PostgreSQL instances for install-all.ps1 (the live database) and setup-dev.ps1 (the
# development one). Dot-sourced after common.ps1.

function Initialize-PostgresCluster {
    # Create (first time) and start a PostgreSQL instance run as the Windows service
    # ServiceId, listening on localhost:Port only. The superuser "postgres" gets a random
    # password, kept in PgPassFile (administrators only) for the scripts.
    param(
        [Parameter(Mandatory)][string]$Root,
        [Parameter(Mandatory)][string]$DataDir,
        [Parameter(Mandatory)][int]$Port,
        [Parameter(Mandatory)][string]$ServiceId,
        [Parameter(Mandatory)][string]$PgPassFile
    )
    $pgBin = "$Root\runtime\pgsql\bin"
    $runtimeMajor = ((Read-TextFile "$Root\runtime\pgsql\.kit-version").Trim() -split '\.')[0]

    if (-not (Test-Path -LiteralPath "$DataDir\PG_VERSION")) {
        # initdb run by an administrator drops its admin rights (restricted token), so the
        # folder must also allow this user's own account; the service runs as NetworkService.
        $me = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
        Set-FolderAccess $DataDir @("*$($me):(OI)(CI)F", "$($NetworkService):(OI)(CI)F")
        $password = New-RandomHex 24
        $pwFile = Join-Path $env:TEMP ("pg-" + [guid]::NewGuid().ToString('N'))
        Write-TextFile $pwFile $password
        try {
            Invoke-Native "$pgBin\initdb.exe" @('-D', $DataDir, '-U', 'postgres', "--pwfile=$pwFile",
                '-A', 'scram-sha-256', '-E', 'UTF8', '--no-locale') -Quiet
        } finally { Remove-Item -LiteralPath $pwFile -Force -ErrorAction SilentlyContinue }
        $conf = Read-TextFile "$DataDir\postgresql.conf"
        $conf += @"

# --- Written by the Windows kit (deploy/windows/postgres.ps1)
listen_addresses = 'localhost'
port = $Port
logging_collector = on
log_directory = 'log'
log_filename = 'postgresql-%a.log'
log_truncate_on_rotation = on
log_rotation_age = 1d
"@
        Write-TextFile "$DataDir\postgresql.conf" $conf
        Set-PgPassEntry $PgPassFile $Port $password
        Write-Note "new PostgreSQL $runtimeMajor instance in $DataDir (port $Port, localhost only)"
    } else {
        $dataMajor = (Read-TextFile "$DataDir\PG_VERSION").Trim()
        if ($dataMajor -ne $runtimeMajor) {
            Stop-WithError "The database in $DataDir is PostgreSQL $dataMajor but the kit has ${runtimeMajor}: a major upgrade (pg_upgrade) is needed first."
        }
    }

    if (-not (Get-Service -Name $ServiceId -ErrorAction SilentlyContinue)) {
        Invoke-Native "$pgBin\pg_ctl.exe" @('register', '-N', $ServiceId, '-U', 'NT AUTHORITY\NetworkService',
            '-D', $DataDir, '-S', 'auto', '-w') -Quiet
    }
    if ((Get-Service -Name $ServiceId).Status -ne 'Running') { Start-Service -Name $ServiceId }
    $ready = 1
    for ($i = 0; $i -lt 60 -and $ready -ne 0; $i++) {
        $ready = Get-NativeExitCode "$pgBin\pg_isready.exe" @('-h', 'localhost', '-p', "$Port", '-q')
        if ($ready -ne 0) { Start-Sleep -Seconds 1 }
    }
    if ($ready -ne 0) { Stop-WithError "PostgreSQL ($ServiceId) did not start; see $DataDir\log." }
    Write-Note "PostgreSQL running as the service $ServiceId (port $Port)"
}

function Set-PgPassEntry([string]$PgPassFile, [int]$Port, [string]$Password) {
    # pgpass.conf lines: host:port:database:user:password. Replaces this port's entries.
    $lines = @()
    if (Test-Path -LiteralPath $PgPassFile) {
        $lines = @((Read-TextFile $PgPassFile) -split "`r?`n" | Where-Object { $_ -and $_ -notmatch "^(localhost|127\.0\.0\.1):$($Port):" })
    }
    $lines += "localhost:$($Port):*:postgres:$Password"
    $lines += "127.0.0.1:$($Port):*:postgres:$Password"
    Write-TextFile $PgPassFile (($lines -join "`r`n") + "`r`n")
}
