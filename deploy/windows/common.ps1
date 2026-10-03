# Shared helpers for the Windows scripts (install-all.ps1, setup-dev.ps1, backup.ps1, ...).
# Dot-sourced, not run. Written for Windows PowerShell 5.1 (built into Windows 10/11).
#
# Two 5.1 pitfalls this file works around everywhere:
# - Set-Content/Out-File write UTF-8 WITH a byte order mark, which breaks the first line of
#   backend.env, the Caddyfile and pgpass: Write-TextFile writes UTF-8 without one.
# - With $ErrorActionPreference = 'Stop', a native program's stderr output becomes a
#   terminating error: Invoke-Native runs programs with 'Continue' and checks the exit code.

# No Set-StrictMode: on Windows PowerShell 5.1 it makes `.Count` of a single value and any
# property of $null fatal ("The property 'Count' cannot be found on this object"), which
# broke the installer on a PC with one IP address. Values are checked explicitly instead.

# Names shared by the scripts. Service ids are what `Get-Service` and the Services app show.
$Script:ServiceIds = @{
    Postgres    = 'mgmt-postgres'
    PostgresDev = 'mgmt-postgres-dev'
    Garnet      = 'mgmt-garnet'
    Web         = 'mgmt-web'
    Ws          = 'mgmt-ws'
    Tcp         = 'mgmt-tcp'
    Frontend    = 'mgmt-frontend'
    Caddy       = 'mgmt-caddy'
}
$Script:AppServices = @('mgmt-web', 'mgmt-ws', 'mgmt-tcp')
$Script:Ports = @{ Web = 8000; Internal = 8001; Ws = 8002; Frontend = 3100; Garnet = 6379 }
$Script:FirewallGroup = 'Management system'
$Script:BackupTaskName = 'Management nightly backup'

# ------------------------------------------------------------------------------- output

function Write-Step([string]$Text) { Write-Host ''; Write-Host "==> $Text" -ForegroundColor Cyan }
function Write-Note([string]$Text) { Write-Host "    $Text" }
function Write-Warn([string]$Text) { Write-Host "    WARNING: $Text" -ForegroundColor Yellow }
function Stop-WithError([string]$Text) {
    Write-Host ''
    Write-Host "ERROR: $Text" -ForegroundColor Red
    throw $Text
}

function Read-Answer([string]$Question, [string]$Default, [switch]$AssumeYes) {
    if ($AssumeYes) { return $Default }
    $answer = Read-Host "    $Question [$Default]"
    if ([string]::IsNullOrWhiteSpace($answer)) { return $Default }
    return $answer.Trim()
}

# ------------------------------------------------------------------------------- files

$Script:Utf8NoBom = New-Object System.Text.UTF8Encoding($false)

function Write-TextFile([string]$Path, [string]$Text) {
    # UTF-8 without BOM and with Windows line endings left as given.
    $dir = Split-Path -Parent $Path
    if ($dir -and -not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    [System.IO.File]::WriteAllText($Path, $Text, $Script:Utf8NoBom)
}

function Read-TextFile([string]$Path) {
    return [System.IO.File]::ReadAllText($Path, $Script:Utf8NoBom)
}

function ConvertTo-ForwardSlash([string]$Path) { return $Path -replace '\\', '/' }

function Expand-Template([string]$Text, [hashtable]$Values) {
    # Replace __KEY__ placeholders; fail on any left over so a typo can't ship.
    foreach ($key in $Values.Keys) { $Text = $Text.Replace("__$($key)__", [string]$Values[$key]) }
    $left = [regex]::Matches($Text, '__[A-Z0-9_]+__') | ForEach-Object { $_.Value } | Select-Object -Unique
    if ($left) { throw "Template placeholders without a value: $($left -join ', ')" }
    return $Text
}

# KEY=value files (backend.env, backup.conf): plain lines, no quotes, # comments.
function Read-EnvFile([string]$Path) {
    $values = [ordered]@{}
    if (-not (Test-Path -LiteralPath $Path)) { return $values }
    foreach ($line in (Read-TextFile $Path) -split "`r?`n") {
        if ($line -match '^\s*#' -or $line -notmatch '=') { continue }
        $key, $value = $line -split '=', 2
        $values[$key.Trim()] = $value
    }
    return $values
}

function Set-EnvValue([string]$Path, [string]$Key, [string]$Value) {
    # Replace KEY=... in place (keeping comments and order), or append it. Returns $true
    # when the file changed.
    $lines = New-Object System.Collections.Generic.List[string]
    if (Test-Path -LiteralPath $Path) {
        foreach ($l in (Read-TextFile $Path) -split "`r?`n") { $lines.Add($l) }
        if ($lines.Count -and $lines[$lines.Count - 1] -eq '') { $lines.RemoveAt($lines.Count - 1) }
    }
    $found = $false
    $changed = $false
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match "^$([regex]::Escape($Key))=") {
            $found = $true
            $new = "$Key=$Value"
            if ($lines[$i] -ne $new) { $lines[$i] = $new; $changed = $true }
        }
    }
    if (-not $found) { $lines.Add("$Key=$Value"); $changed = $true }
    if ($changed) { Write-TextFile $Path (($lines -join "`r`n") + "`r`n") }
    return $changed
}

