#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Sets up MySQL and Python for local LEG-Platform development on Windows (D-027).

.DESCRIPTION
    For local development only. Never use this script for stage or production.

    1. Asks for the new password of the local MySQL 'root' account and validates it before
       anything is installed.
    2. Installs Python (default version: 3.14.8) with Chocolatey, unless it is installed.
    3. Creates C:\LEG-MySQL with the subfolders server and data, each created with restricted
       permissions from the start (no inherited permissions; Administrators and SYSTEM full
       control; Users only read and execute, and only on server). The MySQL service runs as
       LocalSystem, so ordinary users must never be able to change its files.
    4. Writes the server option file my.ini before the installation: loopback only
       (bind-address=127.0.0.1), X Plugin off (mysqlx=OFF), base and data directory below
       C:\LEG-MySQL. The Chocolatey package keeps an existing my.ini, so the server is
       initialised and first started with these settings.
    5. Installs MySQL Community Server (default version: 9.7.2, latest LTS series) with the
       package parameters /installLocation and /dataLocation, verifies that my.ini is unchanged,
       that every file and folder below C:\LEG-MySQL has only the allowed permissions and
       owners, and that the server listens only on 127.0.0.1 and not on the X Plugin port.
    6. Sets the password of 'root'@'localhost' (the package initialises the data directory
       with --initialize-insecure, i.e. root without a password) and verifies a login with it.
    7. Creates the database 'leg_platform' and the account 'leg_platform'@'127.0.0.1' with
       only the privileges Django needs, on 'leg_platform' and on Django's test database
       'test_leg_platform'.
    8. Stores the local configuration as user environment variables (DB_NAME, DB_USER,
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

    If a MySQL installation already exists (from this script or from an earlier version that
    installed below C:\tools and C:\ProgramData\MySQL), the script stops without changing
    anything, unless -RemoveFailedInstallation is given.

.PARAMETER MySqlVersion
    Chocolatey package version of MySQL to install.

.PARAMETER PythonVersion
    Chocolatey package version of the python314 package to install.

.PARAMETER RemoveFailedInstallation
    Removes the local MySQL installation left behind by a failed run before the normal setup
    starts, after a list of everything that will be removed and a typed confirmation (DELETE).
    All local MySQL databases are lost. For local development machines only.
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
$RetryCommand = '.\scripts\local\setup-dev-windows.ps1 -RemoveFailedInstallation'

# Layout (D-027). A dedicated root without spaces: the package registers the service with an
# unquoted path. The package installs to <installLocation>\mysql\current and initialises the
# data directory <dataLocation>\mysql\data (tools\chocolateyInstall.ps1 of mysql 9.7.2).
$InstallRoot = 'C:\LEG-MySQL'
$ServerRoot = 'C:\LEG-MySQL\server'
$DataRoot = 'C:\LEG-MySQL\data'
$ServerPackageRoot = 'C:\LEG-MySQL\server\mysql'
$ServerCurrent = 'C:\LEG-MySQL\server\mysql\current'
$ServerOptionFile = 'C:\LEG-MySQL\server\mysql\current\my.ini'
$DataDirectory = 'C:\LEG-MySQL\data\mysql\data'

# Layout of the earlier version of this script (package defaults), which is only removed:
# <tools>\mysql (Chocolatey's tools directory) and C:\ProgramData\MySQL\data.
$OldDataParent = 'C:\ProgramData\MySQL'
$OldDataDirectory = 'C:\ProgramData\MySQL\data'

# Global option files that mysqld reads before BASEDIR\my.ini (MySQL Reference Manual, "Using
# Option Files"); none may exist, so that only the option file of this script applies.
$GlobalOptionFiles = @(
    (Join-Path $env:WINDIR 'my.ini'), (Join-Path $env:WINDIR 'my.cnf'), 'C:\my.ini', 'C:\my.cnf'
)

# Login path file that MySQL client programs read even with --defaults-file or --no-defaults
# (MySQL Reference Manual, "Command-Line Options that Affect Option-File Handling"); it must
# not exist, so that the client calls of this script use only their own option file.
$LoginPathFile = Join-Path ([Environment]::GetFolderPath('ApplicationData')) 'MySQL\.mylogin.cnf'

# Environment variables that change the behaviour of MySQL client programs or the option
# files they read (MySQL Reference Manual, "Environment Variables"); removed from the
# script's process before the first client call.
$MySqlEnvironmentVariables = @(
    'MYSQL_TEST_LOGIN_FILE', 'MYSQL_HOST', 'MYSQL_TCP_PORT', 'MYSQL_PWD', 'MYSQL_UNIX_PORT',
    'MYSQL_HOME', 'MYSQL_GROUP_SUFFIX', 'LIBMYSQL_PLUGIN_DIR', 'LIBMYSQL_PLUGINS',
    'LIBMYSQL_ENABLE_CLEARTEXT_PLUGIN'
)

# Accounts that the initialisation of a MySQL 9.7 data directory creates (MySQL Reference
# Manual, "Reserved Accounts"). After the root password is set, no other account may exist.
$ReservedAccounts = @('root@localhost', 'mysql.sys@localhost', 'mysql.session@localhost', 'mysql.infoschema@localhost')

# PATH entry that the earlier layout added for the current user (Install-ChocolateyPath).
$LegacyPathEntry = 'C:\tools\mysql\current\bin'

# Well-known SIDs (Microsoft Learn, "Security identifiers"); names are localised, SIDs are not.
$SidAdministrators = 'S-1-5-32-544'
$SidSystem = 'S-1-5-18'
$SidUsers = 'S-1-5-32-545'

# Rights that allow changing a file or directory, its attributes, permissions or owner.
$WriteRights = [Security.AccessControl.FileSystemRights](
    [int][Security.AccessControl.FileSystemRights]::WriteData -bor
    [int][Security.AccessControl.FileSystemRights]::AppendData -bor
    [int][Security.AccessControl.FileSystemRights]::WriteExtendedAttributes -bor
    [int][Security.AccessControl.FileSystemRights]::WriteAttributes -bor
    [int][Security.AccessControl.FileSystemRights]::Delete -bor
    [int][Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles -bor
    [int][Security.AccessControl.FileSystemRights]::ChangePermissions -bor
    [int][Security.AccessControl.FileSystemRights]::TakeOwnership
)

# Privileges Django needs on MySQL: data access, and schema changes by migrations (CREATE,
# DROP, ALTER, INDEX; REFERENCES for foreign key constraints). On the test database, CREATE
# and DROP also allow the test runner to create and remove the database itself.
$AppPrivileges = 'SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, ALTER, INDEX, REFERENCES'

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
            $arguments = Get-MySqlClientArguments -OptionFile $optionFile
            $Sql | & $Client @arguments
        } finally {
            Remove-Item -LiteralPath $optionFile -Force -ErrorAction SilentlyContinue
        }
    } else {
        $arguments = Get-MySqlClientArguments
        $Sql | & $Client @arguments
    }
    if ($LASTEXITCODE -ne 0) { throw "mysql client exited with code $LASTEXITCODE" }
}

