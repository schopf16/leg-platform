#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Sets up MySQL and Python for local LEG-Platform development on Windows (D-027).

.DESCRIPTION
    For local development only. Never use this script for stage or production.

    1. Asks for the new password of the local MySQL 'root' account and validates it before
       anything is installed.
    2. Installs Python (default version: 3.14.8) and then MySQL Community Server (default
       version: 9.7.2, latest LTS series) with Chocolatey.
    3. Binds the server to 127.0.0.1 (the Chocolatey package sets no bind address) and
       disables X Plugin (mysqlx=OFF; Django does not use the X Protocol, and X Plugin listens
       on all interfaces on port 33060 by default). After the restart, the script verifies
       that the server listens only on 127.0.0.1 and not on port 33060.
    4. Sets the password of 'root'@'localhost', because the package initialises the data
       directory with --initialize-insecure (root without a password), and verifies a login
       with the new password.
    5. Creates the database 'leg_platform' and the account 'leg_platform'@'127.0.0.1' with
       only the privileges Django needs, on 'leg_platform' and on Django's test database
       'test_leg_platform'.
    6. Stores the local configuration as user environment variables (DB_NAME, DB_USER,
       DB_PASSWORD, DB_HOST, DB_PORT, DJANGO_SECRET_KEY, APP_ENV=local). Generated values are
       never printed.

    Until the setup has succeeded, the MySQL service has the start type Manual. If a step after
    the MySQL installation fails or the script is interrupted (e.g. with Ctrl+C), the script
    stops the MySQL service, sets its start type to Disabled, verifies both and prints recovery
    instructions (docs/INSTALLATION.md). Closing the console window may end the script without
    this cleanup; check the service afterwards in that case.

    The root password is entered interactively and is not stored by this script; keep it in
    your password manager. Passwords are passed to the mysql client via standard input or a
    temporary option file readable only by the current user, never on the command line.

    If the python314 Chocolatey package is already installed, the Python step is skipped.

    If a service named 'MySQL' already exists (e.g. from a failed run), the script stops
    without changing anything, unless -RemoveFailedInstallation is given.

.PARAMETER MySqlVersion
    Chocolatey package version of MySQL to install.

.PARAMETER PythonVersion
    Chocolatey package version of the python314 package to install.

.PARAMETER RemoveFailedInstallation
    Removes the local MySQL installation left behind by a failed run before the normal setup
    starts: stops the service, uninstalls the Chocolatey package 'mysql', deletes the service,
    deletes the data directory (only the package's default data directory, and only after
    you confirm by typing DELETE) and removes the leftover option file. All local MySQL
    databases are lost. For local development machines only.
#>
[CmdletBinding()]
param(
    [string]$MySqlVersion = '9.7.2',
    [string]$PythonVersion = '3.14.8',
    [switch]$RemoveFailedInstallation
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$ServiceName = 'MySQL'
$DatabaseName = 'leg_platform'
$TestDatabaseName = 'test_leg_platform'
$AppUser = 'leg_platform'
$MySqlPort = 3306
$XPluginPort = 33060
$MinimumRootPasswordLength = 15
$MySqlPackage = 'mysql'
$PythonPackage = 'python314'
# Data directory the Chocolatey package 'mysql' uses without the package parameter
# /dataLocation: "C:\ProgramData\MySQL" plus "\data" (its tools\chocolateyInstall.ps1, 9.7.2).
# Only this directory is ever deleted by -RemoveFailedInstallation.
$PackageDataDirectory = 'C:\ProgramData\MySQL\data'
$RetryCommand = '.\scripts\local\setup-dev-windows.ps1 -RemoveFailedInstallation'

# Privileges Django needs on MySQL: data access, and schema changes by migrations (CREATE,
# DROP, ALTER, INDEX; REFERENCES for foreign key constraints). On the test database, CREATE
# and DROP also allow the test runner to create and remove the database itself.
$AppPrivileges = 'SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, ALTER, INDEX, REFERENCES'

# Options this script manages in my.ini; existing lines for them are replaced. Dashes and
# underscores are interchangeable in option names, and "loose-" is an optional prefix.
$ManagedOptionPattern = '^\s*(loose[-_])?(bind[-_]address|mysqlx[-_]bind[-_]address|skip[-_]mysqlx|mysqlx)\s*(=.*)?$'

function New-RandomSecret {
    param([int]$Bytes = 32)
    $buffer = New-Object byte[] $Bytes
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($buffer) } finally { $rng.Dispose() }
    # URL-safe Base64 without padding: no quotes or backslashes that would need escaping in SQL.
    return [Convert]::ToBase64String($buffer).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

function ConvertFrom-SecureStringToPlain {
    param([Security.SecureString]$Value)
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Value)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
}