function New-RandomHex([int]$Bytes) {
    $buffer = New-Object byte[] $Bytes
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($buffer) } finally { $rng.Dispose() }
    return -join ($buffer | ForEach-Object { $_.ToString('x2') })
}

function Test-Sha256File([string]$File) {
    # FILE.sha256 holds "<hash>  <name>" (sha256sum format). Returns $true when it matches,
    # $null when there is no .sha256 file.
    $sumFile = "$File.sha256"
    if (-not (Test-Path -LiteralPath $sumFile)) { return $null }
    $expected = ((Read-TextFile $sumFile).Trim() -split '\s+')[0].ToLowerInvariant()
    $actual = (Get-FileHash -LiteralPath $File -Algorithm SHA256).Hash.ToLowerInvariant()
    return $expected -eq $actual
}

function Write-Sha256File([string]$File) {
    $hash = (Get-FileHash -LiteralPath $File -Algorithm SHA256).Hash.ToLowerInvariant()
    Write-TextFile "$File.sha256" "$hash  $(Split-Path -Leaf $File)`n"
}

# ------------------------------------------------------------------------------- programs

function Invoke-Native {
    # Run a program, stream its output, fail on a non-zero exit code. -Quiet hides the
    # output unless it fails; -PassThru returns the output lines instead of printing them.
    param(
        [Parameter(Mandatory)][string]$FilePath,
        [string[]]$ArgumentList = @(),
        [switch]$Quiet,
        [switch]$PassThru,
        [switch]$AllowFailure,
        [hashtable]$Environment = @{}
    )
    $saved = @{}
    foreach ($k in $Environment.Keys) {
        $saved[$k] = [Environment]::GetEnvironmentVariable($k, 'Process')
        [Environment]::SetEnvironmentVariable($k, [string]$Environment[$k], 'Process')
    }
    $oldPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = & $FilePath @ArgumentList 2>&1 | ForEach-Object { "$_" }
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $oldPreference
        foreach ($k in $saved.Keys) { [Environment]::SetEnvironmentVariable($k, $saved[$k], 'Process') }
    }
    if ($null -eq $output) { $output = @() }
    if ($code -ne 0 -and -not $AllowFailure) {
        $output | Select-Object -Last 40 | ForEach-Object { Write-Host "    | $_" }
        throw "$(Split-Path -Leaf $FilePath) failed (exit code $code)"
    }
    if ($PassThru) { return , @($output) }
    if (-not $Quiet) { $output | ForEach-Object { Write-Host "    | $_" } }
}

function Get-NativeExitCode([string]$FilePath, [string[]]$ArgumentList = @()) {
    # Run a program silently and return its exit code (for checks like pg_isready).
    $oldPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & $FilePath @ArgumentList *> $null; return $LASTEXITCODE } finally { $ErrorActionPreference = $oldPreference }
}

function Get-TarExe {
    $tar = Join-Path $env:SystemRoot 'System32\tar.exe'
    if (-not (Test-Path -LiteralPath $tar)) { Stop-WithError 'tar.exe is missing: Windows 10 version 1803 or newer is needed.' }
    return $tar
}

function Expand-ArchiveFast([string]$Archive, [string]$Destination) {
    # tar.exe (built into Windows) unpacks .zip, .tar.gz and .nupkg much faster than
    # Expand-Archive, which matters for node_modules (tens of thousands of files).
    if (-not (Test-Path -LiteralPath $Destination)) { New-Item -ItemType Directory -Path $Destination -Force | Out-Null }
    Invoke-Native (Get-TarExe) @('-xf', $Archive, '-C', $Destination) -Quiet
}

