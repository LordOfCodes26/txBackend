<#
.SYNOPSIS
Install or upgrade EVERYTHING on an offline Windows 10/11 PC from this kit folder: the
backend, the frontend behind the same web address, nightly backups, and development copies
of both projects. No internet needed.

.DESCRIPTION
Double-click install.cmd (it asks for administrator rights and runs this script), or in an
administrator PowerShell:

    powershell -ExecutionPolicy Bypass -File .\install-all.ps1
    powershell -ExecutionPolicy Bypass -File .\install-all.ps1 -ServerIp 192.168.1.10 `
        -TimeZone Asia/Pyongyang -Language ko -AdminUser admin -DevUser kim

Re-running is safe: it upgrades what is installed (safety backup first) and never
overwrites developers' work. Data, settings and accounts are kept.

.PARAMETER Root
Where everything goes (default C:\Management).
.PARAMETER ServerIp
The PC's address in the company network that browsers, tills and doors use.
.PARAMETER TimeZone
Company timezone, e.g. Asia/Pyongyang (IANA name).
.PARAMETER Language
Web/API language when the browser doesn't choose: en or ko.
.PARAMETER DeviceLanguage
Language on door and till reader screens: en or ko (ko only if they show Korean letters).
.PARAMETER Hosts
Extra host names or IPs clients use, comma separated.
.PARAMETER AdminUser
Create the first admin with this username (asks for the password).
.PARAMETER NoAdmin
Don't create an admin account.
.PARAMETER DevUser
Set up the development copies for this Windows user (default: you; "none" = no copies).
.PARAMETER SeedDemo
Fill the backend development copy with demo data.
.PARAMETER SkipBackend
Don't (re)install the backend (e.g. only the frontend changed).
.PARAMETER FrontendFrom
Build the frontend from this folder (e.g. a developer's frontend-dev) instead of the kit's.
.PARAMETER DoorNetwork
Only these addresses may use the door port, e.g. 192.168.1.0/24 (default: as before; any
on a new installation).
.PARAMETER DoorPort
The TCP port the door and reader devices send to (default: as before; 9100 on a new
installation). C:\Management\change-door-port.bat changes it without reinstalling.
.PARAMETER Yes
Don't ask questions: use the parameters and defaults.
#>
[CmdletBinding()]
param(
    [string]$Root = 'C:\Management',
    [string]$ServerIp = '',
    [string]$TimeZone = '',
    [string]$Language = '',
    [string]$DeviceLanguage = '',
    [string]$Hosts = '',
    [string]$AdminUser = '',
    [switch]$NoAdmin,
    [string]$DevUser = $env:USERNAME,
    [switch]$SeedDemo,
    [switch]$SkipBackend,
    [string]$FrontendFrom = '',
    [string]$DoorNetwork = '',
    [int]$DoorPort = 0,
    [int]$PgPort = 5432,
    [int]$HttpPort = 0,
    [int]$HttpsPort = 0,
    [switch]$Yes
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # Expand/Invoke progress bars are very slow on 5.1
. (Join-Path $PSScriptRoot 'common.ps1')

$Kit = $PSScriptRoot
$Root = $Root.TrimEnd('\')
$RootFwd = ConvertTo-ForwardSlash $Root
$Etc = "$Root\etc"
$Private = "$Root\etc\private"
$EnvFile = "$Etc\backend.env"
$PgPass = "$Private\pgpass.conf"
$Runtime = "$Root\runtime"
$ServicesDir = "$Root\services"
$Logs = "$Root\logs"
$Work = "$Root\work"
$Winsw = "$Runtime\winsw\WinSW-x64.exe"
$Python = "$Runtime\python\python.exe"
$Node = "$Runtime\node\node.exe"
$PgBin = "$Runtime\pgsql\bin"
# What every backend process needs in its environment. Only the settings module and the
# settings file's path: Django reads the rest (passwords, keys) from that file itself,
# so they never appear in the service definitions, which ordinary users can read.
# (manage.py would otherwise default to the development settings.)
$DjangoEnv = @{ DJANGO_ENV_FILE = ConvertTo-ForwardSlash $EnvFile; DJANGO_SETTINGS_MODULE = 'config.settings.prod' }

# ============================================================================ 0. checks
Write-Step "Kit in $Kit"
if (-not (Test-Administrator)) { Stop-WithError 'Run as administrator (right-click install.cmd > Run as administrator).' }
$os = Get-CimInstance Win32_OperatingSystem
if (-not [Environment]::Is64BitOperatingSystem -or [int]$os.BuildNumber -lt 17763) {
    Stop-WithError "This kit needs 64-bit Windows 10 (version 1809 or newer) or Windows 11; this is $($os.Caption) build $($os.BuildNumber)."
}
Write-Note "$($os.Caption) (build $($os.BuildNumber))"
if ($DevUser -eq 'none') { $DevUser = '' }

$versions = Read-EnvFile "$Kit\runtime\versions.txt"
if (-not $versions.Count) { Stop-WithError "No runtime\versions.txt in ${Kit}: is this the Windows kit folder?" }
$BackendBundle = Get-ChildItem -LiteralPath $Kit -Filter 'backend-*-windows.tar.gz' | Sort-Object LastWriteTime | Select-Object -Last 1
$FrontendPkg = Get-ChildItem -LiteralPath $Kit -Filter 'management-app-offline-windows*.tar.gz' | Sort-Object LastWriteTime | Select-Object -Last 1
if (-not $SkipBackend -and -not $BackendBundle) { Stop-WithError 'No backend-*-windows.tar.gz in this folder.' }
if (-not $FrontendPkg) { Stop-WithError 'No management-app-offline-windows.tar.gz (frontend) in this folder.' }

Write-Note 'checking the kit files (SHA256SUMS)...'
$sums = "$Kit\SHA256SUMS"
if (-not (Test-Path -LiteralPath $sums)) { Stop-WithError 'SHA256SUMS is missing: copy the whole kit folder.' }
foreach ($line in (Read-TextFile $sums) -split "`r?`n") {
    if (-not $line.Trim()) { continue }
    $hash, $name = $line -split '\s+\*?', 2
    $file = Join-Path $Kit ($name -replace '/', '\')
    if (-not (Test-Path -LiteralPath $file)) { Stop-WithError "Missing from the kit: $name" }
    if ((Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash -ne $hash.ToUpperInvariant()) {
        Stop-WithError "Damaged: $name (checksum mismatch). Copy the kit again."
    }
}
Write-Note 'kit files OK'
$Upgrade = Test-Path -LiteralPath $EnvFile
# The door devices' port: RFID_TCP_PORT in backend.env, kept on upgrades unless -DoorPort.
$DoorPortNow = 9100
if ($Upgrade) { $DoorPortNow = Get-DoorPort $EnvFile }
if ($DoorPort) {
    if ($DoorPort -lt 1 -or $DoorPort -gt 65535) { Stop-WithError "Not a port: $DoorPort" }
    $DoorPortNow = $DoorPort
}
# Installations before 2026-10-04 reach Garnet as "localhost": on Windows that tries IPv6
# (::1) first, where Garnet doesn't listen, and the cache check failed (503).
if ($Upgrade) {
    $oldRedis = (Read-EnvFile $EnvFile)['REDIS_URL']
    if ($oldRedis -match '^redis://localhost:(.*)$') { [void](Set-EnvValue $EnvFile 'REDIS_URL' "redis://127.0.0.1:$($Matches[1])") }
}
# The web ports: WEB_HTTP_PORT / WEB_HTTPS_PORT in backend.env, kept unless -HttpPort/-HttpsPort.
$web = @{ Http = 80; Https = 443 }
if ($Upgrade) { $web = Get-WebPorts $EnvFile }
if ($HttpPort) { $web.Http = $HttpPort }
if ($HttpsPort) { $web.Https = $HttpsPort }
$HttpPort = $web.Http
$HttpsPort = $web.Https
foreach ($p in @($HttpPort, $HttpsPort)) { if ($p -lt 1 -or $p -gt 65535) { Stop-WithError "Not a port: $p" } }
if ($HttpPort -eq $HttpsPort) { Stop-WithError 'The HTTP and HTTPS ports must differ.' }
Write-Note "install folder: $Root ($(if ($Upgrade) { 'UPGRADE: data and settings are kept' } else { 'new installation' }))"
if ($BackendBundle) { Write-Note "backend:  $($BackendBundle.Name)$(if ($SkipBackend) { ' (skipped)' })" }
Write-Note "frontend: $($FrontendPkg.Name)$(if ($FrontendFrom) { " (building from $FrontendFrom)" })"
Write-Note "developer copies for: $(if ($DevUser) { $DevUser } else { 'nobody' })"
if ((Read-Answer 'Continue? (yes/no)' 'yes' -AssumeYes:$Yes) -ne 'yes') { Stop-WithError 'Cancelled.' }
if ($SkipBackend -and -not $Upgrade) { Stop-WithError 'The backend is not installed yet: run without -SkipBackend.' }

# Ports this installation needs must be free (or already ours).
$ours = @('caddy', 'GarnetServer', 'postgres', 'python', 'node')
foreach ($p in @($HttpPort, $HttpsPort, $PgPort, $Ports.Garnet, $Ports.Web, $Ports.Internal, $Ports.Ws, $Ports.Frontend, $DoorPortNow)) {
    $owner = Get-PortOwner $p
    if ($owner -and ($ours -notcontains $owner)) {
        Stop-WithError "Port $p is used by '$owner'. Stop that program (e.g. IIS, Skype, another Redis/PostgreSQL) or see the README for other ports."
    }
}

# ============================================================================ 1. this PC
Write-Step '1. Preparing this PC'
# Long paths (node_modules) and no sleep: the server must stay reachable.
New-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem' -Name LongPathsEnabled -Value 1 -PropertyType DWord -Force | Out-Null
foreach ($setting in @('standby-timeout-ac', 'hibernate-timeout-ac')) {
    Invoke-Native "$env:SystemRoot\System32\powercfg.exe" @('/change', $setting, '0') -Quiet -AllowFailure
}
Write-Note 'long paths enabled; sleep and hibernation off while on power'

# Folders and who may use them. Settings, passwords and backups: administrators and
# services only. PostgreSQL runs as NetworkService; the other services as LocalSystem
# (see Install-WinswService in common.ps1).
$readAll = @("$($LocalService):(OI)(CI)RX", "$($NetworkService):(OI)(CI)RX", "$($Users):(OI)(CI)RX")
Set-FolderAccess $Root $readAll
foreach ($d in @($Runtime, $ServicesDir, "$Root\backend\releases", "$Root\frontend\releases", "$Root\certificate")) {
    if (-not (Test-Path -LiteralPath $d)) { New-Item -ItemType Directory -Path $d -Force | Out-Null }
}
Set-FolderAccess $Etc @()
# Earlier kits let LocalService read the settings; the services now run as LocalSystem.
Invoke-Native "$env:SystemRoot\System32\icacls.exe" @($Etc, '/remove:g', $LocalService, '/T', '/C', '/Q') -Quiet -AllowFailure
Set-FolderAccess $Private @()
Set-FolderAccess "$Root\data" @("$($LocalService):(OI)(CI)RX", "$($NetworkService):(RX)")
Set-FolderAccess "$Root\data\media" @("$($LocalService):(OI)(CI)M")
Set-FolderAccess "$Root\data\caddy" @("$($LocalService):(OI)(CI)M")
Set-FolderAccess $Logs @("$($LocalService):(OI)(CI)M", "$($Users):(OI)(CI)RX")
Set-FolderAccess "$Root\backups" @()
Set-FolderAccess $Work @()
Write-Note "folders under $Root, permissions set"

# ============================================================================ 2. runtimes
Write-Step '2. Programs (Python, Node.js, PostgreSQL, Garnet, Caddy, ...)'
$stoppedForRuntime = $false
function Install-Runtime([string]$Name, [string]$Archive, [string]$Version, [string]$SubDir) {
    $dest = "$Runtime\$Name"
    $marker = "$dest\.kit-version"
    if ((Test-Path -LiteralPath $marker) -and ((Read-TextFile $marker).Trim() -eq $Version)) {
        Write-Note "$Name $Version (already installed)"
        return
    }
    if (-not $Script:stoppedForRuntime) {
        # Programs in use can't be replaced: stop every service of this installation once.
        Get-Service -Name 'mgmt-*' -ErrorAction SilentlyContinue | ForEach-Object { Stop-ServiceSafely $_.Name }
        $Script:stoppedForRuntime = $true
    }
    $tmp = "$Runtime\.unpack-$Name"
    Remove-Tree $tmp
    Expand-ArchiveFast (Join-Path $Kit "runtime\$Archive") $tmp
    $src = $tmp
    if ($SubDir) { $src = Join-Path $tmp $SubDir }
    if (-not (Test-Path -LiteralPath $src)) { Stop-WithError "$Archive has no $SubDir folder." }
    Remove-Tree $dest
    Move-Item -LiteralPath $src -Destination $dest
    Remove-Tree $tmp
    Write-TextFile $marker "$Version`n"
    Write-Note "$Name $Version"
}
Install-Runtime 'python' $versions.PYTHON_FILE $versions.PYTHON_VERSION 'tools'
Install-Runtime 'node' $versions.NODE_FILE $versions.NODE_VERSION "node-v$($versions.NODE_VERSION)-win-x64"
Install-Runtime 'pgsql' $versions.PG_FILE $versions.PG_VERSION 'pgsql'
Install-Runtime 'dotnet' $versions.DOTNET_FILE $versions.DOTNET_VERSION ''
Install-Runtime 'garnet' $versions.GARNET_FILE $versions.GARNET_VERSION ''
Install-Runtime 'caddy' $versions.CADDY_FILE $versions.CADDY_VERSION ''
if (-not (Test-Path -LiteralPath $Winsw) -or ((Get-Item -LiteralPath $Winsw).Length -ne (Get-Item -LiteralPath "$Kit\runtime\$($versions.WINSW_FILE)").Length)) {
    New-Item -ItemType Directory -Path "$Runtime\winsw" -Force | Out-Null
    Copy-Item -LiteralPath "$Kit\runtime\$($versions.WINSW_FILE)" -Destination $Winsw -Force
}
Write-Note "WinSW $($versions.WINSW_VERSION) (runs programs as Windows services)"

