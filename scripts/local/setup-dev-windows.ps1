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

function Test-ContainsOnly {
    # True if every entry directly below $Path matches one of the patterns (case-insensitive,
    # -like) and none is a reparse point.
    param([string]$Path, [string[]]$Patterns)
    foreach ($entry in @(Get-ChildItem -LiteralPath $Path -Force)) {
        if ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) { return $false }
        if (-not ($Patterns | Where-Object { $entry.Name -like $_ })) { return $false }
    }
    return $true
}

function Remove-UserPathEntry {
    # Removes the given directories from the PATH of the current user. The package adds its
    # bin directory there (Install-ChocolateyPath, default PathType User). A stale entry would
    # let anyone who can recreate the folder place programs that are found through PATH.
    param([string[]]$Directories)
    $value = [Environment]::GetEnvironmentVariable('Path', 'User')
    if (-not $value) { return }
    $entries = @($value.Split(';') | Where-Object { $_ -ne '' })
    $kept = @($entries | Where-Object {
        $entry = $_
        -not ($Directories | Where-Object { Test-IsSamePath -First $entry -Second $_ })
    })
    if ($kept.Count -ne $entries.Count) {
        [Environment]::SetEnvironmentVariable('Path', ($kept -join ';'), 'User')
        Write-Host "Removed $($entries.Count - $kept.Count) MySQL entry or entries from the user PATH."
    }
}

function Get-OldPackageRoot {
    return Join-Path (Get-ChocolateyToolsRoot) $MySqlPackage
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
    $oldPackageRoot = Get-OldPackageRoot
    $oldOptionFile = Join-Path $oldPackageRoot 'current\my.ini'
    return [pscustomobject]@{
        ServiceExists              = [bool]$service
        ServiceRunning             = $serviceRunning
        OwnershipProblem           = $ownershipProblem
        PackageVersion             = Get-InstalledChocolateyPackageVersion -Name $MySqlPackage
        NewRootExists              = Test-Path -LiteralPath $InstallRoot
        OldPackageRootExists       = Test-Path -LiteralPath $oldPackageRoot
        OldConfiguredDataDirectory = Get-DataDirectory -MyIni $oldOptionFile
        OldDataDirectoryExists     = Test-Path -LiteralPath $OldDataDirectory
    }
}

function Test-HasLeftovers {
    param($Installation)
    return $Installation.ServiceExists -or $Installation.PackageVersion -or $Installation.NewRootExists -or
        $Installation.OldPackageRootExists -or $Installation.OldDataDirectoryExists
}

function Get-RemovalProblems {
    # Checks everything that would be deleted; returns descriptions of problems. Called before
    # the confirmation and again immediately before each deletion.
    param($Installation)
    $problems = @()
    if ($Installation.OwnershipProblem) {
        $problems += "the service '$ServiceName' does not look like a local installation of the Chocolatey package '$MySqlPackage': $($Installation.OwnershipProblem)"
    }
    if (Test-Path -LiteralPath $InstallRoot) {
        if (Test-IsReparsePoint -Path $InstallRoot) { $problems += "$InstallRoot is a reparse point" }
        elseif (-not (Test-ContainsOnly -Path $InstallRoot -Patterns @('server', 'data'))) {
            $problems += "$InstallRoot contains entries other than server and data, or reparse points"
        }
    }
    $oldPackageRoot = Get-OldPackageRoot
    if (Test-Path -LiteralPath $oldPackageRoot) {
        if (Test-IsReparsePoint -Path $oldPackageRoot) { $problems += "$oldPackageRoot is a reparse point" }
        elseif (-not (Test-ContainsOnly -Path $oldPackageRoot -Patterns @('current', 'mysql-*-winx64'))) {
            $problems += "$oldPackageRoot contains entries the package did not create (only 'current' and 'mysql-<version>-winx64' are removed automatically)"
        }
    }
    foreach ($path in @($OldDataParent, $OldDataDirectory)) {
        if (Test-IsReparsePoint -Path $path) { $problems += "$path is a reparse point" }
    }
    if ((Test-Path -LiteralPath $OldDataDirectory) -and -not (Test-IsMySqlDataDirectory -Path $OldDataDirectory)) {
        $problems += "$OldDataDirectory does not look like a MySQL data directory (no mysql.ibd and mysql folder)"
    }
    return $problems
}

function Write-ManualRemovalInstructions {
    Write-Host ''
    Write-Host 'Nothing was changed. Check the installation and remove it manually if it is no longer needed' -ForegroundColor Red
    Write-Host '(docs/INSTALLATION.md, "Recovery after a failed setup", manual steps).' -ForegroundColor Red
}

function Confirm-FailedInstallationRemoval {
    # Checks that everything to be removed belongs to a package installation, lists it and
    # asks for the typed confirmation before anything is changed. Throws otherwise.
    param($Installation)
    $problems = @(Get-RemovalProblems -Installation $Installation)
    if ($problems.Count -gt 0) {
        Write-ManualRemovalInstructions
        throw "The installation cannot be removed automatically: $($problems -join '; ')."
    }
    if ($Installation.OldConfiguredDataDirectory -and
        -not (Test-IsSamePath -First $Installation.OldConfiguredDataDirectory -Second $OldDataDirectory)) {
        Write-Warning "The old my.ini names the data directory '$($Installation.OldConfiguredDataDirectory)'. It is not the package default and is NOT deleted; delete it yourself if it is no longer needed."
    }
    Write-Host ''
    Write-Host 'The following local MySQL installation will be removed:' -ForegroundColor Yellow
    if ($Installation.ServiceExists) { Write-Host "  - Windows service '$ServiceName' (stopped and disabled first, then deleted)" }
    if ($Installation.PackageVersion) { Write-Host "  - Chocolatey package '$MySqlPackage' $($Installation.PackageVersion) (choco uninstall)" }
    if ($Installation.NewRootExists) { Write-Host "  - $InstallRoot with programs, option file and ALL local MySQL databases" -ForegroundColor Yellow }
    if ($Installation.OldPackageRootExists) { Write-Host "  - $(Get-OldPackageRoot) (programs and option file of the earlier layout)" }
    if ($Installation.OldDataDirectoryExists) { Write-Host "  - $OldDataDirectory with ALL local MySQL databases of the earlier layout" -ForegroundColor Yellow }
    Write-Host '  - MySQL entries in the PATH of the current user'
    Write-Host 'This cannot be undone.' -ForegroundColor Yellow
    $answer = Read-Host 'Type DELETE to remove all items listed above'
    if ($answer -cne 'DELETE') {
        throw 'Not confirmed. Nothing was changed.'
    }
}

