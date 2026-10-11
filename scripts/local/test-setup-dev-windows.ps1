<#
.SYNOPSIS
    Tests of the helper functions of setup-dev-windows.ps1 (D-027).

.DESCRIPTION
    Parses setup-dev-windows.ps1 with the PowerShell parser, fails on syntax errors, loads only
    its function definitions (the setup itself is never run) and tests the helpers that do not
    change the system. Needs no administrator rights, no Chocolatey and no MySQL; creates and
    removes a temporary directory. Runs with Windows PowerShell 5.1 and PowerShell 7.

    Usage: powershell -NoProfile -File scripts\local\test-setup-dev-windows.ps1
    Exit code 0 if all tests pass, 1 otherwise.
#>
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$scriptPath = Join-Path $PSScriptRoot 'setup-dev-windows.ps1'
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($scriptPath, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count -gt 0) {
    $parseErrors | ForEach-Object { Write-Host "PARSE ERROR: $($_.Message) (line $($_.Extent.StartLineNumber))" }
    exit 1
}

$functions = @($ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $false))
# Script-level constants the helpers use: only the assignments before the first function
# definition, so that no statement of the setup itself (e.g. reading the password) runs.
$firstFunctionOffset = ($functions | ForEach-Object { $_.Extent.StartOffset } | Measure-Object -Minimum).Minimum
$constants = @($ast.EndBlock.Statements | Where-Object {
    $_ -is [System.Management.Automation.Language.AssignmentStatementAst] -and $_.Extent.StartOffset -lt $firstFunctionOffset
})
foreach ($assignment in $constants) {
    . ([scriptblock]::Create($assignment.Extent.Text))
}
foreach ($function in $functions) {
    . ([scriptblock]::Create($function.Extent.Text))
}

$script:failures = 0
$script:passed = 0
function Assert-Equal {
    param($Actual, $Expected, [string]$Name)
    if ("$Actual" -ceq "$Expected" -and ($null -eq $Actual) -eq ($null -eq $Expected)) {
        $script:passed++
    } else {
        $script:failures++
        Write-Host "FAIL: $Name`n  expected: '$Expected'`n  actual:   '$Actual'"
    }
}