# ============================================================================ 3. database
Write-Step '3. Database (PostgreSQL)'
. (Join-Path $PSScriptRoot 'postgres.ps1')
Initialize-PostgresCluster -Root $Root -DataDir "$Root\data\pgdata" -Port $PgPort `
    -ServiceId $ServiceIds.Postgres -PgPassFile $PgPass
$pgEnv = @{ PGPASSFILE = $PgPass; PGHOST = 'localhost'; PGPORT = "$PgPort"; PGUSER = 'postgres' }

# ============================================================================ 4. cache
Write-Step '4. Cache and realtime messages (Garnet, Redis-compatible)'
$garnetXml = New-WinswXml -Id $ServiceIds.Garnet -Name 'Management cache (Garnet)' `
    -Description 'Redis-compatible cache and realtime message broker for the management backend.' `
    -Executable "$Runtime\garnet\GarnetServer.exe" `
    -Arguments "--bind 127.0.0.1 --port $($Ports.Garnet) --lua true --memory 512m --index 64m" `
    -WorkingDirectory "$Runtime\garnet" -Environment @{ DOTNET_ROOT = "$Runtime\dotnet" } -LogPath $Logs
Install-WinswService $ServicesDir $Winsw $ServiceIds.Garnet $garnetXml
Start-ServiceChecked $ServiceIds.Garnet $Logs -Port $Ports.Garnet
Write-Note "Garnet on 127.0.0.1:$($Ports.Garnet)"

# ============================================================================ 5. backend
$commit = ''
if ($SkipBackend) {
    Write-Step '5. Backend: skipped (-SkipBackend)'
    [void](Set-EnvValue $EnvFile 'WEB_HTTP_PORT' "$HttpPort")
    [void](Set-EnvValue $EnvFile 'WEB_HTTPS_PORT' "$HttpsPort")
    if ($DoorPort -and (Set-EnvValue $EnvFile 'RFID_TCP_PORT' "$DoorPortNow")) {
        Stop-ServiceSafely $ServiceIds.Tcp
        Start-ServiceChecked $ServiceIds.Tcp $Logs -Port $DoorPortNow
        Write-Note "door port is now $DoorPortNow"
    }
} else {
    Write-Step '5. Backend'
    $bundleName = $BackendBundle.Name -replace '\.tar\.gz$', ''
    $version = ($bundleName -replace '^backend-', '') -replace '-windows$', ''
    Remove-Tree "$Work\$bundleName"
    Write-Note 'unpacking the backend bundle...'
    Expand-ArchiveFast $BackendBundle.FullName $Work
    $bundle = "$Work\$bundleName"
    $commit = (Read-TextFile "$bundle\COMMIT").Trim()

    # Settings file (first install) and the database user it names.
    if (-not $Upgrade) {
        $dbPassword = New-RandomHex 24
        $text = Expand-Template (Read-TextFile "$bundle\app\deploy\windows\backend.env.template") @{
            SECRET_KEY = New-RandomHex 50; DB_PASSWORD = $dbPassword; PG_PORT = $PgPort
            ALLOWED_HOSTS = 'localhost,127.0.0.1'; ROOT = $RootFwd; GARNET_PORT = $Ports.Garnet
        }
        Write-TextFile $EnvFile $text
        Write-TextFile "$Etc\backup.conf" (Expand-Template (Read-TextFile "$bundle\app\deploy\windows\backup.conf.template") @{ ROOT = $RootFwd })
        $sql = "$Private\create-db.sql"
        Write-TextFile $sql @"
DO `$`$ BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'backend') THEN
        CREATE ROLE backend LOGIN PASSWORD '$dbPassword';
    ELSE
        ALTER ROLE backend PASSWORD '$dbPassword';
    END IF;
END `$`$;
"@
        try {
            Invoke-Native "$PgBin\psql.exe" @('-v', 'ON_ERROR_STOP=1', '-q', '-d', 'postgres', '-f', $sql) -Environment $pgEnv -Quiet
        } finally { Remove-Item -LiteralPath $sql -Force }
        $exists = Invoke-Native "$PgBin\psql.exe" @('-tA', '-d', 'postgres', '-c', "SELECT 1 FROM pg_database WHERE datname='backend'") -Environment $pgEnv -PassThru
        if (($exists -join '').Trim() -ne '1') {
            Invoke-Native "$PgBin\createdb.exe" @('-O', 'backend', 'backend') -Environment $pgEnv -Quiet
        }
        Write-Note "settings: $EnvFile (new); database 'backend' created"
    } elseif (Test-Path -LiteralPath "$Root\backend\current") {
        Write-Note 'safety backup before upgrading...'
        & "$bundle\app\deploy\windows\backup.ps1" -Root $Root
        if (-not $?) { Stop-WithError 'The safety backup failed; not upgrading.' }
    }

    # ---- the release: code and Python packages. Timestamped, so re-installing the same
    # version never touches the running one.
    $release = "$Root\backend\releases\$version-$(Get-Date -Format yyyyMMddHHmmss)"
    Write-Note "release $version (commit $commit)..."
    Copy-Tree "$bundle\app" $release
    Invoke-Native $Python @('-m', 'venv', "$release\.venv") -Quiet
    $venvPython = "$release\.venv\Scripts\python.exe"
    Invoke-Native $venvPython @('-m', 'pip', 'install', '--quiet', '--disable-pip-version-check', '--no-index',
        '--find-links', "$bundle\wheelhouse", '-r', "$release\requirements\windows.txt") -Quiet
    # The services can't write here: compile now so Python doesn't try at every start.
    Invoke-Native $venvPython @('-m', 'compileall', '-q', $release) -Quiet -AllowFailure
    Write-Note 'Python packages installed'

    # ---- settings (asked on first install, kept on upgrades unless given)
    $current = Read-EnvFile $EnvFile
    if (-not $TimeZone -and -not $Upgrade) {
        $TimeZone = Read-Answer 'Company timezone (e.g. Asia/Pyongyang, Asia/Seoul, Europe/Berlin)' (Get-SuggestedTimeZone) -AssumeYes:$Yes
    }
    if ($TimeZone) {
        $tzCheck = Invoke-Native $venvPython @('-c', "import zoneinfo; zoneinfo.ZoneInfo('$TimeZone'); print('ok')") -PassThru -AllowFailure
        if (($tzCheck | Select-Object -Last 1) -ne 'ok') { Stop-WithError "Unknown timezone: $TimeZone (an IANA name like Asia/Pyongyang)" }
        [void](Set-EnvValue $EnvFile 'TIME_ZONE' $TimeZone)
    }
    if (-not $Language -and -not $Upgrade) {
        $Language = Read-Answer 'Default language for the web and API: en = English, ko = Korean' 'en' -AssumeYes:$Yes
    }
    if (-not $DeviceLanguage -and -not $Upgrade) {
        $DeviceLanguage = Read-Answer 'Language on door/till reader screens (ko only if they show Korean letters): en / ko' 'en' -AssumeYes:$Yes
    }
    foreach ($pair in @(@('LANGUAGE_CODE', $Language), @('DEVICE_LANGUAGE', $DeviceLanguage))) {
        if ($pair[1]) {
            $code = ConvertTo-LanguageCode $pair[1]
            if (-not $code) { Stop-WithError "Unknown language: $($pair[1]) (use en or ko)" }
            [void](Set-EnvValue $EnvFile $pair[0] $code)
        }
    }
    # @(): Windows PowerShell returns a one-item list as the bare item; with one IP address
    # $ips would be a string ($ips[0] its first character).
    $ips = @(Get-ServerIPv4Candidates)
    if (-not $ServerIp) {
        if ($Upgrade -and $current.Contains('SERVER_IP') -and $current['SERVER_IP']) { $ServerIp = $current['SERVER_IP'] }
        else {
            Write-Note "this PC's IP addresses: $($ips -join ', ')"
            $suggested = '127.0.0.1'
            if ($ips.Count) { $suggested = $ips[0] }
            $ServerIp = Read-Answer 'IP address that computers, tills and doors will use' $suggested -AssumeYes:$Yes
        }
    }
    if ($ServerIp -notmatch '^\d{1,3}(\.\d{1,3}){3}$') { Stop-WithError "Not an IPv4 address: $ServerIp (see: ipconfig)" }
    if ($ips -notcontains $ServerIp -and $ServerIp -ne '127.0.0.1') {
        Write-Warn "$ServerIp is not an address of this PC right now ($($ips -join ', ')). Give the PC that fixed address (see the README)."
    }
    [void](Set-EnvValue $EnvFile 'SERVER_IP' $ServerIp)
    [void](Set-EnvValue $EnvFile 'RFID_TCP_PORT' "$DoorPortNow")
    [void](Set-EnvValue $EnvFile 'WEB_HTTP_PORT' "$HttpPort")
    [void](Set-EnvValue $EnvFile 'WEB_HTTPS_PORT' "$HttpsPort")
    $hostList = @('localhost', '127.0.0.1', $ServerIp, $env:COMPUTERNAME) + $ips
    if ($Hosts) { $hostList += ($Hosts -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ }) }
    $existingHosts = @()
    if ($current.Contains('DJANGO_ALLOWED_HOSTS')) { $existingHosts = @($current['DJANGO_ALLOWED_HOSTS'] -split ',' | Where-Object { $_ }) }
    [void](Set-EnvValue $EnvFile 'DJANGO_ALLOWED_HOSTS' ((@($existingHosts + $hostList | Select-Object -Unique)) -join ','))
    $settings = Read-EnvFile $EnvFile
    foreach ($k in @('SERVER_IP', 'RFID_TCP_PORT', 'TIME_ZONE', 'LANGUAGE_CODE', 'DEVICE_LANGUAGE', 'DJANGO_ALLOWED_HOSTS')) {
        Write-Note "$k=$($settings[$k])"
    }

    # ---- database changes and static files
    $django = $DjangoEnv
    Push-Location $release
    try {
        Invoke-Native $venvPython @('manage.py', 'check', '--deploy', '--fail-level', 'ERROR') -Environment $django -Quiet
        Invoke-Native $venvPython @('manage.py', 'migrate', '--noinput') -Environment $django -Quiet
        Invoke-Native $venvPython @('manage.py', 'collectstatic', '--noinput') -Environment $django -Quiet
    } finally { Pop-Location }
    Write-Note 'database up to date, static files collected'

    # ---- switch to it and (re)start the backend services
    foreach ($svc in $AppServices) { Stop-ServiceSafely $svc }
    Set-Junction "$Root\backend\current" $release
    $cur = "$Root\backend\current"
    $pyEnv = $DjangoEnv + @{ PYTHONUNBUFFERED = '1'; PYTHONDONTWRITEBYTECODE = '1' }
    $deps = @($ServiceIds.Postgres, $ServiceIds.Garnet)
    # waitress listens on 127.0.0.1 only (Caddy and the frontend server reach it) and passes
    # X-Forwarded-For/-Proto on untouched: Django decides (TRUST_X_FORWARDED_FOR,
    # SECURE_PROXY_SSL_HEADER), as with gunicorn behind nginx on Linux. By default waitress
    # would delete them: every request would look like plain HTTP from 127.0.0.1.
    $defs = @(
        @{ Id = $ServiceIds.Web; Name = 'Management API (waitress)'; Description = 'Backend web API on 127.0.0.1:8000.'
            Arguments = "-m waitress --listen=127.0.0.1:$($Ports.Web) --threads=16 --channel-timeout=60 --no-clear-untrusted-proxy-headers config.wsgi:application" },
        @{ Id = $ServiceIds.Ws; Name = 'Management realtime (uvicorn)'; Description = 'WebSockets (/ws/) on 127.0.0.1:8002.'
            Arguments = "-m uvicorn config.asgi:application --host 127.0.0.1 --port $($Ports.Ws) --proxy-headers --no-access-log" },
        @{ Id = $ServiceIds.Tcp; Name = 'Management door listener (TCP)'; Description = 'Raw TCP listener for door and reader devices (RFID_TCP_PORT).'
            Arguments = 'manage.py run_rfid_tcp' }
    )
    foreach ($d in $defs) {
        $xml = New-WinswXml -Id $d.Id -Name $d.Name -Description $d.Description -Executable "$cur\.venv\Scripts\python.exe" `
            -Arguments $d.Arguments -WorkingDirectory $cur -Environment $pyEnv -DependsOn $deps -LogPath $Logs
        Install-WinswService $ServicesDir $Winsw $d.Id $xml
    }
    Start-ServiceChecked $ServiceIds.Web $Logs -Port $Ports.Web
    Start-ServiceChecked $ServiceIds.Ws $Logs -Port $Ports.Ws
    Start-ServiceChecked $ServiceIds.Tcp $Logs -Port $DoorPortNow
    Write-Note "backend $version running (services: $($AppServices -join ', '))"

    # Keep the last 3 releases.
    $active = Get-JunctionTarget $cur
    Get-ChildItem -LiteralPath "$Root\backend\releases" -Directory | Sort-Object LastWriteTime -Descending |
        Select-Object -Skip 3 | Where-Object { $_.FullName -ne $active } | ForEach-Object { Remove-Tree $_.FullName }
}

