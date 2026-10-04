<#
.SYNOPSIS
Put a developer's committed changes live: the backend from backend-dev and/or the frontend
from frontend-dev, on this PC, without internet and without a new kit.

.DESCRIPTION
Run through C:\Management\deploy-backend.bat, deploy-frontend.bat or deploy-all.bat (they
ask for administrator rights), or in an administrator PowerShell:

    powershell -ExecutionPolicy Bypass -File deploy-dev.ps1 -Backend -Frontend

Only COMMITTED changes are deployed (git commit first). Each deploy is a new release next
to the running one; if the new one doesn't start, the previous one is put back.

Backend: safety backup, new release from `git archive HEAD`, Python packages from the
copy's .offline-cache\wheelhouse, database migrations, static files, restart.
Frontend: build of HEAD with the copy's node_modules, restart.

.PARAMETER DevUser
Whose development copies (default: you).
#>
[CmdletBinding()]
param(
    [switch]$Backend,
    [switch]$Frontend,
    [string]$Root = 'C:\Management',
    [string]$DevUser = $env:USERNAME,
    [switch]$Yes
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
. (Join-Path $PSScriptRoot 'common.ps1')

$Root = $Root.TrimEnd('\')
$Runtime = "$Root\runtime"
$Work = "$Root\work"
$Logs = "$Root\logs"
$EnvFile = "$Root\etc\backend.env"
$Node = "$Runtime\node\node.exe"
$GitExe = "$env:ProgramFiles\Git\cmd\git.exe"
$DjangoEnv = @{ DJANGO_ENV_FILE = ConvertTo-ForwardSlash $EnvFile; DJANGO_SETTINGS_MODULE = 'config.settings.prod' }

if (-not ($Backend -or $Frontend)) { Stop-WithError 'Say what to deploy: -Backend, -Frontend or both.' }
if (-not (Test-Administrator)) { Stop-WithError 'Run as administrator (the .bat files ask for it).' }
if (-not (Test-Path -LiteralPath $EnvFile)) { Stop-WithError "The system isn't installed in $Root." }
if (-not (Test-Path -LiteralPath $GitExe)) { Stop-WithError 'Git is missing: development copies are set up by install.cmd (-DevUser).' }

try {
    $sid = (New-Object Security.Principal.NTAccount($DevUser)).Translate([Security.Principal.SecurityIdentifier]).Value
} catch { Stop-WithError "No such Windows user: $DevUser" }
$userProfile = Get-CimInstance Win32_UserProfile | Where-Object { $_.SID -eq $sid } | Select-Object -First 1
if (-not $userProfile) { Stop-WithError "$DevUser has no user profile." }
$BackendDev = "$($userProfile.LocalPath)\backend-dev"
$FrontendDev = "$($userProfile.LocalPath)\frontend-dev"

function Invoke-Git([string]$Dir, [string[]]$GitArgs) {
    return Invoke-Native $GitExe (@('-c', 'safe.directory=*', '-C', $Dir) + $GitArgs) -PassThru
}

function Get-CommitInfo([string]$Dir, [string]$Label, [bool]$AssumeYes) {
    # The commit to deploy; uncommitted changes are NOT deployed, so say so.
    if (-not (Test-Path -LiteralPath "$Dir\.git")) { Stop-WithError "$Dir is not a development copy (no git repository)." }
    $commit = ((Invoke-Git $Dir @('rev-parse', '--short', 'HEAD')) -join '').Trim()
    $subject = ((Invoke-Git $Dir @('log', '-1', '--format=%s')) -join '').Trim()
    Write-Note "$Label $commit  $subject"
    $dirty = @(Invoke-Git $Dir @('status', '--porcelain', '--untracked-files=no') | Where-Object { $_ })
    if ($dirty.Count) {
        Write-Warn "$($dirty.Count) changed file(s) in $Dir are NOT committed and will NOT be deployed:"
        $dirty | Select-Object -First 10 | ForEach-Object { Write-Note "    $_" }
        if ((Read-Answer 'Deploy the last commit anyway? (yes/no)' 'no' -AssumeYes:$AssumeYes) -ne 'yes') {
            Stop-WithError 'Cancelled: commit your changes (git add -A && git commit -m "..."), then deploy again.'
        }
    }
    return $commit
}

function Export-Commit([string]$Dir, [string]$Destination) {
    # The committed files only (git archive), like the kit builder.
    $tar = "$Work\export-$([guid]::NewGuid().ToString('N')).tar"
    Invoke-Native $GitExe @('-c', 'safe.directory=*', '-C', $Dir, 'archive', '--format=tar', '-o', $tar, 'HEAD') -Quiet
    try { Expand-ArchiveFast $tar $Destination } finally { Remove-Item -LiteralPath $tar -Force -ErrorAction SilentlyContinue }
}

function Switch-Release([string]$Link, [string]$NewRelease, [string[]]$Services, [hashtable]$Ports) {
    # Point the junction at the new release and restart; if a service doesn't come up, put
    # the previous release back and restart again.
    $previous = Get-JunctionTarget $Link
    foreach ($svc in $Services) { Stop-ServiceSafely $svc }
    Set-Junction $Link $NewRelease
    try {
        foreach ($svc in $Services) { Start-ServiceChecked $svc $Logs -Port $Ports[$svc] }
    } catch {
        Write-Warn "The new release didn't start: going back to $previous"
        foreach ($svc in $Services) { Stop-ServiceSafely $svc }
        if ($previous) {
            Set-Junction $Link $previous
            foreach ($svc in $Services) { Start-Service -Name $svc -ErrorAction SilentlyContinue }
        }
        throw
    }
}

function Remove-OldReleases([string]$ReleasesDir, [string]$Link) {
    $active = Get-JunctionTarget $Link
    Get-ChildItem -LiteralPath $ReleasesDir -Directory | Sort-Object LastWriteTime -Descending |
        Select-Object -Skip 3 | Where-Object { $_.FullName -ne $active } | ForEach-Object { Remove-Tree $_.FullName }
}

New-Item -ItemType Directory -Path $Work -Force | Out-Null
$stamp = Get-Date -Format yyyyMMddHHmmss

# ============================================================================ backend
if ($Backend) {
    Write-Step "Backend from $BackendDev"
    $commit = Get-CommitInfo $BackendDev 'commit' $Yes.IsPresent
    $wheels = "$BackendDev\.offline-cache\wheelhouse"
    if (-not (Test-Path -LiteralPath $wheels)) { Stop-WithError "No ${wheels}: set the copy up with install.cmd first." }

    Write-Note 'safety backup...'
    & "$Root\backend\current\deploy\windows\backup.ps1" -Root $Root

    $release = "$Root\backend\releases\dev-$commit-$stamp"
    Write-Note "release $(Split-Path -Leaf $release)..."
    Export-Commit $BackendDev $release
    Invoke-Native "$Runtime\python\python.exe" @('-m', 'venv', "$release\.venv") -Quiet
    $venvPython = "$release\.venv\Scripts\python.exe"
    try {
        Invoke-Native $venvPython @('-m', 'pip', 'install', '--quiet', '--disable-pip-version-check', '--no-index',
            '--find-links', $wheels, '-r', "$release\requirements\windows.txt") -Quiet
    } catch {
        Remove-Tree $release
        Stop-WithError 'A Python package the code needs is not in the offline wheelhouse. New packages need a kit built on the internet machine.'
    }
    Invoke-Native $venvPython @('-m', 'compileall', '-q', $release) -Quiet -AllowFailure
    Push-Location $release
    try {
        Invoke-Native $venvPython @('manage.py', 'check', '--deploy', '--fail-level', 'ERROR') -Environment $DjangoEnv -Quiet
        Write-Note 'database migrations...'
        Invoke-Native $venvPython @('manage.py', 'migrate', '--noinput') -Environment $DjangoEnv
        Invoke-Native $venvPython @('manage.py', 'collectstatic', '--noinput') -Environment $DjangoEnv -Quiet
    } finally { Pop-Location }

    Switch-Release "$Root\backend\current" $release $AppServices @{
        'mgmt-web' = $Ports.Web; 'mgmt-ws' = $Ports.Ws; 'mgmt-tcp' = 9100
    }
    Remove-OldReleases "$Root\backend\releases" "$Root\backend\current"
    Write-Note "backend $commit is live"
}

# ============================================================================ frontend
if ($Frontend) {
    Write-Step "Frontend from $FrontendDev"
    $commit = Get-CommitInfo $FrontendDev 'commit' $Yes.IsPresent
    if (-not (Test-Path -LiteralPath "$FrontendDev\node_modules\next")) { Stop-WithError "$FrontendDev has no node_modules." }
    $build = "$Work\frontend-build"
    Remove-Tree $build
    Export-Commit $FrontendDev $build
    Write-Note 'copying node_modules...'
    Copy-Tree "$FrontendDev\node_modules" "$build\node_modules" -ExcludeDirs @("$FrontendDev\node_modules\.cache")
    Write-Note 'building (next build; a few minutes)...'
    Push-Location $build
    try {
        Invoke-Native $Node @("$build\node_modules\next\dist\bin\next", 'build') -Environment @{ NEXT_TELEMETRY_DISABLED = '1'; NODE_ENV = 'production' } -Quiet
    } finally { Pop-Location }
    if (-not (Test-Path -LiteralPath "$build\.next\standalone\server.js")) { Stop-WithError 'The build made no standalone server.' }
    $release = "$Root\frontend\releases\dev-$commit-$stamp"
    Copy-Tree "$build\.next\standalone" $release
    Copy-Tree "$build\.next\static" "$release\.next\static"
    if (Test-Path -LiteralPath "$build\public") { Copy-Tree "$build\public" "$release\public" }
    Remove-Tree $build
    Switch-Release "$Root\frontend\current" $release @('mgmt-frontend') @{ 'mgmt-frontend' = $Ports.Frontend }
    Remove-OldReleases "$Root\frontend\releases" "$Root\frontend\current"
    Write-Note "frontend $commit is live"
}

Write-Step 'Checks'
$failed = $false
foreach ($check in @(@('https://localhost/', 'frontend'), @('https://localhost/health/', 'backend'), @('https://localhost/health/db/', 'database'))) {
    $code = Wait-HttpOk $check[0] 30
    Write-Note ('{0,-12} {1}  {2}' -f $check[1], $code, $check[0])
    if ($code -notmatch '^(200|30[1278])$') { $failed = $true }
}
if ($failed) { Stop-WithError "Deployed, but something isn't answering. Logs: $Logs" }
Write-Host ''
Write-Host 'Deployed.' -ForegroundColor Green