$root = Join-Path ([IO.Path]::GetTempPath()) ("leg-setup-test-" + [Guid]::NewGuid().ToString('N'))
try {
    $plainBin = Join-Path $root 'tools\mysql\current\bin'
    $spaceBin = Join-Path $root 'Program Files\My SQL\bin'
    New-Item -ItemType Directory -Path $plainBin, $spaceBin | Out-Null
    $plainExe = Join-Path $plainBin 'mysqld.exe'
    $spaceExe = Join-Path $spaceBin 'mysqld.exe'
    Set-Content -LiteralPath $plainExe -Value ''
    Set-Content -LiteralPath $spaceExe -Value ''
    $plainBase = Join-Path $plainBin 'mysqld'
    $spaceBase = Join-Path $spaceBin 'mysqld'

    # Resolve-ServiceExecutablePath
    $cases = @(
        @{ Name = 'unquoted, no extension (Chocolatey package)'; PathName = "$plainBase MySQL"; Expected = $plainExe },
        @{ Name = 'unquoted, no extension, no arguments'; PathName = $plainBase; Expected = $plainExe },
        @{ Name = 'unquoted, with .exe'; PathName = "$plainExe --defaults-file=C:\x\my.ini MySQL"; Expected = $plainExe },
        @{ Name = 'quoted, no extension'; PathName = "`"$plainBase`" MySQL"; Expected = $plainExe },
        @{ Name = 'quoted, with .exe'; PathName = "`"$plainExe`" MySQL"; Expected = $plainExe },
        @{ Name = 'quoted with spaces, no extension'; PathName = "`"$spaceBase`" --defaults-file=`"C:\a b\my.ini`" MySQL"; Expected = $spaceExe },
        @{ Name = 'quoted with spaces, with .exe'; PathName = "`"$spaceExe`" MySQL"; Expected = $spaceExe },
        @{ Name = 'unquoted with spaces, no extension'; PathName = "$spaceBase MySQL"; Expected = $spaceExe },
        @{ Name = 'unquoted with spaces, with .exe'; PathName = "$spaceExe MySQL"; Expected = $spaceExe },
        @{ Name = 'leading white space'; PathName = "  $plainBase MySQL"; Expected = $plainExe },
        @{ Name = 'missing file'; PathName = (Join-Path $root 'missing\mysqld MySQL'); Expected = $null },
        @{ Name = 'directory is not an executable'; PathName = "$plainBin MySQL"; Expected = $null },
        @{ Name = 'quoted missing file'; PathName = "`"$(Join-Path $root 'missing\mysqld')`" MySQL"; Expected = $null },
        @{ Name = 'unterminated quote'; PathName = "`"$plainBase MySQL"; Expected = $null },
        @{ Name = 'empty'; PathName = ''; Expected = $null }
    )
    foreach ($case in $cases) {
        Assert-Equal (Resolve-ServiceExecutablePath -PathName $case.PathName) $case.Expected "Resolve-ServiceExecutablePath: $($case.Name)"
    }

    # Get-OptionFilePath
    Assert-Equal (Get-OptionFilePath -PathName "$plainBase MySQL" -Executable $plainExe) (Join-Path $root 'tools\mysql\current\my.ini') 'Get-OptionFilePath: base directory'
    Assert-Equal (Get-OptionFilePath -PathName "`"$spaceExe`" --defaults-file=`"C:\a b\my.ini`" MySQL" -Executable $spaceExe) 'C:\a b\my.ini' 'Get-OptionFilePath: quoted --defaults-file'
    Assert-Equal (Get-OptionFilePath -PathName "$plainExe --defaults-file=C:\x\my.ini MySQL" -Executable $plainExe) 'C:\x\my.ini' 'Get-OptionFilePath: unquoted --defaults-file'
    Assert-Equal (Get-OptionFilePath -PathName 'x' -Executable $null) $null 'Get-OptionFilePath: no executable'

    # Get-DataDirectory: escaped backslashes as written by the Chocolatey package
    $myIni = Join-Path $root 'my.ini'
    [IO.File]::WriteAllText($myIni, "[mysqld]`r`nbasedir=C:\\tools\\mysql\\current`r`ndatadir=C:\\ProgramData\\MySQL\\data`r`nport=3306`r`n", (New-Object Text.UTF8Encoding($false)))
    Assert-Equal (Get-DataDirectory -MyIni $myIni) 'C:\ProgramData\MySQL\data' 'Get-DataDirectory: unescapes \\'
    [IO.File]::WriteAllText($myIni, "[mysqld]`ndatadir=`"D:/data`"`n[mysqld]`ndatadir=E:\\other`n", (New-Object Text.UTF8Encoding($false)))
    Assert-Equal (Get-DataDirectory -MyIni $myIni) 'E:\other' 'Get-DataDirectory: last instance wins'
    Assert-Equal (Get-DataDirectory -MyIni (Join-Path $root 'missing.ini')) $null 'Get-DataDirectory: missing file'

    # Test-IsSamePath
    Assert-Equal (Test-IsSamePath -First 'C:\ProgramData\MySQL\data' -Second 'c:\programdata\mysql\DATA\') $true 'Test-IsSamePath: case and trailing separator'
    Assert-Equal (Test-IsSamePath -First 'C:/ProgramData/MySQL/data' -Second 'C:\ProgramData\MySQL\data') $true 'Test-IsSamePath: forward slashes'
    Assert-Equal (Test-IsSamePath -First 'C:\ProgramData\MySQL\data\..\data2' -Second 'C:\ProgramData\MySQL\data') $false 'Test-IsSamePath: different directory'
    Assert-Equal (Test-IsSamePath -First 'C:\ProgramData\MySQL\data\..' -Second 'C:\ProgramData\MySQL\data') $false 'Test-IsSamePath: parent via ..'
    Assert-Equal (Test-IsSamePath -First 'data' -Second 'data') $false 'Test-IsSamePath: relative paths'
    Assert-Equal (Test-IsSamePath -First '' -Second 'C:\') $false 'Test-IsSamePath: empty'
    Assert-Equal $PackageDataDirectory 'C:\ProgramData\MySQL\data' 'PackageDataDirectory is the package default'

    # Test-IsMySqlDataDirectory
    $dataDir = Join-Path $root 'data'
    New-Item -ItemType Directory -Path $dataDir | Out-Null
    Assert-Equal (Test-IsMySqlDataDirectory -Path $dataDir) $true 'Test-IsMySqlDataDirectory: empty directory'
    Set-Content -LiteralPath (Join-Path $dataDir 'notes.txt') -Value ''
    Assert-Equal (Test-IsMySqlDataDirectory -Path $dataDir) $false 'Test-IsMySqlDataDirectory: unrelated content'
    Set-Content -LiteralPath (Join-Path $dataDir 'mysql.ibd') -Value ''
    Assert-Equal (Test-IsMySqlDataDirectory -Path $dataDir) $false 'Test-IsMySqlDataDirectory: mysql.ibd without mysql folder'
    New-Item -ItemType Directory -Path (Join-Path $dataDir 'mysql') | Out-Null
    Assert-Equal (Test-IsMySqlDataDirectory -Path $dataDir) $true 'Test-IsMySqlDataDirectory: mysql.ibd and mysql folder'
    Assert-Equal (Test-IsMySqlDataDirectory -Path (Join-Path $root 'missing')) $false 'Test-IsMySqlDataDirectory: missing'

    # ConvertFrom-ChocolateyListOutput
    Assert-Equal (ConvertFrom-ChocolateyListOutput -Lines @('python314|3.14.8') -Name 'python314') '3.14.8' 'Chocolatey list: installed'
    Assert-Equal (ConvertFrom-ChocolateyListOutput -Lines @() -Name 'python314') $null 'Chocolatey list: not installed'
    Assert-Equal (ConvertFrom-ChocolateyListOutput -Lines @('python3|3.14.8', 'python314.install|3.14.8') -Name 'python314') $null 'Chocolatey list: other packages only'
    Assert-Equal (ConvertFrom-ChocolateyListOutput -Lines @('Chocolatey v2.2.2', 'mysql|9.7.2') -Name 'mysql') '9.7.2' 'Chocolatey list: ignores other lines'

    # Get-RootPasswordProblem
    Assert-Equal (Get-RootPasswordProblem -Password 'short') "Use at least 15 characters." 'Password: too short'
    Assert-Equal (Get-RootPasswordProblem -Password 'abcdefghijklmno#') 'The password must not contain # " '' \ or ;.' 'Password: forbidden character'
    Assert-Equal (Get-RootPasswordProblem -Password ' abcdefghijklmnop') 'The password must not start or end with a space.' 'Password: leading space'
    Assert-Equal (Get-RootPasswordProblem -Password "abcdefghijklmnop$([char]0xE4)") 'Use only printable ASCII characters (no umlauts or other non-ASCII characters).' 'Password: non-ASCII'
    Assert-Equal (Get-RootPasswordProblem -Password 'correct horse battery staple') $null 'Password: acceptable'
} finally {
    Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host "$script:passed passed, $script:failures failed."
if ($script:failures -gt 0) { exit 1 }
exit 0