# manage.cmd: Django's management commands with the live settings, from an administrator
# command prompt, e.g.  C:\Management\manage.cmd changepassword admin@example.com
Write-TextFile "$Root\manage.cmd" (@(
        '@echo off',
        'rem Django management commands for the installed backend (run as administrator).',
        "set DJANGO_ENV_FILE=$($DjangoEnv.DJANGO_ENV_FILE)",
        "set DJANGO_SETTINGS_MODULE=$($DjangoEnv.DJANGO_SETTINGS_MODULE)",
        "cd /d `"$Root\backend\current`"",
        '.venv\Scripts\python.exe manage.py %*'
    ) -join "`r`n")

# Tools used by the .bat files live in $Root\deploy (NOT under backend\current): deploy
# retargets that junction, and running deploy-dev.ps1 from under it left services stopped
# when the restart step failed.
$DeployTools = "$Root\deploy"
New-Item -ItemType Directory -Path $DeployTools -Force | Out-Null
foreach ($name in @('common.ps1', 'deploy-dev.ps1', 'backup.ps1', 'door-port.ps1', 'web-port.ps1')) {
    Copy-Item -LiteralPath "$Root\backend\current\deploy\windows\$name" -Destination "$DeployTools\$name" -Force
}
$deployScript = "$DeployTools\deploy-dev.ps1"
foreach ($deploy in @(
        @('deploy-backend.bat', '-Backend', 'backend-dev'),
        @('deploy-frontend.bat', '-Frontend', 'frontend-dev'),
        @('deploy-all.bat', '-Backend -Frontend', 'backend-dev and frontend-dev'))) {
    Write-TextFile "$Root\$($deploy[0])" ((@(
                '@echo off',
                "rem Put the COMMITTED changes of your $($deploy[2]) live (asks for administrator rights).",
                'net session >nul 2>&1',
                'if errorlevel 1 (',
                "    powershell -NoProfile -Command `"Start-Process -FilePath '%~f0' -Verb RunAs`"",
                '    exit /b',
                ')',
                "powershell -NoProfile -ExecutionPolicy Bypass -File `"$deployScript`" -Root `"$Root`" $($deploy[1]) %*",
                'echo.',
                'pause'
            ) -join "`r`n") + "`r`n")
}