function Get-MySqlClientArguments {
    # Arguments of the mysql client. --defaults-file ("Read only the given option file") or
    # --no-defaults must come first; clients still read .mylogin.cnf, whose absence is checked
    # in the preconditions (MySQL Reference Manual, "Command-Line Options that Affect
    # Option-File Handling"). Protocol, host and port are given on the command line, which
    # "take precedence over values specified in option files and environment variables"
    # (MySQL Reference Manual, "Environment Variables").
    param([string]$OptionFile)
    $arguments = @()
    if ($OptionFile) { $arguments += "--defaults-file=$OptionFile" } else { $arguments += '--no-defaults' }
    $arguments += @('--protocol=TCP', '--host=127.0.0.1', "--port=$MySqlPort", '--user=root')
    if (-not $OptionFile) { $arguments += '--skip-password' }
    $arguments += '--batch'
    return $arguments
}

function Clear-MySqlClientEnvironment {
    # Removes the environment variables that change the behaviour of MySQL programs (MySQL
    # Reference Manual, "Environment Variables") from this script's process only; their values
    # are never printed. The user and machine environment is not changed.
    foreach ($name in $MySqlEnvironmentVariables) {
        # The Environment provider removes the variable from the process environment.
        Remove-Item -LiteralPath "Env:\$name" -ErrorAction SilentlyContinue
    }
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
    # Installation root of the package 'mysql' in the earlier layout (without
    # /installLocation): Chocolatey's Get-ToolsLocation uses the variable
    # ChocolateyToolsLocation or "<system drive>\tools" (helpers\functions\Get-ToolsLocation.ps1).
    # Only the machine-wide value or exactly C:\tools is used: process and user values can be
    # set without administrator rights and must not decide which folders are deleted.
    $value = [Environment]::GetEnvironmentVariable('ChocolateyToolsLocation', 'Machine')
    if ($value) { return $value }
    return 'C:\tools'
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

function Test-IsReparsePoint {
    # True if the file or directory exists and is a reparse point (e.g. a symbolic link, a
    # junction or a mount point).
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return $false }
    $item = Get-Item -LiteralPath $Path -Force
    return [bool]($item.Attributes -band [IO.FileAttributes]::ReparsePoint)
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

function Test-RightsAllowWrite {
    # Takes the raw value: generic rights are not defined in the FileSystemRights enumeration.
    param([long]$Rights)
    # Generic rights (e.g. GENERIC_WRITE 0x40000000, GENERIC_ALL 0x10000000) can appear in
    # inherit-only ACEs; they are treated as write access.
    $generic = 0x10000000 -bor 0x40000000
    return (($Rights -band [long]$WriteRights) -ne 0) -or (($Rights -band $generic) -ne 0)
}

function Get-ServerOptionFileContent {
    # The option file written before the installation. "/" may be used in Windows path names
    # in option files and is treated as "\" (MySQL Reference Manual, "Using Option Files").
    return @(
        '[mysqld]',
        '# Written by scripts/local/setup-dev-windows.ps1 before the installation (D-027).',
        "basedir=$($ServerCurrent.Replace('\', '/'))",
        "datadir=$($DataDirectory.Replace('\', '/'))",
        "port=$MySqlPort",
        'bind-address=127.0.0.1',
        'mysqlx=OFF',
        ''
    ) -join "`r`n"
}

function New-RestrictedDirectorySecurity {
    # Permissions of the folders below C:\LEG-MySQL: no inheritance from the parent, full
    # control for Administrators and SYSTEM, optionally read and execute for Users; inherited
    # by all files and folders created below. With -SetOwner, the owner is Administrators.
    param([switch]$UsersReadExecute, [switch]$SetOwner)
    $inherit = [Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
    $propagation = [Security.AccessControl.PropagationFlags]::None
    $security = New-Object Security.AccessControl.DirectorySecurity
    $security.SetAccessRuleProtection($true, $false)
    $grants = @(
        @($SidAdministrators, [Security.AccessControl.FileSystemRights]::FullControl),
        @($SidSystem, [Security.AccessControl.FileSystemRights]::FullControl)
    )
    if ($UsersReadExecute) { $grants += , @($SidUsers, [Security.AccessControl.FileSystemRights]::ReadAndExecute) }
    foreach ($grant in $grants) {
        $sid = New-Object Security.Principal.SecurityIdentifier($grant[0])
        $rule = New-Object Security.AccessControl.FileSystemAccessRule($sid, $grant[1], $inherit, $propagation, 'Allow')
        $security.AddAccessRule($rule)
    }
    if ($SetOwner) { $security.SetOwner((New-Object Security.Principal.SecurityIdentifier($SidAdministrators))) }
    return $security
}

function New-RestrictedDirectory {
    # Creates a directory that does not exist yet, with its permissions set at creation
    # (DirectoryInfo.Create(DirectorySecurity) in .NET Framework, FileSystemAclExtensions.Create
    # in .NET; Microsoft Learn). Both do nothing if the directory exists, so its absence is
    # checked before and the result is verified by Assert-InstallationSecurity.
    param([string]$Path, [switch]$UsersReadExecute)
    if (Test-Path -LiteralPath $Path) { throw "$Path already exists." }
    $parent = Split-Path -Parent $Path
    if (Test-IsReparsePoint -Path $parent) { throw "$parent is a symbolic link, junction or mount point." }
    $security = New-RestrictedDirectorySecurity -UsersReadExecute:$UsersReadExecute -SetOwner
    $info = New-Object IO.DirectoryInfo($Path)
    $extensions = 'System.IO.FileSystemAclExtensions' -as [type]
    if ($extensions) {
        $extensions::Create($info, $security)
    } else {
        $info.Create($security)
    }
}

function New-RestrictedFileSecurity {
    # Permissions of a file written by this script: no inheritance, full control for
    # Administrators and SYSTEM, optionally read and execute for Users, owner Administrators.
    param([switch]$UsersReadExecute, [switch]$SetOwner)
    $security = New-Object Security.AccessControl.FileSecurity
    $security.SetAccessRuleProtection($true, $false)
    $grants = @(
        @($SidAdministrators, [Security.AccessControl.FileSystemRights]::FullControl),
        @($SidSystem, [Security.AccessControl.FileSystemRights]::FullControl)
    )
    if ($UsersReadExecute) { $grants += , @($SidUsers, [Security.AccessControl.FileSystemRights]::ReadAndExecute) }
    foreach ($grant in $grants) {
        $sid = New-Object Security.Principal.SecurityIdentifier($grant[0])
        $security.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($sid, $grant[1], 'Allow')))
    }
    if ($SetOwner) { $security.SetOwner((New-Object Security.Principal.SecurityIdentifier($SidAdministrators))) }
    return $security
}