function Get-RootPasswordProblem {
    # Returns a description of the problem, or $null if the password is acceptable.
    # The password is written into an SQL string literal and into a MySQL option file. Only
    # printable ASCII is accepted, without characters that have a meaning there: quotes and
    # backslashes (SQL literal, option file quoting and escape sequences), '#' and ';'
    # (option file comments). Leading and trailing spaces would be removed from option file
    # values (MySQL Reference Manual, "Using Option Files").
    param([string]$Password)
    if ($Password.Length -lt $MinimumRootPasswordLength) {
        return "Use at least $MinimumRootPasswordLength characters."
    }
    if ($Password -cnotmatch '^[\x20-\x7E]+$') {
        return 'Use only printable ASCII characters (no umlauts or other non-ASCII characters).'
    }
    if ($Password -match '[#"''\\;]') {
        return 'The password must not contain # " '' \ or ;.'
    }
    if ($Password -ne $Password.Trim()) {
        return 'The password must not start or end with a space.'
    }
    return $null
}

function Read-RootPassword {
    while ($true) {
        $first = ConvertFrom-SecureStringToPlain (Read-Host -AsSecureString 'New password for the local MySQL root account')
        $second = ConvertFrom-SecureStringToPlain (Read-Host -AsSecureString 'Repeat the password')
        if ($first -cne $second) {
            Write-Warning 'The passwords do not match. Please try again.'
            continue
        }
        $problem = Get-RootPasswordProblem -Password $first
        if ($problem) {
            Write-Warning "$problem Please try again."
            continue
        }
        return $first
    }
}

function Invoke-MySql {
    param(
        [string]$Client,
        [string]$Sql,
        [string]$RootPassword
    )
    if ($RootPassword) {
        # The password is passed through a temporary option file that only the current user
        # can read, so that it appears neither on the command line nor in the process list.
        $optionFile = [IO.Path]::GetTempFileName()
        try {
            $acl = New-Object Security.AccessControl.FileSecurity
            $acl.SetAccessRuleProtection($true, $false)
            $identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
            $rule = New-Object Security.AccessControl.FileSystemAccessRule($identity, 'FullControl', 'Allow')
            $acl.AddAccessRule($rule)
            Set-Acl -Path $optionFile -AclObject $acl
            # The password contains only printable ASCII (Get-RootPasswordProblem).
            Set-Content -Path $optionFile -Encoding ascii -Value "[client]`npassword=$RootPassword"
            $Sql | & $Client "--defaults-extra-file=$optionFile" --user=root --batch
        } finally {
            Remove-Item -LiteralPath $optionFile -Force -ErrorAction SilentlyContinue
        }
    } else {
        $Sql | & $Client --user=root --skip-password --batch
    }
    if ($LASTEXITCODE -ne 0) { throw "mysql client exited with code $LASTEXITCODE" }
}

function Test-IsFile {
    param([string]$Path)
    try { return [bool](Test-Path -LiteralPath $Path -PathType Leaf) } catch { return $false }
}

function Resolve-ServiceExecutablePath {
    # Returns the existing executable that a service command line (Win32_Service.PathName)
    # starts, or $null. The Chocolatey package 'mysql' registers the service with
    # "mysqld --install MySQL", which results in an unquoted path without extension, e.g.
    # C:\tools\mysql\current\bin\mysqld MySQL (observed on 2026-10-11).
    # A quoted path is taken as it is. For an unquoted path, the white-space separated
    # prefixes are tried from the shortest to the longest, the order Microsoft documents for
    # CreateProcess (Microsoft Learn, "CreateProcessW function": "c:\program.exe", then
    # "c:\program files\sub.exe", ...). For a candidate without an extension, the path itself
    # and then the path with ".exe" appended are tried. Only existing files are returned.
    param([string]$PathName)
    if (-not $PathName) { return $null }
    $trimmed = $PathName.Trim()
    $candidates = @()
    if ($trimmed.StartsWith('"')) {
        $end = $trimmed.IndexOf('"', 1)
        if ($end -lt 2) { return $null }
        $candidates += $trimmed.Substring(1, $end - 1)
    } else {
        $tokens = @($trimmed -split '\s+')
        for ($count = 1; $count -le $tokens.Count; $count++) {
            $candidates += ($tokens[0..($count - 1)] -join ' ')
        }
    }
    foreach ($candidate in $candidates) {
        $paths = @($candidate)
        try {
            $extension = [IO.Path]::GetExtension($candidate)
        } catch {
            # Characters that are invalid in paths: not an executable path.
            continue
        }
        if (-not $extension) { $paths += "$candidate.exe" }
        foreach ($path in $paths) {
            if (Test-IsFile -Path $path) { return $path }
        }
    }
    return $null
}

function Get-OptionFilePath {
    # Returns the option file of the server: the value of --defaults-file in the service
    # command line, or otherwise my.ini in the base directory (the parent of bin), which
    # mysqld reads on Windows (MySQL Reference Manual, "Using Option Files": BASEDIR\my.ini).
    param([string]$PathName, [string]$Executable)
    # --defaults-file="C:\path with spaces\my.ini" or --defaults-file=C:\path\my.ini
    $match = [regex]::Match("$PathName", '--defaults-file=(?:"([^"]+)"|(\S+))')
    if ($match.Success) {
        if ($match.Groups[1].Success) { return $match.Groups[1].Value }
        return $match.Groups[2].Value
    }
    if (-not $Executable) { return $null }
    return Join-Path (Split-Path -Parent (Split-Path -Parent $Executable)) 'my.ini'
}