function Copy-Tree([string]$Source, [string]$Destination, [string[]]$ExcludeDirs = @(), [switch]$Mirror) {
    # robocopy: fast, keeps long paths working. Exit codes 0-7 mean success.
    $robocopyArgs = @($Source, $Destination, '/E', '/NFL', '/NDL', '/NJH', '/NJS', '/NP', '/R:2', '/W:2', '/MT:8')
    if ($Mirror) { $robocopyArgs += '/MIR' }
    if ($ExcludeDirs.Count) { $robocopyArgs += '/XD'; $robocopyArgs += $ExcludeDirs }
    $oldPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & robocopy.exe @robocopyArgs | Out-Null; $code = $LASTEXITCODE } finally { $ErrorActionPreference = $oldPreference }
    if ($code -ge 8) { throw "robocopy $Source -> $Destination failed (exit code $code)" }
}

function Remove-Tree([string]$Path) {
    # Remove-Item can't delete long paths (node_modules) on Windows PowerShell 5.1:
    # mirror an empty folder over it first.
    if (-not (Test-Path -LiteralPath $Path)) { return }
    $empty = Join-Path ([System.IO.Path]::GetTempPath()) ("empty-" + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $empty | Out-Null
    try { Copy-Tree $empty $Path -Mirror } finally { Remove-Item -LiteralPath $empty -Force }
    Remove-Item -LiteralPath $Path -Recurse -Force
}

function Set-Junction([string]$Link, [string]$Target) {
    # Point a junction (the "current" release) at Target. rmdir removes only the link;
    # Remove-Item on a junction can delete the target's files on PowerShell 5.1.
    if (Test-Path -LiteralPath $Link) {
        $item = Get-Item -LiteralPath $Link -Force
        if (-not ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw "$Link exists and is not a junction" }
        Invoke-Native "$env:SystemRoot\System32\cmd.exe" @('/c', 'rmdir', $Link) -Quiet
    }
    Invoke-Native "$env:SystemRoot\System32\cmd.exe" @('/c', 'mklink', '/J', $Link, $Target) -Quiet
}

function Get-JunctionTarget([string]$Link) {
    if (-not (Test-Path -LiteralPath $Link)) { return $null }
    $item = Get-Item -LiteralPath $Link -Force
    if ($item.PSObject.Properties['Target'] -and $item.Target) { return @($item.Target)[0] }
    return $null
}

# ------------------------------------------------------------------------------- access

function Set-FolderAccess {
    # Replace a folder's permissions: no inheritance from above, the given grants only.
    # Grants use icacls syntax, e.g. 'NT AUTHORITY\LocalService:(OI)(CI)RX'.
    param([string]$Path, [string[]]$Grants)
    if (-not (Test-Path -LiteralPath $Path)) { New-Item -ItemType Directory -Path $Path -Force | Out-Null }
    $icacls = "$env:SystemRoot\System32\icacls.exe"
    Invoke-Native $icacls @($Path, '/inheritance:r', '/grant:r', '*S-1-5-32-544:(OI)(CI)F', '/grant:r', '*S-1-5-18:(OI)(CI)F') -Quiet
    foreach ($grant in $Grants) { Invoke-Native $icacls @($Path, '/grant:r', $grant) -Quiet }
}

function Add-FolderAccess([string]$Path, [string[]]$Grants) {
    $icacls = "$env:SystemRoot\System32\icacls.exe"
    foreach ($grant in $Grants) { Invoke-Native $icacls @($Path, '/grant', $grant, '/T', '/C', '/Q') -Quiet }
}

# Well-known accounts by SID, so the scripts work on non-English Windows too.
$Script:LocalService = '*S-1-5-19'
$Script:NetworkService = '*S-1-5-20'
$Script:Users = '*S-1-5-32-545'

# ------------------------------------------------------------------------------- services

function New-WinswXml {
    # The XML for a service run by WinSW (a small program that makes any command a
    # Windows service: restart on failure, logs rolled by size).
    param(
        [string]$Id, [string]$Name, [string]$Description,
        [string]$Executable, [string]$Arguments, [string]$WorkingDirectory,
        [hashtable]$Environment = @{}, [string[]]$DependsOn = @(), [string]$LogPath
    )
    $esc = { param($s) [System.Security.SecurityElement]::Escape([string]$s) }
    $lines = @(
        '<service>',
        "  <id>$(& $esc $Id)</id>",
        "  <name>$(& $esc $Name)</name>",
        "  <description>$(& $esc $Description)</description>",
        "  <executable>$(& $esc $Executable)</executable>",
        "  <arguments>$(& $esc $Arguments)</arguments>",
        "  <workingdirectory>$(& $esc $WorkingDirectory)</workingdirectory>",
        '  <startmode>Automatic</startmode>',
        '  <stoptimeout>20 sec</stoptimeout>',
        '  <onfailure action="restart" delay="5 sec"/>',
        '  <onfailure action="restart" delay="20 sec"/>',
        '  <resetfailure>1 hour</resetfailure>',
        "  <logpath>$(& $esc $LogPath)</logpath>",
        '  <log mode="roll-by-size"><sizeThreshold>10240</sizeThreshold><keepFiles>5</keepFiles></log>'
    )
    foreach ($key in ($Environment.Keys | Sort-Object)) {
        $lines += "  <env name=`"$(& $esc $key)`" value=`"$(& $esc $Environment[$key])`"/>"
    }
    foreach ($dep in $DependsOn) { $lines += "  <depend>$(& $esc $dep)</depend>" }
    $lines += '</service>'
    return ($lines -join "`r`n") + "`r`n"
}

function Install-WinswService {
    # Create or update a WinSW service: ServicesDir\<id>\<id>.exe + <id>.xml. Leaves it
    # stopped. Runs as LocalSystem: under a lesser account (LocalService) WinSW can't tell
    # Windows that its program has exited ("Failed to open the service control manager
    # database. Access is denied"), so a crashed program left the service "Running" and was
    # never restarted.
    param([string]$ServicesDir, [string]$WinswExe, [string]$Id, [string]$Xml)
    $dir = Join-Path $ServicesDir $Id
    if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir | Out-Null }
    $exe = Join-Path $dir "$Id.exe"
    if (Get-Service -Name $Id -ErrorAction SilentlyContinue) { Stop-ServiceSafely $Id }
    Copy-Item -LiteralPath $WinswExe -Destination $exe -Force
    Write-TextFile (Join-Path $dir "$Id.xml") $Xml
    if (-not (Get-Service -Name $Id -ErrorAction SilentlyContinue)) {
        Invoke-Native $exe @('install') -Quiet
    }
    Set-ServiceAccount $Id 'LocalSystem'
}