function Remove-DirectoryTree {
    # Deletes a directory tree after re-checking it. [IO.Directory]::Delete with recursive=true
    # "does not recurse through the reparse point" (Microsoft Learn, "Directory.Delete Method").
    param([string]$Path, [string[]]$Patterns)
    if (-not (Test-Path -LiteralPath $Path)) { return }
    if (Test-IsReparsePoint -Path $Path) { throw "$Path became a reparse point; it was not deleted." }
    if ($Patterns -and -not (Test-ContainsOnly -Path $Path -Patterns $Patterns)) {
        throw "$Path contains unexpected entries; it was not deleted."
    }
    [IO.Directory]::Delete($Path, $true)
    if (Test-Path -LiteralPath $Path) { throw "$Path could not be deleted." }
    Write-Host "$Path was deleted."
}

function Remove-FailedInstallation {
    # Removes the installation after Confirm-FailedInstallationRemoval. Every step checks the
    # current state, so that the removal can be repeated after an interruption.
    param($Installation)
    # Re-check immediately before changing anything.
    $current = Get-FailedInstallation
    $problems = @(Get-RemovalProblems -Installation $current)
    if ($problems.Count -gt 0) {
        Write-ManualRemovalInstructions
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
    $oldPackageRoot = Get-OldPackageRoot
    Remove-UserPathEntry -Directories @((Join-Path $ServerCurrent 'bin'), (Join-Path $oldPackageRoot 'current\bin'))
    Remove-DirectoryTree -Path $InstallRoot -Patterns @('server', 'data')
    if ((Test-Path -LiteralPath $OldDataDirectory)) {
        if ((Test-IsReparsePoint -Path $OldDataParent) -or -not (Test-IsMySqlDataDirectory -Path $OldDataDirectory)) {
            throw "$OldDataDirectory changed since the confirmation; it was not deleted."
        }
        Remove-DirectoryTree -Path $OldDataDirectory
    }
    if ((Test-Path -LiteralPath $OldDataParent) -and @(Get-ChildItem -LiteralPath $OldDataParent -Force).Count -eq 0) {
        [IO.Directory]::Delete($OldDataParent, $false)
    }
    Remove-DirectoryTree -Path $oldPackageRoot -Patterns @('current', 'mysql-*-winx64')
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
$globalOptionFiles = @($GlobalOptionFiles | Where-Object { Test-Path -LiteralPath $_ })
if ($globalOptionFiles.Count -gt 0) {
    throw "MySQL would also read these option files: $($globalOptionFiles -join ', '). Remove or rename them first."
}

$failedInstallation = Get-FailedInstallation
if (Test-HasLeftovers -Installation $failedInstallation) {
    if (-not $RemoveFailedInstallation) {
        if ($failedInstallation.ServiceRunning) {
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
    # 4a. Create C:\LEG-MySQL with restricted permissions and write my.ini before the
    # installation, so that the server is initialised and first started with it.

    New-RestrictedDirectory -Path $InstallRoot
    New-RestrictedDirectory -Path $ServerRoot -UsersReadExecute
    New-RestrictedDirectory -Path $DataRoot
    Assert-InstallationSecurity
    # Created below server, inheriting its permissions; the package keeps an existing my.ini.
    New-Item -ItemType Directory -Path $ServerCurrent | Out-Null
    $optionFileContent = Get-ServerOptionFileContent
    [IO.File]::WriteAllText($ServerOptionFile, $optionFileContent, (New-Object Text.ASCIIEncoding))
    Set-OwnerAdministrators -Path $InstallRoot
    Assert-InstallationSecurity

    # 4b. Install MySQL below C:\LEG-MySQL (package parameters /installLocation and
    # /dataLocation, https://community.chocolatey.org/packages/mysql).

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
    # Owner Administrators for everything the package and the initialisation created, then
    # the allowlist check (owner, permission entries, no reparse points).
    Set-OwnerAdministrators -Path $InstallRoot
    Assert-InstallationSecurity
    Write-Host "Permissions below $InstallRoot verified (Administrators and SYSTEM; Users read and execute on server only)."

    # 4c. Verify the listening sockets: loopback only, X Plugin off.

    Assert-LoopbackOnly
    Write-Host "MySQL listens only on 127.0.0.1:$MySqlPort; X Plugin is disabled."

    # 4d. Set the root password and verify a login with it

    Invoke-MySql -Client $paths.Client -Sql "ALTER USER 'root'@'localhost' IDENTIFIED BY '$rootPassword';"
    Invoke-MySql -Client $paths.Client -Sql 'SELECT 1;' -RootPassword $rootPassword | Out-Null
    Assert-ServerConfiguration -Client $paths.Client -RootPassword $rootPassword

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