function Get-ServerPaths {
    $service = Get-CimInstance Win32_Service -Filter "Name='$ServiceName'"
    if (-not $service) { throw "Service '$ServiceName' was not created by the installation." }
    $mysqld = Resolve-ServiceExecutablePath -PathName $service.PathName
    if (-not $mysqld) { throw "mysqld not found in the service command line '$($service.PathName)'." }
    $client = Join-Path (Split-Path -Parent $mysqld) 'mysql.exe'
    if (-not (Test-IsFile -Path $client)) { throw "mysql client not found at $client" }
    $myIni = Get-OptionFilePath -PathName $service.PathName -Executable $mysqld
    if (-not (Test-IsFile -Path $myIni)) { throw "Option file not found at $myIni." }
    return [pscustomobject]@{ Client = $client; MyIni = $myIni }
}

function Read-TextFile {
    # Reads a text file and returns its text together with the encoding and line break, so
    # that it can be written back unchanged apart from the edited lines. A file without a byte
    # order mark must be valid UTF-8 (ASCII is a subset); otherwise the script stops instead of
    # guessing a code page and damaging the file.
    param([string]$Path)
    $bytes = [IO.File]::ReadAllBytes($Path)
    $strictUtf8 = New-Object Text.UTF8Encoding($false, $true)
    $encoding = $null
    $offset = 0
    if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) {
        $encoding = New-Object Text.UTF8Encoding($true, $true)
        $offset = 3
    } elseif ($bytes.Length -ge 2 -and $bytes[0] -eq 0xFF -and $bytes[1] -eq 0xFE) {
        $encoding = New-Object Text.UnicodeEncoding($false, $true, $true)
        $offset = 2
    } elseif ($bytes.Length -ge 2 -and $bytes[0] -eq 0xFE -and $bytes[1] -eq 0xFF) {
        $encoding = New-Object Text.UnicodeEncoding($true, $true, $true)
        $offset = 2
    } else {
        $encoding = $strictUtf8
    }
    try {
        $text = $encoding.GetString($bytes, $offset, $bytes.Length - $offset)
    } catch {
        # Strict decoders throw on invalid byte sequences.
        throw "$Path is not valid UTF-8 or UTF-16; edit it manually (see docs/INSTALLATION.md)."
    }
    $lineBreak = if ($text.Contains("`r`n")) { "`r`n" } else { "`n" }
    return [pscustomobject]@{ Text = $text; Encoding = $encoding; LineBreak = $lineBreak }
}

function Set-ServerNetworkOptions {
    # Removes existing lines for the managed options from all groups and appends a [mysqld]
    # group at the end of the file. "If multiple instances of a given option are found, the
    # last instance takes precedence" (MySQL Reference Manual, "Using Option Files"). Options
    # from files included with !include or !includedir are not edited; the listening sockets
    # are therefore verified after the restart (Assert-LoopbackOnly).
    param([string]$Path)
    $file = Read-TextFile -Path $Path
    $lines = $file.Text -split "\r?\n"
    $kept = @($lines | Where-Object { $_ -notmatch $ManagedOptionPattern })
    while ($kept.Count -gt 0 -and $kept[-1] -eq '') { $kept = @($kept | Select-Object -SkipLast 1) }
    $kept += @(
        '',
        '[mysqld]',
        '# Managed by scripts/local/setup-dev-windows.ps1 (D-027): loopback only, X Plugin off.',
        'bind-address=127.0.0.1',
        'mysqlx=OFF',
        ''
    )
    # Keep a backup next to the original; it contains no secrets.
    Copy-Item -LiteralPath $Path -Destination "$Path.before-leg-setup" -Force
    [IO.File]::WriteAllText($Path, ($kept -join $file.LineBreak), $file.Encoding)
}

function Get-ServiceProcessId {
    $service = Get-CimInstance Win32_Service -Filter "Name='$ServiceName'"
    if (-not $service -or $service.ProcessId -eq 0) { throw "Service '$ServiceName' is not running." }
    return [int]$service.ProcessId
}

function Assert-LoopbackOnly {
    # Waits until the server accepts connections on 127.0.0.1:$MySqlPort and verifies that it
    # listens on no other address and that nothing listens on the X Plugin port.
    $deadline = (Get-Date).AddSeconds(60)
    $listeners = @()
    while ((Get-Date) -lt $deadline) {
        $processId = Get-ServiceProcessId
        $listeners = @(Get-NetTCPConnection -State Listen -OwningProcess $processId -ErrorAction SilentlyContinue)
        if (@($listeners | Where-Object { $_.LocalPort -eq $MySqlPort }).Count -gt 0) { break }
        Start-Sleep -Seconds 2
    }
    if (@($listeners | Where-Object { $_.LocalAddress -eq '127.0.0.1' -and $_.LocalPort -eq $MySqlPort }).Count -eq 0) {
        throw "MySQL does not listen on 127.0.0.1:$MySqlPort."
    }
    $exposed = @($listeners | Where-Object { $_.LocalAddress -ne '127.0.0.1' })
    if ($exposed.Count -gt 0) {
        $addresses = ($exposed | ForEach-Object { "$($_.LocalAddress):$($_.LocalPort)" }) -join ', '
        throw "MySQL listens on addresses other than 127.0.0.1: $addresses."
    }
    $xPlugin = @(Get-NetTCPConnection -State Listen -LocalPort $XPluginPort -ErrorAction SilentlyContinue)
    if ($xPlugin.Count -gt 0) {
        throw "A process listens on the X Plugin port $XPluginPort; X Plugin is not disabled."
    }
}

