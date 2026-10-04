<#
.SYNOPSIS
Set up (or update) DEVELOPMENT copies of the backend and the frontend for a Windows user,
from the kit. No internet needed. Separate from the installed system: own folders, own
database (port 5433), own settings.

.DESCRIPTION
install-all.ps1 runs this for -DevUser. To run it on its own, in an administrator
PowerShell in the kit folder (the system must be installed first):

    powershell -ExecutionPolicy Bypass -File .\setup-dev.ps1 -DevUser kim -SeedDemo

Running it again with a newer kit updates the copies: new packages are installed and the
new code is fetched as the git branch offline/main. Your own work is never changed: merge
it yourself with  git merge offline/main.
#>
[CmdletBinding()]
param(
    [string]$Root = 'C:\Management',
    [string]$KitDir = $PSScriptRoot,
    [string]$DevUser = $env:USERNAME,
    [string]$FrontendSource = '',
    [switch]$SeedDemo,
    [switch]$RunTests,
    [switch]$SkipBackend,
    [switch]$SkipFrontend,
    [switch]$Yes
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
. (Join-Path $PSScriptRoot 'common.ps1')
. (Join-Path $PSScriptRoot 'postgres.ps1')

$Root = $Root.TrimEnd('\')
$Runtime = "$Root\runtime"
$Work = "$Root\work"
$DevPgPort = 5433
# The development backend's web port (8000 is the installed backend's) and door port (9100 is
# the installed door listener's; RFID_TCP_PORT in .env.example).
$DevWebPort = 8100
$DbName = 'backend_dev'
$DbUser = 'backend_dev'
$PgPass = "$Root\etc\private\pgpass.conf"
$PgBin = "$Runtime\pgsql\bin"
$Npm = "$Runtime\node\npm.cmd"
if (-not (Test-Administrator)) { Stop-WithError 'Run as administrator.' }
if (-not (Test-Path -LiteralPath "$Root\etc\backend.env")) { Stop-WithError "The system isn't installed in $Root yet: run install-all.ps1 first." }

# ------------------------------------------------------------------------------ who, where
try {
    $sid = (New-Object Security.Principal.NTAccount($DevUser)).Translate([Security.Principal.SecurityIdentifier]).Value
} catch { Stop-WithError "No such Windows user: $DevUser" }
$userProfile = Get-CimInstance Win32_UserProfile | Where-Object { $_.SID -eq $sid } | Select-Object -First 1
$profileDir = $null
if ($userProfile) { $profileDir = $userProfile.LocalPath }
if (-not $profileDir) { Stop-WithError "$DevUser has no user profile yet: sign in to Windows as $DevUser once, then run this again." }
$BackendDev = "$profileDir\backend-dev"
$FrontendDev = "$profileDir\frontend-dev"
Write-Step "Development copies for $DevUser in $profileDir"
if ((Read-Answer 'Continue? (yes/no)' 'yes' -AssumeYes:$Yes) -ne 'yes') { Stop-WithError 'Cancelled.' }

function Grant-Owner([string]$Dir) {
    # Files made here by the (administrator) installer belong to the developer.
    $icacls = "$env:SystemRoot\System32\icacls.exe"
    Invoke-Native $icacls @($Dir, '/setowner', "*$sid", '/T', '/C', '/Q') -Quiet -AllowFailure
    Invoke-Native $icacls @($Dir, '/grant', "*$($sid):(OI)(CI)F", '/T', '/C', '/Q') -Quiet -AllowFailure
}

# ------------------------------------------------------------------------------ tools
Write-Step 'Tools: Git, Node.js and Python on the PATH'
$versions = Read-EnvFile "$KitDir\runtime\versions.txt"
$gitExe = "$env:ProgramFiles\Git\cmd\git.exe"
$haveGit = Test-Path -LiteralPath $gitExe
$gitWanted = $versions['GIT_VERSION']
if ($haveGit) {
    $gitNow = ((Invoke-Native $gitExe @('--version') -PassThru) -join '') -replace '^git version ', ''
    if ($gitWanted -and -not $gitNow.StartsWith($gitWanted)) { $haveGit = $false }
}
if (-not $haveGit) {
    Write-Note "installing Git for Windows $gitWanted..."
    Invoke-Native "$KitDir\runtime\$($versions['GIT_FILE'])" @('/VERYSILENT', '/NORESTART', '/NOCANCEL', '/SP-', '/SUPPRESSMSGBOXES', '/o:PathOption=Cmd') -Quiet
}
if (-not (Test-Path -LiteralPath $gitExe)) { Stop-WithError "Git didn't install ($gitExe missing)." }
Add-MachinePath @("$Runtime\node", "$Runtime\python", "$Runtime\python\Scripts", "$env:ProgramFiles\Git\cmd")
function Invoke-Git([string]$Dir, [string[]]$GitArgs, [switch]$PassThru) {
    # safe.directory: the copies belong to the developer, the installer runs as administrator.
    $all = @('-c', 'safe.directory=*', '-C', $Dir) + $GitArgs
    if ($PassThru) { return Invoke-Native $gitExe $all -PassThru }
    Invoke-Native $gitExe $all -Quiet
}
Write-Note "Git $((Invoke-Native $gitExe @('--version') -PassThru) -join ''); node, npm, python, git on the PATH (new terminals)"

# ------------------------------------------------------------------------------ backend copy
if (-not $SkipBackend) {
    Write-Step "Backend copy ($BackendDev)"
    $bundleFile = Get-ChildItem -LiteralPath $KitDir -Filter 'backend-*-windows.tar.gz' | Sort-Object LastWriteTime | Select-Object -Last 1
    if (-not $bundleFile) { Stop-WithError "No backend-*-windows.tar.gz in $KitDir." }
    $bundleName = $bundleFile.Name -replace '\.tar\.gz$', ''
    $bundle = "$Work\dev-$bundleName"
    Remove-Tree $bundle
    New-Item -ItemType Directory -Path $bundle -Force | Out-Null
    Write-Note 'unpacking...'
    Expand-ArchiveFast $bundleFile.FullName $bundle
    $bundle = "$bundle\$bundleName"
    if (-not (Test-Path -LiteralPath "$bundle\backend.git-bundle")) { Stop-WithError 'The bundle has no git history (backend.git-bundle).' }

    $newRepo = $false
    if (-not (Test-Path -LiteralPath $BackendDev)) {
        $heads = Invoke-Native $gitExe @('bundle', 'list-heads', "$bundle\backend.git-bundle") -PassThru
        $branch = 'main'
        if (-not ($heads | Where-Object { $_ -match ' refs/heads/main$' })) {
            $branch = (($heads | Select-Object -First 1) -split ' refs/heads/')[1]
        }
        Invoke-Native $gitExe @('-c', 'safe.directory=*', 'clone', '-q', '-b', $branch, '-o', 'offline', "$bundle\backend.git-bundle", $BackendDev) -Quiet
        $newRepo = $true
        Write-Note "cloned: branch $branch, $((Invoke-Git $BackendDev @('rev-list', '--count', 'HEAD') -PassThru) -join '') commits of history"
    } elseif (Test-Path -LiteralPath "$BackendDev\.git") {
        Write-Note 'existing copy: your work is kept'
    } else {
        Stop-WithError "$BackendDev exists but is not a git repository: move it away first."
    }
    # Keep the bundle's packages and history in the copy, for later updates.
    $cache = "$BackendDev\.offline-cache"
    Copy-Tree "$bundle\wheelhouse" "$cache\wheelhouse" -Mirror
    Copy-Item -LiteralPath "$bundle\backend.git-bundle" -Destination "$cache\backend.git-bundle" -Force
    Invoke-Native $gitExe @('-c', 'safe.directory=*', '-C', $BackendDev, 'remote', 'set-url', 'offline', "$cache\backend.git-bundle") -Quiet -AllowFailure
    if ($LASTEXITCODE -ne 0) { Invoke-Git $BackendDev @('remote', 'add', 'offline', "$cache\backend.git-bundle") }
    Invoke-Git $BackendDev @('fetch', '-q', 'offline')
    if (-not $newRepo) {
        $behind = (Invoke-Git $BackendDev @('rev-list', '--count', 'HEAD..offline/main') -PassThru) -join ''
        Write-Note "the kit's code is the branch offline/main ($behind new commits): merge it with  git merge offline/main"
    }
    $exclude = "$BackendDev\.git\info\exclude"
    if (-not ((Test-Path -LiteralPath $exclude) -and ((Read-TextFile $exclude) -match '(?m)^\.offline-cache/$'))) {
        $text = ''
        if (Test-Path -LiteralPath $exclude) { $text = Read-TextFile $exclude }
        Write-TextFile $exclude ($text.TrimEnd() + "`n.offline-cache/`n")
    }

    Write-Note 'Python environment with the development tools...'
    $venvPython = "$BackendDev\.venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $venvPython)) { Invoke-Native "$Runtime\python\python.exe" @('-m', 'venv', "$BackendDev\.venv") -Quiet }
    Invoke-Native $venvPython @('-m', 'pip', 'install', '--quiet', '--disable-pip-version-check', '--no-index', '--find-links', "$cache\wheelhouse",
        '-r', "$BackendDev\requirements\windows.txt", '-r', "$BackendDev\requirements\dev.txt") -Quiet
    Write-Note "$((Invoke-Native $venvPython @('--version') -PassThru) -join ''), pytest, ruff, Django: installed"

    # Own PostgreSQL instance (port 5433, no backups): demo data and test runs stay out of the
    # live database and its backups.
    Write-Note "development database ($DbName on port $DevPgPort)..."
    Initialize-PostgresCluster -Root $Root -DataDir "$Root\data\pgdata-dev" -Port $DevPgPort -ServiceId 'mgmt-postgres-dev' -PgPassFile $PgPass
    $pgEnv = @{ PGPASSFILE = $PgPass; PGHOST = 'localhost'; PGPORT = "$DevPgPort"; PGUSER = 'postgres' }
    $envFile = "$BackendDev\.env"
    $roleExists = ((Invoke-Native "$PgBin\psql.exe" @('-d', 'postgres', '-tAc', "SELECT 1 FROM pg_roles WHERE rolname='$DbUser'") -Environment $pgEnv -PassThru) -join '').Trim() -eq '1'
    $dbPassword = ''
    if (-not ($roleExists -and (Test-Path -LiteralPath $envFile))) {
        $dbPassword = New-RandomHex 16
        $verb = 'CREATE'
        if ($roleExists) { $verb = 'ALTER' }
        # CREATEDB: the tests make their own throw-away test database.
        Invoke-Native "$PgBin\psql.exe" @('-d', 'postgres', '-qc', "$verb ROLE $DbUser WITH LOGIN CREATEDB PASSWORD '$dbPassword'") -Environment $pgEnv -Quiet
    }
    $dbExists = ((Invoke-Native "$PgBin\psql.exe" @('-d', 'postgres', '-tAc', "SELECT 1 FROM pg_database WHERE datname='$DbName'") -Environment $pgEnv -PassThru) -join '').Trim() -eq '1'
    if (-not $dbExists) { Invoke-Native "$PgBin\createdb.exe" @('-O', $DbUser, $DbName) -Environment $pgEnv -Quiet }

    if (-not (Test-Path -LiteralPath $envFile)) {
        $live = Read-EnvFile "$Root\etc\backend.env"
        $values = @{
            DJANGO_SECRET_KEY = New-RandomHex 50
            DJANGO_ALLOWED_HOSTS = "localhost,127.0.0.1,$($env:COMPUTERNAME)"
            DATABASE_URL = "postgres://$($DbUser):$dbPassword@localhost:$DevPgPort/$DbName"
            REDIS_URL = "redis://127.0.0.1:$($Ports.Garnet)/1"
            TIME_ZONE = $live['TIME_ZONE']
            LANGUAGE_CODE = $live['LANGUAGE_CODE']
            MEDIA_ROOT = ConvertTo-ForwardSlash "$BackendDev\media"
        }
        $lines = foreach ($line in (Read-TextFile "$BackendDev\.env.example") -split "`r?`n") {
            $key = ($line -split '=', 2)[0]
            if ($line -notmatch '^\s*#' -and $values.ContainsKey($key) -and $values[$key]) { "$key=$($values[$key])" } else { $line }
        }
        Write-TextFile $envFile (($lines -join "`r`n").TrimEnd() + "`r`n")
        Write-Note "settings: $envFile (DEBUG on, its own database, Garnet database 1)"
    } elseif ((Read-EnvFile $envFile)['REDIS_URL'] -match '^redis://localhost:(.*)$') {
        # Copies set up before 2026-10-04: localhost tries IPv6 first, where Garnet doesn't listen.
        [void](Set-EnvValue $envFile 'REDIS_URL' "redis://127.0.0.1:$($Matches[1])")
    }
    if ($dbPassword -and (Test-Path -LiteralPath $envFile) -and ((Read-EnvFile $envFile)['DATABASE_URL'] -notmatch [regex]::Escape(":$dbPassword@"))) {
        # The database user was made again (e.g. a new development database): new password.
        [void](Set-EnvValue $envFile 'DATABASE_URL' "postgres://$($DbUser):$dbPassword@localhost:$DevPgPort/$DbName")
    }
    New-Item -ItemType Directory -Path "$BackendDev\media" -Force | Out-Null
    Push-Location $BackendDev
    try {
        Invoke-Native $venvPython @('manage.py', 'migrate', '--noinput') -Quiet
        Write-Note 'database ready: tables and roles created'
        if ($SeedDemo) {
            Write-Note 'demo data...'
            foreach ($cmd in @('seed_demo', 'seed_more', 'seed_attendance_month', 'seed_purchases_month')) {
                Invoke-Native $venvPython @('manage.py', $cmd) -Quiet
            }
        }
        if ($RunTests) {
            Write-Note 'tests (a few minutes)...'
            Invoke-Native $venvPython @('-m', 'pytest', '-q', '-p', 'no:cacheprovider')
        }
    } finally { Pop-Location }
    Grant-Owner $BackendDev
    Remove-Tree (Split-Path -Parent $bundle)
}

# ------------------------------------------------------------------------------ frontend copy
if (-not $SkipFrontend) {
    Write-Step "Frontend copy ($FrontendDev)"
    $unpacked = ''
    if (-not $FrontendSource) {
        $pkg = Get-ChildItem -LiteralPath $KitDir -Filter 'management-app-offline-windows*.tar.gz' | Sort-Object LastWriteTime | Select-Object -Last 1
        if (-not $pkg) { Stop-WithError "No management-app-offline-windows.tar.gz in $KitDir." }
        $unpacked = "$Work\dev-frontend"
        Remove-Tree $unpacked
        Write-Note 'unpacking the frontend package (a few minutes)...'
        Expand-ArchiveFast $pkg.FullName $unpacked
        $FrontendSource = "$unpacked\management-app"
    }
    $newCopy = $false
    if (-not (Test-Path -LiteralPath $FrontendDev)) {
        Write-Note 'copying source, git history and node_modules...'
        Copy-Tree $FrontendSource $FrontendDev
        $newCopy = $true
    } elseif (-not (Test-Path -LiteralPath "$FrontendDev\.git")) {
        Write-Warn "$FrontendDev exists but is not a git repository: left alone"
    } else {
        Write-Note "existing copy: your work is kept (not overwritten)"
    }
    # The development frontend talks to the development backend. Port 8100, not 8000: the
    # installed backend (mgmt-web) has 8000. Copies set up before 2026-10-04 were pointed at
    # 8000, i.e. at the LIVE backend: that file is replaced too, a hand-edited one is kept.
    $envLocal = "$FrontendDev\.env.local"
    $oldEnvLocal = $false
    if (Test-Path -LiteralPath $envLocal) {
        $text = Read-TextFile $envLocal
        $oldEnvLocal = ($text -match '(?m)^# Written by setup-dev\.ps1') -and ($text -match '(?m)^API_URL=http://127\.0\.0\.1:8000\s*$')
    }
    if ((Test-Path -LiteralPath $FrontendDev) -and ($newCopy -or $oldEnvLocal)) {
        Write-TextFile $envLocal "# Written by setup-dev.ps1: the development backend (backend-dev, port $DevWebPort).`r`nAPI_URL=http://127.0.0.1:$DevWebPort`r`n"
        if ($oldEnvLocal) { Write-Note "frontend copy: .env.local now points at the development backend (port $DevWebPort), not the live one" }
    }
    if (Test-Path -LiteralPath "$FrontendDev\.git") {
        # The kit's frontend history becomes the branch offline/main; merge it when ready.
        $feCache = "$FrontendDev\.offline-cache"
        Remove-Tree "$feCache\frontend.git"
        New-Item -ItemType Directory -Path $feCache -Force | Out-Null
        Invoke-Native $gitExe @('-c', 'safe.directory=*', 'clone', '-q', '--bare', '--no-hardlinks', $FrontendSource, "$feCache\frontend.git") -Quiet
        Invoke-Native $gitExe @('-c', 'safe.directory=*', '-C', $FrontendDev, 'remote', 'set-url', 'offline', "$feCache\frontend.git") -Quiet -AllowFailure
        if ($LASTEXITCODE -ne 0) { Invoke-Git $FrontendDev @('remote', 'add', 'offline', "$feCache\frontend.git") }
        Invoke-Git $FrontendDev @('fetch', '-q', 'offline')
        $exclude = "$FrontendDev\.git\info\exclude"
        $text = ''
        if (Test-Path -LiteralPath $exclude) { $text = Read-TextFile $exclude }
        if ($text -notmatch '(?m)^\.offline-cache/$') { Write-TextFile $exclude ($text.TrimEnd() + "`n.offline-cache/`n") }
        if (-not $newCopy) {
            # The new kit's npm packages, for after the merge (offline there's no npm install):
            #   rmdir /s /q node_modules  &&  robocopy .offline-cache\node_modules node_modules /E
            Copy-Tree "$FrontendSource\node_modules" "$feCache\node_modules" -Mirror
            $behind = (Invoke-Git $FrontendDev @('rev-list', '--count', 'HEAD..offline/main') -PassThru) -join ''
            Write-Note "the kit's frontend code is the branch offline/main ($behind new commits): merge it with  git merge offline/main"
            Write-Note 'its npm packages are in .offline-cache\node_modules (see the README)'
        }
    }
    if ($newCopy) {
        # The package has no node_modules\.bin (links don't survive the trip): npm makes the
        # Windows command shims (next.cmd, eslint.cmd, ...) again, offline.
        Push-Location $FrontendDev
        try { Invoke-Native $Npm @('rebuild', '--ignore-scripts', '--offline', '--no-audit', '--no-fund') -Quiet } finally { Pop-Location }
        Write-Note "frontend copy ready ($((Invoke-Git $FrontendDev @('log', '--oneline', '-1') -PassThru) -join ''))"
    }
    Grant-Owner $FrontendDev
    if ($unpacked) { Remove-Tree $unpacked }
}

Write-Step 'Development copies ready'
Write-Host @"
    Backend   $BackendDev   (database $DbName on port $DevPgPort, settings in .env)
              cd $BackendDev
              .venv\Scripts\uvicorn config.asgi:application --reload --port $DevWebPort   (http://127.0.0.1:$DevWebPort/admin/)
              .venv\Scripts\python manage.py createsuperuser                       (a login for this copy)
              .venv\Scripts\pytest -q                                              (all tests)
              .venv\Scripts\ruff check . ; .venv\Scripts\ruff format .            (lint and format)
    Frontend  $FrontendDev
              cd $FrontendDev
              npm run dev        (http://127.0.0.1:3000, uses the development backend)
    Both:     git add -A ; git commit -m "..."     (record your changes)
"@