# change-door-port.bat: move the door devices' TCP port without reinstalling (door-port.ps1).
Write-TextFile "$Root\change-door-port.bat" ((@(
            '@echo off',
            'rem Change the TCP port the door and reader devices send to, e.g.  change-door-port.bat 9200',
            'rem (double-click: it asks for the port). Asks for administrator rights.',
            'setlocal',
            'net session >nul 2>&1',
            'if errorlevel 1 (',
            '    if "%~1"=="" (',
            "        powershell -NoProfile -Command `"Start-Process -FilePath '%~f0' -Verb RunAs`"",
            '    ) else (',
            "        powershell -NoProfile -Command `"Start-Process -FilePath '%~f0' -ArgumentList '%*' -Verb RunAs`"",
            '    )',
            '    exit /b',
            ')',
            "powershell -NoProfile -ExecutionPolicy Bypass -File `"$DeployTools\door-port.ps1`" -Root `"$Root`" %*",
            'echo.',
            'pause'
        ) -join "`r`n") + "`r`n")

# change-web-port.bat: move the web ports without reinstalling (web-port.ps1).
Write-TextFile "$Root\change-web-port.bat" ((@(
            '@echo off',
            'rem Change the web ports (HTTP, HTTPS), e.g.  change-web-port.bat 8080 8443',
            'rem (double-click: it asks for them). Asks for administrator rights.',
            'setlocal',
            'net session >nul 2>&1',
            'if errorlevel 1 (',
            '    if "%~1"=="" (',
            "        powershell -NoProfile -Command `"Start-Process -FilePath '%~f0' -Verb RunAs`"",
            '    ) else (',
            "        powershell -NoProfile -Command `"Start-Process -FilePath '%~f0' -ArgumentList '%*' -Verb RunAs`"",
            '    )',
            '    exit /b',
            ')',
            "powershell -NoProfile -ExecutionPolicy Bypass -File `"$DeployTools\web-port.ps1`" -Root `"$Root`" %*",
            'echo.',
            'pause'
        ) -join "`r`n") + "`r`n")