function Assert-ServerConfiguration {
    # Positive check inside the server, independent of the timing of the listening sockets:
    # bind_address must be 127.0.0.1 and X Plugin ('mysqlx') must not be ACTIVE (MySQL
    # Reference Manual, "The INFORMATION_SCHEMA PLUGINS Table": PLUGIN_STATUS is one of ACTIVE,
    # INACTIVE, DISABLED, DELETING, DELETED).
    param([string]$Client, [string]$RootPassword)
    $sql = "SELECT CONCAT(@@GLOBAL.bind_address, '|', COALESCE((SELECT PLUGIN_STATUS FROM information_schema.PLUGINS WHERE PLUGIN_NAME = 'mysqlx'), 'NONE')) AS result;"
    $output = @(Invoke-MySql -Client $Client -Sql $sql -RootPassword $RootPassword)
    $result = @($output | Where-Object { "$_".Trim() -ne '' } | Select-Object -Last 1)
    if ($result.Count -eq 0) { throw 'The server configuration could not be read.' }
    $bindAddress, $xPluginStatus = "$($result[0])".Trim().Split('|')
    if ($bindAddress -ne '127.0.0.1') {
        throw "bind_address is '$bindAddress', not 127.0.0.1."
    }
    # Allowlist: the plugin is either not loaded (no row) or DISABLED; any other status
    # (ACTIVE, INACTIVE, DELETING, DELETED or unknown) fails.
    if ($xPluginStatus -cnotin @('NONE', 'DISABLED')) {
        throw "X Plugin (mysqlx) has status '$xPluginStatus'; expected no plugin or DISABLED."
    }
}

