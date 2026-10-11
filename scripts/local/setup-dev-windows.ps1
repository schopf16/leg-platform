#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Sets up MySQL and Python for local LEG-Platform development on Windows (D-027).

.DESCRIPTION
    For local development only. Never use this script for stage or production.

    1. Installs MySQL Community Server (default version: 9.7.2, latest LTS series) and
       Python (default version: 3.14.8) with Chocolatey.
    2. Binds the server to 127.0.0.1, because the Chocolatey package sets no bind address.
    3. Sets a password for 'root'@'localhost', because the package initialises the data
       directory with --initialize-insecure (root without a password).
    4. Creates the database 'leg_platform' and the account 'leg_platform'@'127.0.0.1' with
       privileges only on 'leg_platform' and on Django's test database 'test_leg_platform'.
    5. Stores the local configuration as user environment variables (DB_NAME, DB_USER,
       DB_PASSWORD, DB_HOST, DB_PORT, DJANGO_SECRET_KEY, APP_ENV=local). Generated values are
       never printed.

    The root password is entered interactively and is not stored by this script; keep it in
    your password manager. Passwords are passed to the mysql client via standard input, never
    on the command line.

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

# 1. Install MySQL

if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
    throw "A Windows service named '$ServiceName' already exists. This script only sets up a new installation."
}
choco install mysql --version $MySqlVersion -y --no-progress
if ($LASTEXITCODE -ne 0) { throw "Chocolatey exited with code $LASTEXITCODE" }

# Newest Python version supported by the newest Django release (D-015: the newest possible
# version is used for development; the minimum versions are only declared and tested in CI).
choco install python314 --version $PythonVersion -y --no-progress
if ($LASTEXITCODE -ne 0) { throw "Chocolatey exited with code $LASTEXITCODE" }

# 2. Locate the server and its option file

$service = Get-CimInstance Win32_Service -Filter "Name='$ServiceName'"
if (-not $service) { throw "Service '$ServiceName' was not created by the installation." }
$pathName = $service.PathName
$mysqld = ([regex]::Match($pathName, '^"?([^"]+mysqld\.exe)"?')).Groups[1].Value
$optionFileMatch = [regex]::Match($pathName, '--defaults-file="?([^"\s]+)"?')
$binDir = Split-Path -Parent $mysqld
$client = Join-Path $binDir 'mysql.exe'
if (-not (Test-Path $client)) { throw "mysql client not found at $client" }

if ($optionFileMatch.Success) {
    $myIni = $optionFileMatch.Groups[1].Value
} else {
    $myIni = Join-Path (Split-Path -Parent $binDir) 'my.ini'
}
if (-not (Test-Path $myIni)) { throw "Option file not found at $myIni; bind address not set." }
Write-Host "Server option file: $myIni"

# 3. Bind the server to the loopback interface

$ini = Get-Content -LiteralPath $myIni
if ($ini -match '^\s*bind-address\s*=') {
    $ini = $ini -replace '^\s*bind-address\s*=.*$', 'bind-address=127.0.0.1'
} elseif ($ini -match '^\s*\[mysqld\]\s*$') {
    $ini = $ini -replace '^\s*\[mysqld\]\s*$', "[mysqld]`r`nbind-address=127.0.0.1"
} else {
    $ini += @('', '[mysqld]', 'bind-address=127.0.0.1')
}
Set-Content -LiteralPath $myIni -Value $ini -Encoding ascii
Restart-Service -Name $ServiceName

# 4. Set the root password

$rootSecure = Read-Host -AsSecureString 'New password for the local MySQL root account'
$rootConfirm = Read-Host -AsSecureString 'Repeat the password'
$rootPassword = ConvertFrom-SecureStringToPlain $rootSecure
if ($rootPassword -ne (ConvertFrom-SecureStringToPlain $rootConfirm)) { throw 'The passwords do not match.' }
if ($rootPassword.Length -lt 15) { throw 'Use at least 15 characters.' }
if ($rootPassword -match "['\\]") { throw 'The password must not contain single quotes or backslashes.' }

Invoke-MySql -Client $client -Sql "ALTER USER 'root'@'localhost' IDENTIFIED BY '$rootPassword';"

# 5. Create the databases and the application account

$appPassword = New-RandomSecret
$sql = @"
CREATE DATABASE IF NOT EXISTS ``$DatabaseName`` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
CREATE USER '$AppUser'@'127.0.0.1' IDENTIFIED BY '$appPassword';
GRANT ALL PRIVILEGES ON ``$DatabaseName``.* TO '$AppUser'@'127.0.0.1';
GRANT ALL PRIVILEGES ON ``$TestDatabaseName``.* TO '$AppUser'@'127.0.0.1';
"@
Invoke-MySql -Client $client -Sql $sql -RootPassword $rootPassword
$rootPassword = $null

# 6. Store the local configuration as user environment variables

$settings = [ordered]@{
    APP_ENV           = 'local'
    DB_NAME           = $DatabaseName
    DB_USER           = $AppUser
    DB_PASSWORD       = $appPassword
    DB_HOST           = '127.0.0.1'
    DB_PORT           = '3306'
    DJANGO_SECRET_KEY = (New-RandomSecret -Bytes 50)
}
foreach ($name in $settings.Keys) {
    [Environment]::SetEnvironmentVariable($name, $settings[$name], 'User')
}
$appPassword = $null

Write-Host ''
Write-Host "MySQL $MySqlVersion is installed, bound to 127.0.0.1 and the root password is set."
Write-Host "Database '$DatabaseName' and account '$AppUser'@'127.0.0.1' were created."
Write-Host "User environment variables set: $($settings.Keys -join ', ')"
Write-Host 'Restart VS Code so that new terminals and Claude Code receive the variables.'