function Set-ServiceAccount([string]$Name, [string]$Account) {
    # Through WMI rather than `sc.exe config obj= ... password= ""`: Windows PowerShell 5.1
    # drops empty arguments to programs, so sc.exe would never see the empty password.
    $svc = Get-CimInstance -ClassName Win32_Service -Filter "Name='$Name'"
    if ($svc.StartName -eq $Account) { return }
    $result = Invoke-CimMethod -InputObject $svc -MethodName Change -Arguments @{ StartName = $Account; StartPassword = '' }
    if ($result.ReturnValue -ne 0) { throw "Could not set the account of $Name to $Account (code $($result.ReturnValue))" }
}

function Stop-ServiceSafely([string]$Name) {
    $svc = Get-Service -Name $Name -ErrorAction SilentlyContinue
    if ($svc -and $svc.Status -ne 'Stopped') {
        Stop-Service -Name $Name -Force -ErrorAction SilentlyContinue
        $svc.WaitForStatus('Stopped', [TimeSpan]::FromSeconds(30))
    }
}

function Start-ServiceChecked([string]$Name, [string]$LogDir, [int]$Port = 0) {
    # Start a service and make sure its program really runs: "Running" only says the WinSW
    # wrapper started, so with -Port also wait until the program listens there.
    Start-Service -Name $Name
    Start-Sleep -Seconds 3
    $svc = Get-Service -Name $Name
    $listening = $true
    if ($Port -and $svc.Status -eq 'Running') {
        $listening = $false
        for ($i = 0; $i -lt 60 -and -not $listening; $i++) {
            $listening = Test-PortInUse $Port
            if (-not $listening) { Start-Sleep -Seconds 1 }
        }
    }
    if ($svc.Status -ne 'Running' -or -not $listening) {
        if ($LogDir) {
            Get-ChildItem -LiteralPath $LogDir -Filter "$Name*.log" -ErrorAction SilentlyContinue |
                Sort-Object LastWriteTime | Select-Object -Last 3 |
                ForEach-Object { Write-Host "    --- $($_.Name)"; Get-Content -LiteralPath $_.FullName -Tail 20 | ForEach-Object { Write-Host "    | $_" } }
        }
        if ($svc.Status -eq 'Running') { Stop-WithError "The service $Name runs, but nothing listens on port $Port (its program stopped; see its log above)." }
        Stop-WithError "The service $Name did not start (status: $($svc.Status))."
    }
}

function Test-PortInUse([int]$Port) {
    return [bool](Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)
}

function Get-PortOwner([int]$Port) {
    $conn = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $conn) { return $null }
    $proc = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
    if ($proc) { return $proc.ProcessName }
    return "process $($conn.OwningProcess)"
}