function New-RestrictedFile {
    # Creates a new file (it must not exist) with its permissions set at creation and writes
    # $Content as ASCII. .NET Framework: FileStream constructor with FileSecurity; .NET:
    # FileSystemAclExtensions.Create(FileInfo, ..., FileSecurity) (Microsoft Learn).
    param([string]$Path, [string]$Content, [switch]$UsersReadExecute)
    if (Test-Path -LiteralPath $Path) { throw "$Path already exists." }
    $security = New-RestrictedFileSecurity -UsersReadExecute:$UsersReadExecute -SetOwner
    $rights = [Security.AccessControl.FileSystemRights]::FullControl
    $extensions = 'System.IO.FileSystemAclExtensions' -as [type]
    if ($extensions) {
        $stream = $extensions::Create((New-Object IO.FileInfo($Path)), [IO.FileMode]::CreateNew, $rights, [IO.FileShare]::None, 4096, [IO.FileOptions]::None, $security)
    } else {
        $stream = New-Object IO.FileStream($Path, [IO.FileMode]::CreateNew, $rights, [IO.FileShare]::None, 4096, [IO.FileOptions]::None, $security)
    }
    try {
        $bytes = [Text.Encoding]::ASCII.GetBytes($Content)
        $stream.Write($bytes, 0, $bytes.Length)
    } finally {
        $stream.Dispose()
    }
}

function Get-SignedInUser {
    # User signed in at the console: Win32_ComputerSystem.UserName ("Name of a user that is
    # logged on currently"; empty for remote sessions). Returns $null if unknown.
    try {
        $name = (Get-CimInstance Win32_ComputerSystem).UserName
        if ($name) { return "$name" }
    } catch {
    }
    return $null
}

function Get-UnexpectedAccounts {
    # Returns the accounts (user@host) of the output of the account query that are not
    # reserved accounts. The first line is the column header 'account' (mysql --batch).
    param([string[]]$Lines)
    $accounts = @($Lines | ForEach-Object { "$_".Trim() } | Where-Object { $_ -and $_ -ne 'account' })
    return @($accounts | Where-Object { $_ -notin $ReservedAccounts })
}

function Assert-ReservedAccountsOnly {
    # After the initialisation, only the reserved accounts may exist (MySQL Reference
    # Manual, "Reserved Accounts"); checked before the application account is created.
    param([string]$Client, [string]$RootPassword)
    $output = @(Invoke-MySql -Client $Client -Sql "SELECT CONCAT(user, '@', host) AS account FROM mysql.user ORDER BY account;" -RootPassword $RootPassword)
    if ($output.Count -lt 2) { throw 'The MySQL accounts could not be read.' }
    $unexpected = @(Get-UnexpectedAccounts -Lines $output)
    if ($unexpected.Count -gt 0) { throw "Unexpected MySQL accounts: $($unexpected -join ', ')." }
}

