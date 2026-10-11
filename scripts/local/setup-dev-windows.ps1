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

.PARAMETER MySqlVersion
    Chocolatey package version of MySQL to install.

.PARAMETER PythonVersion
    Chocolatey package version of the python314 package to install.
#>
[CmdletBinding()]
param(
    [string]$MySqlVersion = '9.7.2',
    [string]$PythonVersion = '3.14.8'
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

function Get-ServerPaths {
    $service = Get-CimInstance Win32_Service -Filter "Name='$ServiceName'"
    if (-not $service) { throw "Service '$ServiceName' was not created by the installation." }
    $pathName = $service.PathName
    $mysqld = ([regex]::Match($pathName, '^\s*"([^"]+)"|^\s*(\S+)')).Groups |
        Select-Object -Skip 1 | Where-Object { $_.Success } | Select-Object -First 1 -ExpandProperty Value
    if (-not $mysqld -or -not (Test-Path -LiteralPath $mysqld)) { throw "mysqld not found in the service command line." }
    $binDir = Split-Path -Parent $mysqld
    $client = Join-Path $binDir 'mysql.exe'
    if (-not (Test-Path -LiteralPath $client)) { throw "mysql client not found at $client" }
    # --defaults-file="C:\path with spaces\my.ini" or --defaults-file=C:\path\my.ini
    $match = [regex]::Match($pathName, '--defaults-file=(?:"([^"]+)"|(\S+))')
    if ($match.Success) {
        $myIni = if ($match.Groups[1].Success) { $match.Groups[1].Value } else { $match.Groups[2].Value }
    } else {
        $myIni = Join-Path (Split-Path -Parent $binDir) 'my.ini'
    }
    if (-not (Test-Path -LiteralPath $myIni)) { throw "Option file not found at $myIni." }
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
    $match = [regex]::Match($text, '(?m)^\s*datadir\s*=\s*"?([^"\r\n]+?)"?\s*$')
    if ($match.Success) { return $match.Groups[1].Value }
    return $null
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
}

# 0. Preconditions

if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
    throw "A Windows service named '$ServiceName' already exists. This script only sets up a new installation; see docs/INSTALLATION.md for recovery."
}
if (-not (Get-Command choco -ErrorAction SilentlyContinue)) {
    throw 'Chocolatey (choco) was not found.'
}

# 1. Ask for the root password before anything is installed

$rootPassword = Read-RootPassword

# 2. Install Python
# Newest Python version supported by the newest Django release (D-015: the newest possible
# version is used for development; the minimum versions are only declared and tested in CI).

choco install python314 --version $PythonVersion -y --no-progress
if ($LASTEXITCODE -ne 0) { throw "Chocolatey exited with code $LASTEXITCODE while installing Python." }

# 3. Install and configure MySQL; on any failure, leave the server stopped and disabled

$paths = $null
$completed = $false
try {
    choco install mysql --version $MySqlVersion -y --no-progress
    if ($LASTEXITCODE -ne 0) { throw "Chocolatey exited with code $LASTEXITCODE while installing MySQL." }
    # The server must not start automatically before it is hardened.
    Set-Service -Name $ServiceName -StartupType Manual

    $paths = Get-ServerPaths
    Write-Host "Server option file: $($paths.MyIni)"

    # 3a. Loopback only, X Plugin off; verify the listening sockets

    Set-ServerNetworkOptions -Path $paths.MyIni
    Restart-Service -Name $ServiceName
    Assert-LoopbackOnly
    Write-Host "MySQL listens only on 127.0.0.1:$MySqlPort; X Plugin is disabled."

    # 3b. Set the root password and verify a login with it

    Invoke-MySql -Client $paths.Client -Sql "ALTER USER 'root'@'localhost' IDENTIFIED BY '$rootPassword';"
    Invoke-MySql -Client $paths.Client -Sql 'SELECT 1;' -RootPassword $rootPassword | Out-Null
    Assert-ServerConfiguration -Client $paths.Client -RootPassword $rootPassword

    # 3c. Create the database and the application account
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

    # 4. Store the local configuration as user environment variables

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