function Wait-HttpOk([string]$Url, [int]$Seconds = 60) {
    # curl.exe is built into Windows 10/11 and, unlike Invoke-WebRequest on PowerShell 5.1,
    # can skip the certificate check for the self-signed / internal certificate.
    $code = '000'
    for ($i = 0; $i -lt $Seconds; $i++) {
        $code = Get-HttpCode $Url
        if ($code -match '^(200|30[1278])$') { return $code }
        Start-Sleep -Seconds 1
    }
    return $code
}

function Get-HttpCode([string]$Url) {
    $curl = Join-Path $env:SystemRoot 'System32\curl.exe'
    $oldPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { $code = & $curl -sk -o NUL -w '%{http_code}' --max-time 10 $Url 2>$null } finally { $ErrorActionPreference = $oldPreference }
    if (-not $code) { return '000' }
    return "$code"
}

# ------------------------------------------------------------------------------- settings

function Get-ServerIPv4Candidates {
    # The machine's IPv4 addresses, the one with the default route first.
    # An offline network may have no default gateway (no route): then no interface comes first.
    $routeIf = $null
    $route = Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue | Sort-Object RouteMetric | Select-Object -First 1
    if ($route) { $routeIf = $route.InterfaceIndex }
    $addrs = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -notmatch '^(127\.|169\.254\.)' }
    $first = @($addrs | Where-Object { $_.InterfaceIndex -eq $routeIf } | ForEach-Object { $_.IPAddress })
    $rest = @($addrs | Where-Object { $_.InterfaceIndex -ne $routeIf } | ForEach-Object { $_.IPAddress })
    return @($first + $rest | Select-Object -Unique)
}

# Windows timezone names -> IANA names (the ones Django uses). Covers common zones; any
# other is asked for.
$Script:WindowsToIana = @{
    'North Korea Standard Time' = 'Asia/Pyongyang'; 'Korea Standard Time' = 'Asia/Seoul'
    'China Standard Time' = 'Asia/Shanghai'; 'Tokyo Standard Time' = 'Asia/Tokyo'
    'Russian Standard Time' = 'Europe/Moscow'; 'Vladivostok Standard Time' = 'Asia/Vladivostok'
    'Singapore Standard Time' = 'Asia/Singapore'; 'SE Asia Standard Time' = 'Asia/Bangkok'
    'India Standard Time' = 'Asia/Kolkata'; 'Arabian Standard Time' = 'Asia/Dubai'
    'GMT Standard Time' = 'Europe/London'; 'W. Europe Standard Time' = 'Europe/Berlin'
    'Central Europe Standard Time' = 'Europe/Budapest'; 'Romance Standard Time' = 'Europe/Paris'
    'E. Europe Standard Time' = 'Europe/Chisinau'; 'FLE Standard Time' = 'Europe/Kiev'
    'Eastern Standard Time' = 'America/New_York'; 'Central Standard Time' = 'America/Chicago'
    'Mountain Standard Time' = 'America/Denver'; 'Pacific Standard Time' = 'America/Los_Angeles'
    'AUS Eastern Standard Time' = 'Australia/Sydney'; 'UTC' = 'UTC'; 'Coordinated Universal Time' = 'UTC'
}

function Get-SuggestedTimeZone {
    $id = [System.TimeZoneInfo]::Local.Id
    if ($Script:WindowsToIana.ContainsKey($id)) { return $Script:WindowsToIana[$id] }
    return 'UTC'
}

function ConvertTo-LanguageCode([string]$Value) {
    switch -regex ($Value.ToLowerInvariant()) {
        '^(en|english)$' { return 'en' }
        '^(ko|ko-kp|kp|korean)$' { return 'ko-kp' }
        default { return '' }
    }
}

function Test-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    return (New-Object Security.Principal.WindowsPrincipal($identity)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Add-MachinePath([string[]]$Dirs) {
    $path = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $parts = @($path -split ';' | Where-Object { $_ })
    $changed = $false
    foreach ($d in $Dirs) {
        if ($parts -notcontains $d) { $parts += $d; $changed = $true }
    }
    if ($changed) { [Environment]::SetEnvironmentVariable('Path', ($parts -join ';'), 'Machine') }
    foreach ($d in $Dirs) { if (($env:Path -split ';') -notcontains $d) { $env:Path = "$env:Path;$d" } }
}

function Remove-MachinePath([string[]]$Dirs) {
    $path = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $parts = @($path -split ';' | Where-Object { $_ -and ($Dirs -notcontains $_) })
    [Environment]::SetEnvironmentVariable('Path', ($parts -join ';'), 'Machine')
}