function Get-PresentOptionFiles {
    # Returns the paths that exist (as file or folder) among $Paths.
    param([string[]]$Paths)
    return @($Paths | Where-Object { $_ -and (Test-Path -LiteralPath $_) })
}

function Test-IsAllowedOwner {
    param([string]$Sid)
    return $Sid -in @($SidAdministrators, $SidSystem)
}

function Get-AceViolation {
    # Allowlist for one access control entry: deny entries are always allowed; allow entries
    # only for Administrators and SYSTEM, and, with -AllowUsersReadExecute, for Users without
    # any write right (generic rights count as write rights). Returns $null or a description.
    param([string]$Sid, [long]$Rights, [string]$Type, [switch]$AllowUsersReadExecute)
    if ($Type -eq 'Deny') { return $null }
    if ($Sid -in @($SidAdministrators, $SidSystem)) { return $null }
    if ($AllowUsersReadExecute -and $Sid -eq $SidUsers -and -not (Test-RightsAllowWrite -Rights $Rights)) {
        return $null
    }
    return "$Type $Sid $Rights"
}

function Get-SecurityViolations {
    # Checks the owner and every access control entry of $Path and, with -Recurse, of every
    # file and folder below it. Reparse points are never followed; each one is a violation.
    param([string]$Path, [switch]$AllowUsersReadExecute, [switch]$Recurse)
    $violations = @()
    $pending = New-Object System.Collections.Generic.Queue[string]
    $pending.Enqueue($Path)
    while ($pending.Count -gt 0) {
        $current = $pending.Dequeue()
        $item = Get-Item -LiteralPath $current -Force
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            $violations += "$current is a reparse point"
            continue
        }
        $acl = Get-Acl -LiteralPath $current
        $owner = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
        if (-not (Test-IsAllowedOwner -Sid $owner)) { $violations += "$current has the owner $owner" }
        foreach ($rule in $acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])) {
            $violation = Get-AceViolation -Sid $rule.IdentityReference.Value -Rights ([long]$rule.FileSystemRights) -Type "$($rule.AccessControlType)" -AllowUsersReadExecute:$AllowUsersReadExecute
            if ($violation) { $violations += "${current}: $violation" }
        }
        if ($Recurse -and $item.PSIsContainer) {
            foreach ($child in @(Get-ChildItem -LiteralPath $current -Force)) { $pending.Enqueue($child.FullName) }
        }
    }
    return $violations
}

function Get-TreeItemsWithoutReparsePoints {
    # Returns the full paths of $Path and every file and folder below it. Throws on the first
    # reparse point, which is never followed.
    param([string]$Path)
    $items = @()
    $pending = New-Object System.Collections.Generic.Queue[string]
    $pending.Enqueue($Path)
    while ($pending.Count -gt 0) {
        $current = $pending.Dequeue()
        $item = Get-Item -LiteralPath $current -Force
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw "$current is a symbolic link, junction or mount point."
        }
        $items += $current
        if ($item.PSIsContainer) {
            foreach ($child in @(Get-ChildItem -LiteralPath $current -Force)) { $pending.Enqueue($child.FullName) }
        }
    }
    return $items
}

function Set-OwnerAdministrators {
    # Sets the owner of $Path and every file and folder below it to Administrators. The owner
    # of objects created by administrators depends on the policy "System objects: Default
    # owner for objects created by members of the Administrators group", which may make the
    # installing user the owner; an owner can always change the permissions. Uses
    # ObjectSecurity.SetOwner and Set-Acl (Microsoft Learn), item by item: all items are
    # first enumerated without following reparse points (any reparse point fails before
    # anything is changed), and each item is checked again immediately before its change.
    param([string]$Path)
    $administrators = New-Object Security.Principal.SecurityIdentifier($SidAdministrators)
    foreach ($itemPath in @(Get-TreeItemsWithoutReparsePoints -Path $Path)) {
        $item = Get-Item -LiteralPath $itemPath -Force
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw "$itemPath became a symbolic link, junction or mount point."
        }
        $acl = Get-Acl -LiteralPath $itemPath
        if ($acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -ne $SidAdministrators) {
            $acl.SetOwner($administrators)
            Set-Acl -LiteralPath $itemPath -AclObject $acl
        }
    }
}

function Get-ItemOwnerSid {
    param([string]$Path)
    return (Get-Acl -LiteralPath $Path).GetOwner([Security.Principal.SecurityIdentifier]).Value
}

function Assert-InstallationSecurity {
    # Every file and folder below C:\LEG-MySQL: owner Administrators or SYSTEM; access only
    # for Administrators and SYSTEM, and read and execute for Users below server.
    $violations = @(Get-SecurityViolations -Path $InstallRoot) +
        @(Get-SecurityViolations -Path $ServerRoot -AllowUsersReadExecute -Recurse) +
        @(Get-SecurityViolations -Path $DataRoot -Recurse)
    $unexpected = @(Get-ChildItem -LiteralPath $InstallRoot -Force | Where-Object { $_.Name -notin @('server', 'data') })
    if ($unexpected.Count -gt 0) { $violations += "unexpected entries in ${InstallRoot}: $($unexpected.Name -join ', ')" }
    if ($violations.Count -gt 0) {
        throw "Unexpected permissions below ${InstallRoot}: $($violations -join '; ')"
    }
}