function Get-DataDirectory {
    param([string]$MyIni)
    if (-not $MyIni -or -not (Test-Path -LiteralPath $MyIni)) { return $null }
    try {
        $text = (Read-TextFile -Path $MyIni).Text
    } catch {
        return $null
    }
    # "If multiple instances of a given option are found, the last instance takes precedence"
    # (MySQL Reference Manual, "Using Option Files").
    $found = [regex]::Matches($text, '(?m)^\s*datadir\s*=\s*"?([^"\r\n]+?)"?\s*$')
    if ($found.Count -eq 0) { return $null }
    # In option files, "\\" stands for a single backslash (MySQL Reference Manual, "Using
    # Option Files"); the Chocolatey package writes datadir=C:\\ProgramData\\MySQL\\data.
    return $found[$found.Count - 1].Groups[1].Value.Replace('\\', '\')
}

function Test-IsSamePath {
    # Compares two absolute Windows paths case-insensitively, ignoring trailing separators
    # and "/" versus "\". Relative paths are never considered equal.
    param([string]$First, [string]$Second)
    if (-not $First -or -not $Second) { return $false }
    try {
        if (-not [IO.Path]::IsPathRooted($First) -or -not [IO.Path]::IsPathRooted($Second)) { return $false }
        $a = [IO.Path]::GetFullPath($First.Replace('/', '\')).TrimEnd('\')
        $b = [IO.Path]::GetFullPath($Second.Replace('/', '\')).TrimEnd('\')
    } catch {
        return $false
    }
    return [string]::Equals($a, $b, [StringComparison]::OrdinalIgnoreCase)
}

function Test-IsMySqlDataDirectory {
    # A MySQL 8.0 or later data directory contains the data dictionary tablespace mysql.ibd
    # and the system schema directory mysql (MySQL Reference Manual, "The MySQL Data
    # Directory"). An empty directory is also accepted.
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) { return $false }
    if (@(Get-ChildItem -LiteralPath $Path -Force).Count -eq 0) { return $true }
    return (Test-IsFile -Path (Join-Path $Path 'mysql.ibd')) -and
        (Test-Path -LiteralPath (Join-Path $Path 'mysql') -PathType Container)
}

function ConvertFrom-ChocolateyListOutput {
    # Returns the installed version of a package from "choco list <name> --exact
    # --limit-output" (lines of the form name|version), or $null.
    param([string[]]$Lines, [string]$Name)
    foreach ($line in @($Lines)) {
        $parts = "$line".Trim().Split('|')
        if ($parts.Count -eq 2 -and $parts[0] -ieq $Name -and $parts[1]) { return $parts[1] }
    }
    return $null
}

function Get-InstalledChocolateyPackageVersion {
    # Chocolatey 2 lists only locally installed packages with "choco list".
    param([string]$Name)
    $output = @(choco list $Name --exact --limit-output)
    if ($LASTEXITCODE -ne 0) { throw "Chocolatey exited with code $LASTEXITCODE while listing packages." }
    return ConvertFrom-ChocolateyListOutput -Lines $output -Name $Name
}

function Get-ChocolateyToolsRoot {
    # Installation root of the package 'mysql' without /installLocation: Chocolatey's
    # Get-ToolsLocation (helpers\functions\Get-ToolsLocation.ps1), i.e. the variable
    # ChocolateyToolsLocation or "<system drive>\tools". The older ChocolateyBinRoot is not
    # considered; the service command line is used whenever the service exists.
    $value = [Environment]::GetEnvironmentVariable('ChocolateyToolsLocation')
    if ($value) { return $value }
    return Join-Path $env:SystemDrive 'tools'
}

function Disable-MySqlService {
    if (-not (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue)) { return }
    Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
    # Stop-Service can fail while the service is still starting or stopping: wait up to 30
    # seconds for a stable state and try again.
    $deadline = (Get-Date).AddSeconds(30)
    while ((Get-Date) -lt $deadline) {
        $status = "$((Get-Service -Name $ServiceName -ErrorAction SilentlyContinue).Status)"
        if ($status -notin @('StartPending', 'StopPending', 'ContinuePending', 'PausePending')) { break }
        Start-Sleep -Seconds 2
    }
    Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
    Set-Service -Name $ServiceName -StartupType Disabled -ErrorAction SilentlyContinue
    $service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($service -and $service.Status -eq 'Stopped' -and "$($service.StartType)" -eq 'Disabled') {
        Write-Warning "The service '$ServiceName' was stopped and its start type set to Disabled."
    } else {
        Write-Host ''
        Write-Host "ERROR: The service '$ServiceName' could not be stopped and disabled. Run as administrator:" -ForegroundColor Red
        Write-Host "  sc.exe stop $ServiceName" -ForegroundColor Red
        Write-Host "  sc.exe config $ServiceName start= disabled" -ForegroundColor Red
        Write-Host "Then check with: Get-Service $ServiceName | Select-Object Status, StartType" -ForegroundColor Red
    }
}

function Write-RecoveryInstructions {
    param([string]$MyIni)
    $dataDir = Get-DataDirectory -MyIni $MyIni
    if (-not $dataDir) { $dataDir = '<the datadir in my.ini>' }
    Write-Host ''
    Write-Host 'Recovery (docs/INSTALLATION.md, "Recovery after a failed setup"):'
    Write-Host '  1. Uninstall MySQL:            choco uninstall mysql -y'
    Write-Host "  2. If the service still exists: sc.exe delete $ServiceName"
    Write-Host "  3. Delete the data directory:   $dataDir"
    Write-Host '     (it contains only the new, empty local server; root may already have the new password)'
    if ($MyIni) {
        Write-Host "  4. Delete the option file if it remains: $MyIni (and $MyIni.before-leg-setup)"
    }
    Write-Host '  5. Remove user environment variables set by this script, if any:'
    Write-Host '     APP_ENV, DB_NAME, DB_USER, DB_PASSWORD, DB_HOST, DB_PORT, DJANGO_SECRET_KEY'
    Write-Host '  6. Fix the cause shown above and run this script again.'
    Write-Host ''
    Write-Host 'Steps 1 to 4 are done automatically, after a confirmation, by running as administrator:'
    Write-Host "  $RetryCommand"
}

function Test-IsPathUnder {
    # True if $Path is $Root or lies below it. Both are normalised to full paths and compared
    # case-insensitively; relative paths are never accepted.
    param([string]$Path, [string]$Root)
    if (-not $Path -or -not $Root) { return $false }
    try {
        if (-not [IO.Path]::IsPathRooted($Path) -or -not [IO.Path]::IsPathRooted($Root)) { return $false }
        $fullPath = [IO.Path]::GetFullPath($Path.Replace('/', '\')).TrimEnd('\')
        $fullRoot = [IO.Path]::GetFullPath($Root.Replace('/', '\')).TrimEnd('\')
    } catch {
        return $false
    }
    return [string]::Equals($fullPath, $fullRoot, [StringComparison]::OrdinalIgnoreCase) -or
        $fullPath.StartsWith($fullRoot + '\', [StringComparison]::OrdinalIgnoreCase)
}

function Get-ServiceOwnershipProblem {
    # Returns $null if the service command line belongs to an installation of the Chocolatey
    # package 'mysql' that this script may remove, or a description of the problem otherwise:
    # the executable must lie under <tools>\mysql, and --defaults-file must be absent or point
    # to <tools>\mysql\current\my.ini.
    param([string]$PathName, [string]$Executable, [string]$ToolsRoot)
    $packageRoot = Join-Path $ToolsRoot $MySqlPackage
    if (-not $Executable) {
        return "the program of the service could not be found in its command line '$PathName'"
    }
    if (-not (Test-IsPathUnder -Path $Executable -Root $packageRoot)) {
        return "the service runs '$Executable', which is not under $packageRoot"
    }
    $match = [regex]::Match("$PathName", '--defaults-file=(?:"([^"]+)"|(\S+))')
    if ($match.Success) {
        $defaultsFile = Get-OptionFilePath -PathName $PathName -Executable $Executable
        if (-not (Test-IsSamePath -First $defaultsFile -Second (Join-Path $packageRoot 'current\my.ini'))) {
            return "the service uses the option file '$defaultsFile', not $(Join-Path $packageRoot 'current\my.ini')"
        }
    }
    return $null
}

function Test-IsReparsePoint {
    # True if the file or directory exists and is a reparse point (e.g. a symbolic link, a
    # junction or a mount point).
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return $false }
    $item = Get-Item -LiteralPath $Path -Force
    return [bool]($item.Attributes -band [IO.FileAttributes]::ReparsePoint)
}

function Get-ReparsePointPaths {
    # Returns the paths among those this script would delete or delete from that are reparse
    # points. The removal refuses to work through them.
    $packageRoot = Join-Path (Get-ChocolateyToolsRoot) $MySqlPackage
    $paths = @(
        (Split-Path -Parent $PackageDataDirectory),
        $PackageDataDirectory,
        $packageRoot,
        (Join-Path $packageRoot 'current')
    ) + @(Get-PackageOptionFiles)
    return @($paths | Where-Object { Test-IsReparsePoint -Path $_ })
}

function Get-PackageOptionFiles {
    # The only option files the removal deletes: my.ini of the package and the backup this
    # script writes next to it.
    $myIni = Join-Path (Get-ChocolateyToolsRoot) "$MySqlPackage\current\my.ini"
    return @($myIni, "$myIni.before-leg-setup")
}

function Get-FailedInstallation {
    # Collects what an earlier run left behind, without changing anything.
    $service = Get-CimInstance Win32_Service -Filter "Name='$ServiceName'"
    $ownershipProblem = $null
    $serviceRunning = $false
    if ($service) {
        $executable = Resolve-ServiceExecutablePath -PathName $service.PathName
        $ownershipProblem = Get-ServiceOwnershipProblem -PathName $service.PathName -Executable $executable -ToolsRoot (Get-ChocolateyToolsRoot)
        $serviceRunning = "$($service.State)" -ne 'Stopped'
    }
    $myIni = (Get-PackageOptionFiles)[0]
    return [pscustomobject]@{
        ServiceExists           = [bool]$service
        ServiceRunning          = $serviceRunning
        OwnershipProblem        = $ownershipProblem
        PackageVersion          = Get-InstalledChocolateyPackageVersion -Name $MySqlPackage
        OptionFiles             = @(Get-PackageOptionFiles | Where-Object { Test-IsFile -Path $_ })
        ConfiguredDataDirectory = Get-DataDirectory -MyIni $myIni
        DataDirectoryExists     = Test-Path -LiteralPath $PackageDataDirectory -PathType Container
        ReparsePoints           = @(Get-ReparsePointPaths)
    }
}

function Test-HasLeftovers {
    param($Installation)
    return $Installation.ServiceExists -or $Installation.PackageVersion -or
        $Installation.OptionFiles.Count -gt 0 -or $Installation.DataDirectoryExists
}

function Write-ManualRemovalInstructions {
    Write-Host ''
    Write-Host 'Nothing was changed. Check the installation and remove it manually if it is no longer needed' -ForegroundColor Red
    Write-Host '(docs/INSTALLATION.md, "Recovery after a failed setup", manual steps).' -ForegroundColor Red
}

function Confirm-FailedInstallationRemoval {
    # Checks that the installation is one this script may remove, lists everything that will be
    # removed and asks for the typed confirmation before anything is changed. Returns only if
    # the removal may proceed; throws otherwise.
    param($Installation)
    if ($Installation.OwnershipProblem) {
        Write-ManualRemovalInstructions
        throw "The service '$ServiceName' does not look like the local installation of the Chocolatey package '$MySqlPackage': $($Installation.OwnershipProblem)."
    }
    if ($Installation.ReparsePoints.Count -gt 0) {
        Write-ManualRemovalInstructions
        throw "These paths are symbolic links, junctions or mount points: $($Installation.ReparsePoints -join ', ')."
    }
    if ($Installation.DataDirectoryExists -and -not (Test-IsMySqlDataDirectory -Path $PackageDataDirectory)) {
        Write-ManualRemovalInstructions
        throw "'$PackageDataDirectory' does not look like a MySQL data directory (no mysql.ibd and mysql folder)."
    }
    if ($Installation.ConfiguredDataDirectory -and
        -not (Test-IsSamePath -First $Installation.ConfiguredDataDirectory -Second $PackageDataDirectory)) {
        # Never delete a directory other than the package default: a my.ini changed by hand
        # could point anywhere. The package creates a new my.ini with its default datadir.
        Write-Warning "my.ini names the data directory '$($Installation.ConfiguredDataDirectory)'. It is not the package default and is NOT deleted; delete it yourself if it is no longer needed."
    }
    Write-Host ''
    Write-Host 'The following local MySQL installation will be removed:' -ForegroundColor Yellow
    if ($Installation.ServiceExists) { Write-Host "  - Windows service '$ServiceName' (stopped and disabled first, then deleted)" }
    if ($Installation.PackageVersion) { Write-Host "  - Chocolatey package '$MySqlPackage' $($Installation.PackageVersion) (choco uninstall)" }
    foreach ($file in $Installation.OptionFiles) { Write-Host "  - Option file $file" }
    if ($Installation.DataDirectoryExists) {
        Write-Host "  - Data directory $PackageDataDirectory with ALL local MySQL databases (cannot be undone)" -ForegroundColor Yellow
    }
    $answer = Read-Host 'Type DELETE to remove all items listed above'
    if ($answer -cne 'DELETE') {
        throw 'Not confirmed. Nothing was changed.'
    }
}

function Wait-ServiceRemoved {
    # "sc.exe delete" only marks a service for deletion; it disappears once no handle is open.
    $deadline = (Get-Date).AddSeconds(30)
    while ((Get-Date) -lt $deadline) {
        if (-not (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue)) { return }
        Start-Sleep -Seconds 2
    }
    throw "The service '$ServiceName' still exists. Close the Services console and Task Manager, or restart Windows, then run: $RetryCommand"
}

function Remove-PackageDataDirectory {
    # Re-checks the data directory immediately before deleting it. [IO.Directory]::Delete with
    # recursive=true "does not recurse through the reparse point" (Microsoft Learn,
    # "Directory.Delete Method").
    $parent = Split-Path -Parent $PackageDataDirectory
    if ((Test-IsReparsePoint -Path $parent) -or (Test-IsReparsePoint -Path $PackageDataDirectory)) {
        throw "$PackageDataDirectory or its parent became a symbolic link, junction or mount point; it was not deleted."
    }
    if (-not (Test-IsMySqlDataDirectory -Path $PackageDataDirectory)) {
        throw "$PackageDataDirectory no longer looks like a MySQL data directory; it was not deleted."
    }
    [IO.Directory]::Delete($PackageDataDirectory, $true)
    if (Test-Path -LiteralPath $PackageDataDirectory) { throw "The data directory $PackageDataDirectory could not be deleted." }
    Write-Host "Data directory $PackageDataDirectory was deleted."
}

function Remove-FailedInstallation {
    # Removes the installation after Confirm-FailedInstallationRemoval. Every step checks the
    # current state, so that the removal can be repeated after an interruption.
    param($Installation)
    if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
        # First stop the service and set it to Disabled, so that it cannot start again while
        # the installation is removed.
        Disable-MySqlService
        if ("$((Get-Service -Name $ServiceName).Status)" -ne 'Stopped') {
            throw "The service '$ServiceName' could not be stopped. Nothing else was changed."
        }
    }
    if (Get-InstalledChocolateyPackageVersion -Name $MySqlPackage) {
        choco uninstall $MySqlPackage -y --no-progress
        if ($LASTEXITCODE -ne 0) { throw "Chocolatey exited with code $LASTEXITCODE while uninstalling MySQL." }
    }
    # The package has no uninstall script, so the service usually remains.
    if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
        sc.exe delete $ServiceName | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "sc.exe delete $ServiceName failed with exit code $LASTEXITCODE." }
        Wait-ServiceRemoved
        Write-Host "Service '$ServiceName' was deleted."
    }
    if ($Installation.DataDirectoryExists -and (Test-Path -LiteralPath $PackageDataDirectory)) {
        # Confirmed by the user in Confirm-FailedInstallationRemoval.
        Remove-PackageDataDirectory
    }
    foreach ($file in Get-PackageOptionFiles) {
        if (Test-IsFile -Path $file) {
            Remove-Item -LiteralPath $file -Force
            Write-Host "Option file $file was deleted."
        }
    }
}

# 0. Preconditions

if (-not (Get-Command choco -ErrorAction SilentlyContinue)) {
    throw 'Chocolatey (choco) was not found.'
}
$chocoVersion = "$(choco --version)".Trim()
if ($chocoVersion -notmatch '^(\d+)\.' -or [int]$Matches[1] -lt 2) {
    # "choco list" lists only local packages from Chocolatey 2 on.
    throw "Chocolatey 2 or later is required (found '$chocoVersion'). Run: choco upgrade chocolatey -y"
}

$failedInstallation = Get-FailedInstallation
if (Test-HasLeftovers -Installation $failedInstallation) {
    if (-not $RemoveFailedInstallation) {
        if ($failedInstallation.ServiceRunning) {
            # A server of a failed run may still run with a root account without password and
            # listen on all interfaces (docs/INSTALLATION.md, "Recovery after a failed setup").
            Write-Warning "The service '$ServiceName' is running. Stop and disable it as administrator: sc.exe stop $ServiceName; sc.exe config $ServiceName start= disabled"
        }
        throw "A local MySQL installation already exists (e.g. from a failed run). To remove it and start over, run as administrator: $RetryCommand"
    }
    Confirm-FailedInstallationRemoval -Installation $failedInstallation
} elseif ($RemoveFailedInstallation) {
    Write-Host 'No local MySQL installation found; nothing to remove.'
}

# 1. Ask for the root password before anything is installed

$rootPassword = Read-RootPassword

# 2. Remove the failed installation (only with -RemoveFailedInstallation, confirmed above)

if ($RemoveFailedInstallation -and (Test-HasLeftovers -Installation $failedInstallation)) {
    Remove-FailedInstallation -Installation $failedInstallation
    if (Test-HasLeftovers -Installation (Get-FailedInstallation)) {
        throw "Parts of the old installation remain. Check docs/INSTALLATION.md, then run: $RetryCommand"
    }
    Write-Host 'The old local MySQL installation was removed.'
}

# 3. Install Python unless the package is already installed
# Newest Python version supported by the newest Django release (D-015: the newest possible
# version is used for development; the minimum versions are only declared and tested in CI).

$installedPython = Get-InstalledChocolateyPackageVersion -Name $PythonPackage
if ($installedPython) {
    Write-Host "$PythonPackage $installedPython is already installed; skipping the installation."
    if ($installedPython -ne $PythonVersion) {
        Write-Warning "Expected $PythonPackage $PythonVersion. Update it with: choco upgrade $PythonPackage -y"
    }
} else {
    choco install $PythonPackage --version $PythonVersion -y --no-progress
    if ($LASTEXITCODE -ne 0) { throw "Chocolatey exited with code $LASTEXITCODE while installing Python." }
}

# 4. Install and configure MySQL; on any failure, leave the server stopped and disabled

$paths = $null
$completed = $false
try {
    choco install $MySqlPackage --version $MySqlVersion -y --no-progress
    if ($LASTEXITCODE -ne 0) { throw "Chocolatey exited with code $LASTEXITCODE while installing MySQL." }
    # The server must not start automatically before it is hardened.
    Set-Service -Name $ServiceName -StartupType Manual

    $paths = Get-ServerPaths
    Write-Host "Server option file: $($paths.MyIni)"

    # 4a. Loopback only, X Plugin off; verify the listening sockets

    Set-ServerNetworkOptions -Path $paths.MyIni
    Restart-Service -Name $ServiceName
    Assert-LoopbackOnly
    Write-Host "MySQL listens only on 127.0.0.1:$MySqlPort; X Plugin is disabled."

    # 4b. Set the root password and verify a login with it

    Invoke-MySql -Client $paths.Client -Sql "ALTER USER 'root'@'localhost' IDENTIFIED BY '$rootPassword';"
    Invoke-MySql -Client $paths.Client -Sql 'SELECT 1;' -RootPassword $rootPassword | Out-Null
    Assert-ServerConfiguration -Client $paths.Client -RootPassword $rootPassword

    # 4c. Create the database and the application account
    # In database-level GRANT statements, '_' is a wildcard and is escaped as '\_' so that
    # the grant covers exactly these databases (MySQL Reference Manual, "GRANT Statement").

    $appPassword = New-RandomSecret
    $grantDatabase = $DatabaseName.Replace('_', '\_')
    $grantTestDatabase = $TestDatabaseName.Replace('_', '\_')
    $sql = @"
CREATE DATABASE IF NOT EXISTS ``$DatabaseName`` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
CREATE USER '$AppUser'@'127.0.0.1' IDENTIFIED BY '$appPassword';
GRANT $AppPrivileges ON ``$grantDatabase``.* TO '$AppUser'@'127.0.0.1';
GRANT $AppPrivileges ON ``$grantTestDatabase``.* TO '$AppUser'@'127.0.0.1';
"@
    Invoke-MySql -Client $paths.Client -Sql $sql -RootPassword $rootPassword

    # 4d. Store the local configuration as user environment variables

    $settings = [ordered]@{
        APP_ENV           = 'local'
        DB_NAME           = $DatabaseName
        DB_USER           = $AppUser
        DB_PASSWORD       = $appPassword
        DB_HOST           = '127.0.0.1'
        DB_PORT           = "$MySqlPort"
        DJANGO_SECRET_KEY = (New-RandomSecret -Bytes 50)
    }
    foreach ($name in $settings.Keys) {
        [Environment]::SetEnvironmentVariable($name, $settings[$name], 'User')
    }
    $appPassword = $null

    Set-Service -Name $ServiceName -StartupType Automatic
    $completed = $true
} catch {
    Write-Warning "Setup failed: $($_.Exception.Message)"
    throw
} finally {
    # Also runs when the script is interrupted with Ctrl+C, which skips the catch block.
    $rootPassword = $null
    $appPassword = $null
    if (-not $completed) {
        Disable-MySqlService
        $myIni = if ($paths) { $paths.MyIni } else { $null }
        Write-RecoveryInstructions -MyIni $myIni
    }
}

Write-Host ''
Write-Host "MySQL $MySqlVersion is installed, listens only on 127.0.0.1, X Plugin is disabled, the root password is set and the service starts automatically."
Write-Host "Database '$DatabaseName' and account '$AppUser'@'127.0.0.1' were created."
Write-Host "User environment variables set: $($settings.Keys -join ', ')"
Write-Host 'Restart VS Code so that new terminals and Claude Code receive the variables.'
Write-Host 'Never print the environment (e.g. Get-ChildItem env:); it contains DB_PASSWORD and DJANGO_SECRET_KEY.'