# uninstall.bat in the install folder too, for when the kit is gone.
$uninstallBat = "$Root\backend\current\deploy\windows\uninstall.bat"
if (Test-Path -LiteralPath $uninstallBat) { Copy-Item -LiteralPath $uninstallBat -Destination "$Root\uninstall.bat" -Force }

# ============================================================================ 6. frontend
Write-Step '6. Frontend (build and service)'
$feWork = "$Work\frontend"
Remove-Tree $feWork
Write-Note 'unpacking the frontend package (tens of thousands of files; a few minutes)...'
Expand-ArchiveFast $FrontendPkg.FullName $feWork
$feSrc = "$feWork\management-app"
if ($FrontendFrom) { $feSrc = (Resolve-Path -LiteralPath $FrontendFrom).Path }
if (-not (Test-Path -LiteralPath "$feSrc\package.json") -or -not (Test-Path -LiteralPath "$feSrc\node_modules")) {
    Stop-WithError "$feSrc is not a frontend folder with node_modules."
}
function Get-GitCommit([string]$Dir) {
    # The short commit of a git folder, read from .git without needing git.
    $headFile = "$Dir\.git\HEAD"
    if (-not (Test-Path -LiteralPath $headFile)) { return '' }
    $head = (Read-TextFile $headFile).Trim()
    if ($head -notlike 'ref:*') { return $head.Substring(0, 7) }
    $ref = $head.Substring(5).Trim()
    $refFile = "$Dir\.git\$($ref -replace '/', '\')"
    if (Test-Path -LiteralPath $refFile) { return (Read-TextFile $refFile).Trim().Substring(0, 7) }
    $packed = "$Dir\.git\packed-refs"
    if (Test-Path -LiteralPath $packed) {
        $line = (Read-TextFile $packed) -split "`r?`n" | Where-Object { $_ -like "* $ref" } | Select-Object -First 1
        if ($line) { return $line.Substring(0, 7) }
    }
    return ''
}
$feCommit = Get-GitCommit $feSrc
if (-not $feCommit) { $feCommit = 'build' }
$feVersion = "$feCommit-$(Get-Date -Format yyyyMMddHHmmss)"
$build = "$Work\frontend-build"
Remove-Tree $build
Copy-Tree $feSrc $build -ExcludeDirs @("$feSrc\.next", "$feSrc\dist")  # full paths: a bare name would skip every dist/ in node_modules
Remove-Item -LiteralPath "$build\.env.local" -Force -ErrorAction SilentlyContinue
Write-Note 'building (next build, offline; a few minutes)...'
Push-Location $build
try {
    Invoke-Native $Node @("$build\node_modules\next\dist\bin\next", 'build') -Environment @{ NEXT_TELEMETRY_DISABLED = '1'; NODE_ENV = 'production' } -Quiet
} finally { Pop-Location }
if (-not (Test-Path -LiteralPath "$build\.next\standalone\server.js")) { Stop-WithError 'No standalone server after the build (next.config needs output: "standalone").' }
$feRelease = "$Root\frontend\releases\$feVersion"
Copy-Tree "$build\.next\standalone" $feRelease
Copy-Tree "$build\.next\static" "$feRelease\.next\static"
if (Test-Path -LiteralPath "$build\public") { Copy-Tree "$build\public" "$feRelease\public" }
Stop-ServiceSafely $ServiceIds.Frontend
Set-Junction "$Root\frontend\current" $feRelease
$feXml = New-WinswXml -Id $ServiceIds.Frontend -Name 'Management frontend (Next.js)' `
    -Description 'The web app on 127.0.0.1:3100, published by Caddy.' -Executable $Node -Arguments 'server.js' `
    -WorkingDirectory "$Root\frontend\current" -LogPath $Logs -Environment @{
        NODE_ENV = 'production'; NEXT_TELEMETRY_DISABLED = '1'; HOSTNAME = '127.0.0.1'; PORT = "$($Ports.Frontend)"
        API_URL = "http://127.0.0.1:$($Ports.Internal)"
    }