function Get-ServiceOwnershipProblem {
    # Returns $null if the service command line belongs to an installation this script may
    # remove: the program lies under the package directory of the new layout
    # (C:\LEG-MySQL\server\mysql) or of the earlier layout (<tools>\mysql), and --defaults-file
    # is absent or points to that layout's current\my.ini. Otherwise returns the problem.
    param([string]$PathName, [string]$Executable, [string]$ToolsRoot)
    if (-not $Executable) {
        return "the program of the service could not be found in its command line '$PathName'"
    }
    $oldPackageRoot = Join-Path $ToolsRoot $MySqlPackage
    $packageRoot = $null
    foreach ($candidate in @($ServerPackageRoot, $oldPackageRoot)) {
        if (Test-IsPathUnder -Path $Executable -Root $candidate) { $packageRoot = $candidate }
    }
    if (-not $packageRoot) {
        return "the service runs '$Executable', which is neither under $ServerPackageRoot nor under $oldPackageRoot"
    }
    $expected = Join-Path $packageRoot 'current\my.ini'
    if ([regex]::IsMatch("$PathName", '--defaults-file=')) {
        $defaultsFile = Get-OptionFilePath -PathName $PathName -Executable $Executable
        if (-not (Test-IsSamePath -First $defaultsFile -Second $expected)) {
            return "the service uses the option file '$defaultsFile', not $expected"
        }
    }
    return $null
}

function Remove-PathListEntry {
    # Returns the PATH value without the entries that are the same path as one of
    # $Directories; all other entries, including unexpanded ones such as %USERPROFILE%\bin,
    # are kept unchanged and in order.
    param([string]$Value, [string[]]$Directories)
    if (-not $Value) { return $Value }
    $kept = @($Value.Split(';') | Where-Object {
        $entry = $_
        -not ($entry -and ($Directories | Where-Object { Test-IsSamePath -First $entry -Second $_ }))
    })
    return ($kept -join ';')
}

function Get-MySqlPathEntries {
    # Returns the entries of a PATH value that point to a MySQL bin folder.
    param([string]$Value)
    if (-not $Value) { return @() }
    return @($Value.Split(';') | Where-Object { $_ -match '(?i)mysql.*\\bin\\?$' })
}