Install-WinswService $ServicesDir $Winsw $ServiceIds.Frontend $feXml
Start-ServiceChecked $ServiceIds.Frontend $Logs -Port $Ports.Frontend
Remove-Tree $build
$activeFe = Get-JunctionTarget "$Root\frontend\current"
Get-ChildItem -LiteralPath "$Root\frontend\releases" -Directory | Sort-Object LastWriteTime -Descending |
    Select-Object -Skip 3 | Where-Object { $_.FullName -ne $activeFe } | ForEach-Object { Remove-Tree $_.FullName }
Write-Note "frontend $feVersion running on 127.0.0.1:$($Ports.Frontend)"

# ============================================================================ 7. web server
Write-Step '7. Web server (Caddy: HTTPS, one address for everything)'
$settings = Read-EnvFile $EnvFile
$serverIpNow = $settings['SERVER_IP']
$tls = Write-Caddyfile $Root $HttpPort $HttpsPort
if ($tls -eq 'tls internal') {
    Write-Note "certificate: Caddy's own (install $Root\certificate\management-root-ca.crt on the other PCs)"
} else {
    Write-Note "certificate: $Etc\tls\cert.pem (the company's)"
}
Write-Note "web ports: HTTP $HttpPort, HTTPS $HttpsPort"
$caddyXml = New-WinswXml -Id $ServiceIds.Caddy -Name 'Management web server (Caddy)' `
    -Description 'HTTPS for the frontend and the backend (ports: WEB_HTTP_PORT/WEB_HTTPS_PORT in backend.env).' `
    -Executable "$Runtime\caddy\caddy.exe" -Arguments "run --config `"$Etc\Caddyfile`" --adapter caddyfile" `
    -WorkingDirectory "$Runtime\caddy" -LogPath $Logs -DependsOn @($ServiceIds.Web, $ServiceIds.Frontend) `
    -Environment @{ XDG_DATA_HOME = "$Root\data\caddy\data"; XDG_CONFIG_HOME = "$Root\data\caddy\config" }
Install-WinswService $ServicesDir $Winsw $ServiceIds.Caddy $caddyXml
Start-ServiceChecked $ServiceIds.Caddy $Logs -Port $HttpsPort
# Caddy's own certificate authority: its root certificate is what the other PCs install.
$rootCa = "$Root\data\caddy\data\caddy\pki\authorities\local\root.crt"
if ($tls -eq 'tls internal') {
    for ($i = 0; $i -lt 30 -and -not (Test-Path -LiteralPath $rootCa); $i++) { Start-Sleep -Seconds 1 }
    if (Test-Path -LiteralPath $rootCa) {
        Copy-Item -LiteralPath $rootCa -Destination "$Root\certificate\management-root-ca.crt" -Force
        Import-Certificate -FilePath $rootCa -CertStoreLocation Cert:\LocalMachine\Root | Out-Null
        Write-Note "this PC trusts the certificate; for other PCs: $Root\certificate\management-root-ca.crt"
    } else { Write-Warn "Caddy hasn't made its certificate yet ($rootCa)." }
}

# ============================================================================ 8. firewall
Write-Step '8. Windows Firewall'
Set-WebFirewallRule $HttpPort $HttpsPort
# Who may use the door port: -DoorNetwork, else as before (a re-run used to reset it to any).
$doorRemote = @(Get-DoorRemoteAddress)
if ($DoorNetwork) { $doorRemote = @($DoorNetwork -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ }) }
Set-DoorFirewallRule $DoorPortNow $doorRemote
Write-Note "allowed: TCP $HttpPort, $HttpsPort from anywhere; TCP $DoorPortNow from $($doorRemote -join ', ')"

# ============================================================================ 9. backups
Write-Step '9. Nightly backups'
$backupScript = "$Root\backend\current\deploy\windows\backup.ps1"
$action = New-ScheduledTaskAction -Execute "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$backupScript`" -Root `"$Root`""
$trigger = New-ScheduledTaskTrigger -Daily -At '02:30'
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$taskSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 3) -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries
Register-ScheduledTask -TaskName $BackupTaskName -Action $action -Trigger $trigger -Principal $principal -Settings $taskSettings -Force | Out-Null
Write-Note "scheduled task '$BackupTaskName': every night at 02:30"
if (-not (Get-ChildItem -LiteralPath "$Root\backups" -Recurse -Filter '*.dump' -ErrorAction SilentlyContinue)) {
    Write-Note 'first backup now (and its restore check)...'
    & $backupScript -Root $Root
}
$backupConf = Read-EnvFile "$Etc\backup.conf"
if (-not $backupConf['OFFSITE_DIR']) { Write-Warn "set OFFSITE_DIR in $Etc\backup.conf: backups are on this disk only." }

# ============================================================================ 10. admin
$cur = "$Root\backend\current"
$django = $DjangoEnv
Push-Location $cur
try {
    $hasAdmin = Invoke-Native "$cur\.venv\Scripts\python.exe" @('manage.py', 'shell', '-c',
        'from apps.accounts.models import User; print(int(User.objects.filter(is_superuser=True, is_active=True).exists()))') -Environment $django -PassThru
    if (($hasAdmin | Select-Object -Last 1) -ne '1' -and -not $NoAdmin) {
        Write-Step '10. First admin account'
        if (-not $AdminUser) { $AdminUser = Read-Answer 'Admin username' 'admin' -AssumeYes:$Yes }
        if ($env:DJANGO_SUPERUSER_PASSWORD) {
            Invoke-Native "$cur\.venv\Scripts\python.exe" @('manage.py', 'createsuperuser', '--noinput', '--username', $AdminUser) -Environment $django
        } elseif ($Yes) {
            Write-Note '-Yes without DJANGO_SUPERUSER_PASSWORD: create the admin later (see the README).'
        } else {
            Write-Note 'Choose a strong password (at least 8 characters, not only digits):'
            foreach ($k in $django.Keys) { [Environment]::SetEnvironmentVariable($k, $django[$k], 'Process') }
            & "$cur\.venv\Scripts\python.exe" manage.py createsuperuser --username $AdminUser
            foreach ($k in $django.Keys) { [Environment]::SetEnvironmentVariable($k, $null, 'Process') }
        }
    }
} finally { Pop-Location }

# ============================================================================ 11. development
if ($DevUser) {
    Write-Step "11. Development copies for $DevUser"
    $devArgs = @{ Root = $Root; KitDir = $Kit; DevUser = $DevUser; FrontendSource = "$feWork\management-app"; Yes = $true }
    if ($SeedDemo) { $devArgs.SeedDemo = $true }
    if ($FrontendFrom) { $devArgs.SkipFrontend = $true }
    if ($SkipBackend -and -not $BackendBundle) { $devArgs.SkipBackend = $true }
    & (Join-Path $PSScriptRoot 'setup-dev.ps1') @devArgs
} else {
    Write-Step '11. Development copies: none (-DevUser none)'
}
Remove-Tree $feWork
Get-ChildItem -LiteralPath $Work -Directory -Filter 'backend-*' -ErrorAction SilentlyContinue | ForEach-Object { Remove-Tree $_.FullName }

# ============================================================================ 12. checks
Write-Step '12. Checks'
$failed = $false
foreach ($svc in @($ServiceIds.Postgres, $ServiceIds.Garnet) + $AppServices + @($ServiceIds.Frontend, $ServiceIds.Caddy)) {
    $state = (Get-Service -Name $svc -ErrorAction SilentlyContinue).Status
    Write-Note ('{0,-28} {1}' -f $svc, $state)
    if ("$state" -ne 'Running') { $failed = $true }
}
$base = Get-LocalHttpsUrl $HttpsPort
[void](Wait-HttpOk "$base/" 60)
foreach ($check in @(
        @("$base/", 'frontend'), @("$base/health/", 'backend'), @("$base/health/db/", 'database'),
        @("$base/health/redis/", 'cache'), @("$base/health/backup/", 'backups'), @("$base/admin/login/", 'admin'),
        @("http://127.0.0.1:$($Ports.Internal)/health/", 'internal address'))) {
    $code = Get-HttpCode $check[0]
    Write-Note ('{0,-28} {1}  {2}' -f $check[1], $code, $check[0])
    if ($code -notmatch '^(200|30[1278])$') { $failed = $true }
}
$door = Test-NetConnection -ComputerName 127.0.0.1 -Port $DoorPortNow -InformationLevel Quiet -WarningAction SilentlyContinue
Write-Note ('{0,-28} {1}' -f "door listener :$DoorPortNow", $(if ($door) { 'open' } else { 'CLOSED' }))
if (-not $door) { $failed = $true }
if ($failed) { Stop-WithError "Something isn't answering (see above). Logs: $Logs" }

$url = "https://$serverIpNow"
if ($HttpsPort -ne 443) { $url = "$($url):$HttpsPort" }
Write-Host ''
Write-Host 'Everything is installed.' -ForegroundColor Green
Write-Host @"

  Open in a browser:   $url/            (the frontend)
  Backend admin:       $url/admin/
  API documentation:   $url/api/docs/   (sign in at /admin/ first)
  Door devices:        TCP $($serverIpNow):$DoorPortNow
  Till readers:        $url/api/v1/rfid/events/

  Certificate for the other PCs:  $Root\certificate\management-root-ca.crt  (see the README)
  Settings:  $EnvFile      Backups: $Root\backups   Logs: $Logs
  Services:  $(($ServiceIds.Values | Where-Object { $_ -ne $ServiceIds.PostgresDev } | Sort-Object) -join ' ')
"@
if ($DevUser) {
    Write-Host @"

  Development ($DevUser):
    backend-dev    cd %USERPROFILE%\backend-dev  &&  .venv\Scripts\uvicorn config.asgi:application --reload --port 8100
    frontend-dev   cd %USERPROFILE%\frontend-dev &&  npm run dev        (http://127.0.0.1:3000)
"@
}