function Read-PathValue {
    # Reads a PATH value from the registry without expanding environment variables and
    # returns it with its value kind, so that a REG_EXPAND_SZ value is written back as such
    # (Microsoft Learn, "RegistryValueOptions": DoNotExpandEnvironmentNames; RegistryKey.GetValueKind).
    param([Microsoft.Win32.RegistryKey]$Key)
    if (-not $Key -or -not ($Key.GetValueNames() -contains 'Path')) { return $null }
    return [pscustomobject]@{
        Value = [string]$Key.GetValue('Path', '', [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
        Kind  = $Key.GetValueKind('Path')
    }
}

function Remove-UserPathEntry {
    # Removes the given directories from the PATH of the current user (HKCU\Environment). The
    # package adds its bin folder there (Install-ChocolateyPath, default PathType User). A
    # stale entry would let anyone who can recreate the folder place programs that are found
    # through PATH. New terminals see the change; running programs keep their old PATH.
    param([string[]]$Directories)
    $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Environment', $true)
    try {
        $path = Read-PathValue -Key $key
        if (-not $path) { return }
        $newValue = Remove-PathListEntry -Value $path.Value -Directories $Directories
        if ($newValue -cne $path.Value) {
            $key.SetValue('Path', $newValue, $path.Kind)
            Write-Host 'Removed MySQL entries from the user PATH; open a new terminal to use the new PATH.'
        }
    } finally {
        if ($key) { $key.Dispose() }
    }
}

function Write-MachinePathWarning {
    # Machine-wide PATH entries are not changed by this script; MySQL entries there are
    # reported.
    $key = [Microsoft.Win32.Registry]::LocalMachine.OpenSubKey('SYSTEM\CurrentControlSet\Control\Session Manager\Environment', $false)
    try {
        $path = Read-PathValue -Key $key
        $entries = @(if ($path) { Get-MySqlPathEntries -Value $path.Value })
        if ($entries.Count -gt 0) {
            Write-Warning "The machine PATH contains MySQL folders: $($entries -join ', '). Remove them if they are not used by another program (System Properties, Environment Variables)."
        }
    } finally {
        if ($key) { $key.Dispose() }
    }
}

function Get-OldPackageRoot {
    return Join-Path (Get-ChocolateyToolsRoot) $MySqlPackage
}

function Get-InstallRootViolations {
    # Allowlist check of C:\LEG-MySQL (the same rules as after the installation); returns the
    # violations instead of throwing. Unreadable items count as violations.
    try {
        $violations = @(Get-SecurityViolations -Path $InstallRoot) +
            @(if (Test-Path -LiteralPath $ServerRoot) { Get-SecurityViolations -Path $ServerRoot -AllowUsersReadExecute -Recurse }) +
            @(if (Test-Path -LiteralPath $DataRoot) { Get-SecurityViolations -Path $DataRoot -Recurse })
        $unexpected = @(Get-ChildItem -LiteralPath $InstallRoot -Force | Where-Object { $_.Name -notin @('server', 'data') })
        if ($unexpected.Count -gt 0) { $violations += "unexpected entries in ${InstallRoot}: $($unexpected.Name -join ', ')" }
        return $violations
    } catch {
        return @("$InstallRoot could not be checked: $($_.Exception.Message)")
    }
}

function Get-FailedInstallation {
    # Collects what an earlier run left behind, without changing anything.
    $service = Get-CimInstance Win32_Service -Filter "Name='$ServiceName'"
    $ownershipProblem = $null
    $serviceRunning = $false
    $serviceIsLegacy = $false
    if ($service) {
        $executable = Resolve-ServiceExecutablePath -PathName $service.PathName
        $ownershipProblem = Get-ServiceOwnershipProblem -PathName $service.PathName -Executable $executable -ToolsRoot (Get-ChocolateyToolsRoot)
        $serviceRunning = "$($service.State)" -ne 'Stopped'
        $serviceIsLegacy = [bool]($executable -and (Test-IsPathUnder -Path $executable -Root (Get-OldPackageRoot)))
    }
    $newRootExists = Test-Path -LiteralPath $InstallRoot
    return [pscustomobject]@{
        ServiceExists          = [bool]$service
        ServiceRunning         = $serviceRunning
        ServiceIsLegacy        = $serviceIsLegacy
        OwnershipProblem       = $ownershipProblem
        PackageVersion         = Get-InstalledChocolateyPackageVersion -Name $MySqlPackage
        NewRootExists          = $newRootExists
        NewRootViolations      = @(if ($newRootExists) { Get-InstallRootViolations })
        OldPackageRootExists   = Test-Path -LiteralPath (Get-OldPackageRoot)
        OldDataDirectoryExists = Test-Path -LiteralPath $OldDataDirectory
    }
}

function Test-HasLeftovers {
    param($Installation)
    return $Installation.ServiceExists -or $Installation.PackageVersion -or $Installation.NewRootExists -or
        $Installation.OldPackageRootExists -or $Installation.OldDataDirectoryExists
}

function Test-HasLegacyLayout {
    param($Installation)
    return $Installation.ServiceIsLegacy -or $Installation.OldPackageRootExists -or $Installation.OldDataDirectoryExists
}

function Get-RemovalProblems {
    # Returns the reasons why the installation cannot be removed automatically. Only a
    # C:\LEG-MySQL that passes the allowlist (created protected by this script) is removed
    # automatically; the earlier layout and anything else is removed manually (D-027).
    param($Installation)
    $problems = @()
    if ($Installation.OwnershipProblem) {
        $problems += "the service '$ServiceName' does not belong to a local installation of the Chocolatey package '$MySqlPackage': $($Installation.OwnershipProblem)"
    }
    if (Test-HasLegacyLayout -Installation $Installation) {
        $problems += "an installation of the earlier layout ($(Get-OldPackageRoot), $OldDataDirectory) exists; it is removed manually"
    }
    if ($Installation.NewRootExists -and @($Installation.NewRootViolations).Count -gt 0) {
        $problems += "$InstallRoot was not created by this script or was changed: $(@($Installation.NewRootViolations) -join '; ')"
    }
    return $problems
}

function Get-LegacyCheckPaths {
    # Folders whose type is checked before deletion commands are printed.
    param($Installation)
    $paths = @((Get-ChocolateyToolsRoot), (Get-OldPackageRoot), $OldDataParent, $OldDataDirectory)
    if ($Installation -and $Installation.NewRootExists) { $paths += $InstallRoot }
    return $paths
}

function Get-ReparsePaths {
    # Returns the existing paths among $Paths that are reparse points (symbolic links,
    # junctions, mount points).
    param([string[]]$Paths)
    return @($Paths | Where-Object { $_ -and (Test-IsReparsePoint -Path $_) })
}

function Get-LegacyRemovalCommands {
    # Commands for the manual removal of the earlier layout, for an elevated PowerShell.
    # Only C:\ProgramData\MySQL\data is deleted recursively (Oracle's installer uses other
    # folders below C:\ProgramData\MySQL); the parent only if it is then empty (non-recursive
    # Directory.Delete fails otherwise). [IO.Directory]::Delete with recursive=true "does not
    # recurse through the reparse point" (Microsoft Learn, "Directory.Delete Method"). The
    # service commands are included only for the service of the earlier layout. Returns no
    # deletion commands if one of the folders is a reparse point.
    param($Installation, [string[]]$ReparsePaths)
    if (@($ReparsePaths).Count -gt 0) { return @() }
    $checkPaths = (@(Get-LegacyCheckPaths -Installation $Installation) | ForEach-Object { "'$_'" }) -join ', '
    $commands = @(
        "# 1. Must print nothing; otherwise stop and ask for help:",
        "Get-Item -LiteralPath $checkPaths -Force -ErrorAction SilentlyContinue | Where-Object { `$_.Attributes -band [IO.FileAttributes]::ReparsePoint }"
    )
    $serviceIsOurs = $Installation.ServiceIsLegacy -and -not $Installation.OwnershipProblem
    if ($serviceIsOurs) {
        $commands += @(
            "Stop-Service -Name $ServiceName -Force",
            "sc.exe config $ServiceName start= disabled"
        )
    }
    if ($Installation.PackageVersion -and ($serviceIsOurs -or -not $Installation.ServiceExists)) {
        $commands += "choco uninstall $MySqlPackage -y"
    }
    if ($serviceIsOurs) { $commands += "sc.exe delete $ServiceName" }
    if ($Installation.OldPackageRootExists) { $commands += "[IO.Directory]::Delete('$(Get-OldPackageRoot)', `$true)" }
    if ($Installation.OldDataDirectoryExists) {
        $commands += @(
            "[IO.Directory]::Delete('$OldDataDirectory', `$true)",
            "# Optional; fails if the folder is not empty (other MySQL installations):",
            "[IO.Directory]::Delete('$OldDataParent')"
        )
    }
    return $commands
}

function Write-ManualRemovalInstructions {
    param($Installation)
    Write-Host ''
    Write-Host 'Nothing was changed. This installation is not removed automatically (docs/INSTALLATION.md, "Recovery after a failed setup").' -ForegroundColor Red
    if ($Installation -and $Installation.ServiceExists -and -not ($Installation.ServiceIsLegacy -and -not $Installation.OwnershipProblem)) {
        Write-Warning "The service '$ServiceName' belongs to another MySQL installation. Do not stop or delete it for this project; resolve the name conflict manually."
    }
    if ($Installation -and (Test-HasLegacyLayout -Installation $Installation)) {
        $reparsePaths = @(Get-ReparsePaths -Paths (Get-LegacyCheckPaths -Installation $Installation))
        if ($reparsePaths.Count -gt 0) {
            Write-Warning "These folders are symbolic links, junctions or mount points: $($reparsePaths -join ', '). Do not delete anything; check the folders first."
            return
        }
        Write-Host 'Remove it in a PowerShell window opened as administrator, while no other user is signed in to this computer:' -ForegroundColor Red
        foreach ($command in Get-LegacyRemovalCommands -Installation $Installation -ReparsePaths $reparsePaths) { Write-Host "  $command" }
        Write-Host 'Commands for parts that do not exist report an error; that is expected. Then run this script again.' -ForegroundColor Red
    } else {
        $reparsePaths = @(Get-ReparsePaths -Paths (Get-LegacyCheckPaths -Installation $Installation))
        if ($reparsePaths.Count -gt 0) {
            Write-Warning "These folders are symbolic links, junctions or mount points: $($reparsePaths -join ', '). Do not delete anything; check the folders first."
        }
        Write-Host 'Check the installation and remove it manually if it is no longer needed.' -ForegroundColor Red
    }
}

function Confirm-FailedInstallationRemoval {
    # Checks that the installation may be removed automatically, lists it and asks for the
    # typed confirmation before anything is changed. Throws otherwise.
    param($Installation)
    $problems = @(Get-RemovalProblems -Installation $Installation)
    if ($problems.Count -gt 0) {
        Write-ManualRemovalInstructions -Installation $Installation
        throw "The installation cannot be removed automatically: $($problems -join '; ')."
    }
    Write-Host ''
    Write-Host 'The following local MySQL installation will be removed:' -ForegroundColor Yellow
    if ($Installation.ServiceExists) { Write-Host "  - Windows service '$ServiceName' (stopped and disabled first, then deleted)" }
    if ($Installation.PackageVersion) { Write-Host "  - Chocolatey package '$MySqlPackage' $($Installation.PackageVersion) (choco uninstall)" }
    if ($Installation.NewRootExists) { Write-Host "  - $InstallRoot with programs, option file and ALL local MySQL databases" -ForegroundColor Yellow }
    Write-Host '  - MySQL entries in the PATH of the current user'
    Write-Host 'This cannot be undone.' -ForegroundColor Yellow
    $answer = Read-Host 'Type DELETE to remove all items listed above'
    if ($answer -cne 'DELETE') {
        throw 'Not confirmed. Nothing was changed.'
    }
}

function Remove-FailedInstallation {
    # Removes the installation after Confirm-FailedInstallationRemoval. Every step checks the
    # current state, so that the removal can be repeated after an interruption.
    param($Installation)
    # Re-check immediately before changing anything.
    $current = Get-FailedInstallation
    $problems = @(Get-RemovalProblems -Installation $current)
    if ($problems.Count -gt 0) {
        Write-ManualRemovalInstructions -Installation $current
        throw "The installation changed since the confirmation: $($problems -join '; ')."
    }
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
    Remove-UserPathEntry -Directories @((Join-Path $ServerCurrent 'bin'), $LegacyPathEntry)
    if (Test-Path -LiteralPath $InstallRoot) {
        # The tree was created protected and passes the allowlist (checked again here), so
        # ordinary users cannot have changed it. [IO.Directory]::Delete with recursive=true
        # "does not recurse through the reparse point" (Microsoft Learn, "Directory.Delete Method").
        $violations = @(Get-InstallRootViolations)
        if ($violations.Count -gt 0) {
            throw "$InstallRoot changed since the confirmation; it was not deleted: $($violations -join '; ')"
        }
        [IO.Directory]::Delete($InstallRoot, $true)
        if (Test-Path -LiteralPath $InstallRoot) { throw "$InstallRoot could not be deleted." }
        Write-Host "$InstallRoot was deleted."
    }
}

function Write-RecoveryInstructions {
    Write-Host ''
    Write-Host 'Recovery (docs/INSTALLATION.md, "Recovery after a failed setup"): fix the cause shown above and run as administrator:'
    Write-Host "  $RetryCommand"
    Write-Host 'It lists what it removes and asks for a typed confirmation before it deletes anything.'
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
$presentOptionFiles = @(Get-PresentOptionFiles -Paths ($GlobalOptionFiles + @($LoginPathFile)))
if ($presentOptionFiles.Count -gt 0) {
    throw "MySQL would also read these option files: $($presentOptionFiles -join ', '). Remove or rename them first."
}

Clear-MySqlClientEnvironment

# The PATH cleanup and the user environment variables apply to the account that runs this
# script (HKCU). If an administrator account other than the signed-in user is used for the
# elevation, they do not reach the signed-in user.
$signedInUser = Get-SignedInUser
$currentUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
if ($signedInUser -and $signedInUser -ine $currentUser) {
    Write-Warning "This script runs as '$currentUser', but '$signedInUser' is signed in. PATH changes and the environment variables apply to '$currentUser' only. Run the script elevated with the signed-in account (Run as administrator)."
}

# Stale PATH entry of the earlier layout: removed from the user PATH; MySQL entries in the
# machine PATH are only reported.
Remove-UserPathEntry -Directories @($LegacyPathEntry, (Join-Path (Get-OldPackageRoot) 'current\bin'))
Write-MachinePathWarning

$failedInstallation = Get-FailedInstallation
if (Test-HasLeftovers -Installation $failedInstallation) {
    if (-not $RemoveFailedInstallation) {
        if ($failedInstallation.ServiceRunning) {
            Write-Warning "The service '$ServiceName' is running. Stop and disable it as administrator: sc.exe stop $ServiceName; sc.exe config $ServiceName start= disabled"
        }
        if (@(Get-RemovalProblems -Installation $failedInstallation).Count -gt 0) {
            Write-ManualRemovalInstructions -Installation $failedInstallation
            throw 'A local MySQL installation exists that this script does not remove automatically.'
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
    # 4a. Create C:\LEG-MySQL with restricted permissions and write my.ini before the
    # installation, so that the server is initialised and first started with it.

    New-RestrictedDirectory -Path $InstallRoot
    New-RestrictedDirectory -Path $ServerRoot -UsersReadExecute
    New-RestrictedDirectory -Path $DataRoot
    Assert-InstallationSecurity
    # The package keeps an existing my.ini. The folders and the file are created with their
    # permissions and owner set at creation.
    New-RestrictedDirectory -Path $ServerPackageRoot -UsersReadExecute
    New-RestrictedDirectory -Path $ServerCurrent -UsersReadExecute
    $optionFileContent = Get-ServerOptionFileContent
    New-RestrictedFile -Path $ServerOptionFile -Content $optionFileContent -UsersReadExecute
    Set-OwnerAdministrators -Path $InstallRoot
    Assert-InstallationSecurity

    # 4b. Install MySQL below C:\LEG-MySQL (package parameters /installLocation and
    # /dataLocation, https://community.chocolatey.org/packages/mysql).

    if ([IO.File]::ReadAllText($ServerOptionFile) -cne $optionFileContent) {
        throw "$ServerOptionFile was changed before the installation."
    }
    $packageParameters = "/installLocation:$ServerRoot /dataLocation:$DataRoot"
    choco install $MySqlPackage --version $MySqlVersion -y --no-progress --params $packageParameters
    if ($LASTEXITCODE -ne 0) { throw "Chocolatey exited with code $LASTEXITCODE while installing MySQL." }
    # The server must not start automatically before the setup has succeeded.
    Set-Service -Name $ServiceName -StartupType Manual

    $paths = Get-ServerPaths
    if (-not (Test-IsSamePath -First $paths.MyIni -Second $ServerOptionFile)) {
        throw "The service uses the option file $($paths.MyIni), not $ServerOptionFile."
    }
    if ([IO.File]::ReadAllText($ServerOptionFile) -cne $optionFileContent) {
        throw "$ServerOptionFile was changed by the installation."
    }
    if (-not (Test-IsFile -Path (Join-Path $DataDirectory 'mysql.ibd'))) {
        throw "The data directory $DataDirectory was not initialised."
    }
    # Stop the server while the owners are set and the permissions are checked, so that it
    # creates no files in the meantime; then the allowlist check (owner, permission entries,
    # no reparse points).
    Stop-Service -Name $ServiceName -Force
    (Get-Service -Name $ServiceName).WaitForStatus('Stopped', [TimeSpan]::FromSeconds(60))
    Set-OwnerAdministrators -Path $InstallRoot
    Assert-InstallationSecurity
    # Option files mysqld would read in addition to my.ini: BASEDIR\my.cnf, and
    # DATADIR\mysqld-auto.cnf, which mysqld reads even with --defaults-file (MySQL Reference
    # Manual, "Using Option Files"); neither may exist after the installation.
    foreach ($extra in @((Join-Path $ServerCurrent 'my.cnf'), (Join-Path $DataDirectory 'mysqld-auto.cnf'))) {
        if (Test-Path -LiteralPath $extra) { throw "Unexpected option file $extra." }
    }
    Write-Host "Permissions below $InstallRoot verified (Administrators and SYSTEM; Users read and execute on server only)."
    Start-Service -Name $ServiceName

    # 4c. Verify the listening sockets: loopback only, X Plugin off.

    Assert-LoopbackOnly
    Write-Host "MySQL listens only on 127.0.0.1:$MySqlPort; X Plugin is disabled."

    # 4d. Set the root password and verify a login with it

    Invoke-MySql -Client $paths.Client -Sql "ALTER USER 'root'@'localhost' IDENTIFIED BY '$rootPassword';"
    Invoke-MySql -Client $paths.Client -Sql 'SELECT 1;' -RootPassword $rootPassword | Out-Null
    Assert-ServerConfiguration -Client $paths.Client -RootPassword $rootPassword
    Assert-ReservedAccountsOnly -Client $paths.Client -RootPassword $rootPassword

    # 4e. Create the database and the application account
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

    # 4f. Store the local configuration as user environment variables

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
        Write-RecoveryInstructions
    }
}

Write-Host ''
Write-Host "MySQL $MySqlVersion is installed in $InstallRoot, listens only on 127.0.0.1, X Plugin is disabled, the root password is set and the service starts automatically."
Write-Host "Database '$DatabaseName' and account '$AppUser'@'127.0.0.1' were created."
Write-Host "User environment variables set: $($settings.Keys -join ', ')"
Write-Host 'Restart VS Code so that new terminals and Claude Code receive the variables.'
Write-Host 'Never print the environment (e.g. Get-ChildItem env:); it contains DB_PASSWORD and DJANGO_SECRET_KEY.'
