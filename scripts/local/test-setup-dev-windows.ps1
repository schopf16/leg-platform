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
    Assert-Equal $OldDataDirectory 'C:\ProgramData\MySQL\data' 'OldDataDirectory is the package default of the earlier layout'
    [IO.File]::WriteAllText($myIni, (Get-ServerOptionFileContent), (New-Object Text.ASCIIEncoding))
    Assert-Equal (Get-DataDirectory -MyIni $myIni) 'C:/LEG-MySQL/data/mysql/data' 'Get-DataDirectory: forward slashes'
    Assert-Equal (Test-IsSamePath -First (Get-DataDirectory -MyIni $myIni) -Second $DataDirectory) $true 'Option file: datadir is the data directory'
    $content = Get-ServerOptionFileContent
    foreach ($line in @('[mysqld]', 'bind-address=127.0.0.1', 'mysqlx=OFF', 'port=3306', 'basedir=C:/LEG-MySQL/server/mysql/current', 'datadir=C:/LEG-MySQL/data/mysql/data')) {
        Assert-Equal ([bool](($content -split "`r`n") -ccontains $line)) $true "Option file contains '$line'"
    }
    Assert-Equal $InstallRoot.Contains(' ') $false 'Install root has no spaces (unquoted service path)'

    # Test-IsPathUnder
    Assert-Equal (Test-IsPathUnder -Path 'C:\tools\mysql\current\bin\mysqld.exe' -Root 'C:\tools\mysql') $true 'Test-IsPathUnder: below'
    Assert-Equal (Test-IsPathUnder -Path 'c:\TOOLS\MySQL' -Root 'C:\tools\mysql\') $true 'Test-IsPathUnder: same, case and separator'
    Assert-Equal (Test-IsPathUnder -Path 'C:\tools\mysql-other\bin\mysqld.exe' -Root 'C:\tools\mysql') $false 'Test-IsPathUnder: sibling with common prefix'
    Assert-Equal (Test-IsPathUnder -Path 'C:\tools\mysql\..\evil\mysqld.exe' -Root 'C:\tools\mysql') $false 'Test-IsPathUnder: escape via ..'
    Assert-Equal (Test-IsPathUnder -Path 'tools\mysql\bin' -Root 'C:\tools\mysql') $false 'Test-IsPathUnder: relative path'

    # Get-ServiceOwnershipProblem (earlier layout below <tools>\mysql and new layout)
    $toolsRoot = Join-Path $root 'tools'
    $ownIni = Join-Path $toolsRoot 'mysql\current\my.ini'
    Assert-Equal (Get-ServiceOwnershipProblem -PathName "$plainBase MySQL" -Executable $plainExe -ToolsRoot $toolsRoot) $null 'Ownership: earlier layout without --defaults-file'
    Assert-Equal (Get-ServiceOwnershipProblem -PathName "`"$plainExe`" --defaults-file=`"$ownIni`" MySQL" -Executable $plainExe -ToolsRoot $toolsRoot) $null 'Ownership: earlier layout, its own --defaults-file'
    $problem = Get-ServiceOwnershipProblem -PathName "`"$plainExe`" --defaults-file=C:\other\my.ini MySQL" -Executable $plainExe -ToolsRoot $toolsRoot
    Assert-Equal ([bool]$problem) $true 'Ownership: other --defaults-file is refused'
    $newExe = 'C:\LEG-MySQL\server\mysql\current\bin\mysqld.exe'
    Assert-Equal (Get-ServiceOwnershipProblem -PathName 'C:\LEG-MySQL\server\mysql\current\bin\mysqld MySQL' -Executable $newExe -ToolsRoot $toolsRoot) $null 'Ownership: new layout'
    Assert-Equal (Get-ServiceOwnershipProblem -PathName "$newExe --defaults-file=C:\LEG-MySQL\server\mysql\current\my.ini MySQL" -Executable $newExe -ToolsRoot $toolsRoot) $null 'Ownership: new layout, its own --defaults-file'
    $problem = Get-ServiceOwnershipProblem -PathName "$newExe --defaults-file=`"$ownIni`" MySQL" -Executable $newExe -ToolsRoot $toolsRoot
    Assert-Equal ([bool]$problem) $true 'Ownership: new layout with the option file of the earlier layout is refused'
    $problem = Get-ServiceOwnershipProblem -PathName 'C:\LEG-MySQL\other\mysqld.exe MySQL' -Executable 'C:\LEG-MySQL\other\mysqld.exe' -ToolsRoot $toolsRoot
    Assert-Equal ([bool]$problem) $true 'Ownership: other program below C:\LEG-MySQL is refused'
    $problem = Get-ServiceOwnershipProblem -PathName "`"$spaceExe`" MySQL" -Executable $spaceExe -ToolsRoot $toolsRoot
    Assert-Equal ([bool]$problem) $true 'Ownership: executable outside both layouts is refused'
    $problem = Get-ServiceOwnershipProblem -PathName 'C:\missing\mysqld MySQL' -Executable $null -ToolsRoot $toolsRoot
    Assert-Equal ([bool]$problem) $true 'Ownership: unresolved executable is refused'

    # Test-IsReparsePoint (a junction needs no administrator rights)
    $target = Join-Path $root 'junction-target'
    $junction = Join-Path $root 'junction'
    New-Item -ItemType Directory -Path $target | Out-Null
    New-Item -ItemType Junction -Path $junction -Target $target | Out-Null
    Assert-Equal (Test-IsReparsePoint -Path $junction) $true 'Test-IsReparsePoint: junction'
    Assert-Equal (Test-IsReparsePoint -Path $target) $false 'Test-IsReparsePoint: normal directory'
    Assert-Equal (Test-IsReparsePoint -Path (Join-Path $root 'missing')) $false 'Test-IsReparsePoint: missing'
    # Remove the junction itself before the temporary directory is deleted.
    [IO.Directory]::Delete($junction, $false)

    # Test-RightsAllowWrite
    $rights = [Security.AccessControl.FileSystemRights]
    Assert-Equal (Test-RightsAllowWrite -Rights ([long]$rights::ReadAndExecute)) $false 'Rights: read and execute'
    Assert-Equal (Test-RightsAllowWrite -Rights ([long]$rights::Read)) $false 'Rights: read'
    Assert-Equal (Test-RightsAllowWrite -Rights ([long]$rights::Modify)) $true 'Rights: modify'
    Assert-Equal (Test-RightsAllowWrite -Rights ([long]$rights::FullControl)) $true 'Rights: full control'
    Assert-Equal (Test-RightsAllowWrite -Rights ([long]$rights::AppendData)) $true 'Rights: append data (add subdirectory)'
    Assert-Equal (Test-RightsAllowWrite -Rights ([long]$rights::ChangePermissions)) $true 'Rights: change permissions'
    Assert-Equal (Test-RightsAllowWrite -Rights ([long]0x40000000)) $true 'Rights: generic write'

    # Get-AceViolation and Test-IsAllowedOwner (allowlist)
    $full = [long][Security.AccessControl.FileSystemRights]::FullControl
    $readExecute = [long][Security.AccessControl.FileSystemRights]::ReadAndExecute
    $modify = [long][Security.AccessControl.FileSystemRights]::Modify
    $userSid = 'S-1-5-21-1000000000-2000000000-3000000000-1001'
    Assert-Equal (Get-AceViolation -Sid $SidAdministrators -Rights $full -Type 'Allow') $null 'ACE: Administrators full control'
    Assert-Equal (Get-AceViolation -Sid $SidSystem -Rights $full -Type 'Allow') $null 'ACE: SYSTEM full control'
    Assert-Equal (Get-AceViolation -Sid $SidUsers -Rights $readExecute -Type 'Allow' -AllowUsersReadExecute) $null 'ACE: Users read and execute on server'
    Assert-Equal ([bool](Get-AceViolation -Sid $SidUsers -Rights $readExecute -Type 'Allow')) $true 'ACE: Users read and execute on data is refused'
    Assert-Equal ([bool](Get-AceViolation -Sid $SidUsers -Rights $modify -Type 'Allow' -AllowUsersReadExecute)) $true 'ACE: Users modify is refused'
    Assert-Equal ([bool](Get-AceViolation -Sid $SidUsers -Rights 0x10000000 -Type 'Allow' -AllowUsersReadExecute)) $true 'ACE: Users GENERIC_ALL is refused'
    Assert-Equal ([bool](Get-AceViolation -Sid $SidUsers -Rights 0x40000000 -Type 'Allow' -AllowUsersReadExecute)) $true 'ACE: Users GENERIC_WRITE is refused'
    Assert-Equal ([bool](Get-AceViolation -Sid 'S-1-5-11' -Rights $readExecute -Type 'Allow' -AllowUsersReadExecute)) $true 'ACE: Authenticated Users is refused'
    Assert-Equal ([bool](Get-AceViolation -Sid $userSid -Rights $readExecute -Type 'Allow' -AllowUsersReadExecute)) $true 'ACE: unlisted user SID is refused'
    Assert-Equal ([bool](Get-AceViolation -Sid 'S-1-3-0' -Rights $full -Type 'Allow')) $true 'ACE: Creator Owner is refused'
    Assert-Equal (Get-AceViolation -Sid 'S-1-1-0' -Rights $full -Type 'Deny') $null 'ACE: deny entries are allowed'
    Assert-Equal (Test-IsAllowedOwner -Sid $SidAdministrators) $true 'Owner: Administrators'
    Assert-Equal (Test-IsAllowedOwner -Sid $SidSystem) $true 'Owner: SYSTEM'
    Assert-Equal (Test-IsAllowedOwner -Sid $userSid) $false 'Owner: non-admin user is refused'
    Assert-Equal (Test-IsAllowedOwner -Sid $SidUsers) $false 'Owner: Users is refused'

    # New-RestrictedDirectorySecurity
    $security = New-RestrictedDirectorySecurity -UsersReadExecute
    $rules = @($security.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]))
    Assert-Equal $security.AreAccessRulesProtected $true 'Directory security: inheritance from the parent is blocked'
    Assert-Equal (($rules | ForEach-Object { $_.IdentityReference.Value } | Sort-Object) -join ',') ((@($SidUsers, $SidSystem, $SidAdministrators) | Sort-Object) -join ',') 'Directory security: only Administrators, SYSTEM and Users'
    $violations = @($rules | ForEach-Object { Get-AceViolation -Sid $_.IdentityReference.Value -Rights ([long]$_.FileSystemRights) -Type "$($_.AccessControlType)" -AllowUsersReadExecute } | Where-Object { $_ })
    Assert-Equal $violations.Count 0 'Directory security: passes the allowlist for server'
    $dataRules = @((New-RestrictedDirectorySecurity).GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]))
    Assert-Equal (@($dataRules | Where-Object { $_.IdentityReference.Value -eq $SidUsers }).Count) 0 'Directory security: no Users entry for data'

    # Get-SecurityViolations on a temporary tree (owned by the current user, so it must fail)
    $treeRoot = Join-Path $root 'tree'
    New-Item -ItemType Directory -Path (Join-Path $treeRoot 'bin') | Out-Null
    $treeViolations = @(Get-SecurityViolations -Path $treeRoot -Recurse)
    Assert-Equal ([bool]($treeViolations | Where-Object { $_ -like '*has the owner*' })) $true 'Security: owner that is not Administrators or SYSTEM is reported'
    Assert-Equal ([bool]($treeViolations | Where-Object { $_ -like '*bin*' })) $true 'Security: items below are checked'
    $treeTarget = Join-Path $root 'tree-target'
    New-Item -ItemType Directory -Path $treeTarget | Out-Null
    New-Item -ItemType Junction -Path (Join-Path $treeRoot 'link') -Target $treeTarget | Out-Null
    $treeViolations = @(Get-SecurityViolations -Path $treeRoot -Recurse)
    Assert-Equal ([bool]($treeViolations | Where-Object { $_ -like '*link is a reparse point*' })) $true 'Security: reparse point is reported and not followed'
    Assert-Equal ([bool]($treeViolations | Where-Object { $_ -like '*tree-target*' })) $false 'Security: junction target is not checked'

    # Test-ContainsOnly
    $packageDir = Join-Path $root 'package'
    New-Item -ItemType Directory -Path (Join-Path $packageDir 'current'), (Join-Path $packageDir 'mysql-9.7.2-winx64') | Out-Null
    Assert-Equal (Test-ContainsOnly -Path $packageDir -Patterns @('current', 'mysql-*-winx64')) $true 'ContainsOnly: package content'
    New-Item -ItemType Directory -Path (Join-Path $packageDir 'other') | Out-Null
    Assert-Equal (Test-ContainsOnly -Path $packageDir -Patterns @('current', 'mysql-*-winx64')) $false 'ContainsOnly: other content'
    Assert-Equal (Test-ContainsOnly -Path $treeRoot -Patterns @('bin', 'link')) $false 'ContainsOnly: reparse point'
    [IO.Directory]::Delete((Join-Path $treeRoot 'link'), $false)

    # Get-TreeItemsWithoutReparsePoints and Set-OwnerAdministrators
    $ownerRoot = Join-Path $root 'owner'
    New-Item -ItemType Directory -Path (Join-Path $ownerRoot 'bin') | Out-Null
    Set-Content -LiteralPath (Join-Path $ownerRoot 'bin\mysqld.exe') -Value ''
    $treeItems = @(Get-TreeItemsWithoutReparsePoints -Path $ownerRoot)
    Assert-Equal $treeItems.Count 3 'Tree items: folder, subfolder and file'
    $ownerTarget = Join-Path $root 'owner-target'
    New-Item -ItemType Directory -Path $ownerTarget | Out-Null
    New-Item -ItemType Junction -Path (Join-Path $ownerRoot 'link') -Target $ownerTarget | Out-Null
    $ownerBefore = (Get-Acl -LiteralPath $ownerRoot).GetOwner([Security.Principal.SecurityIdentifier]).Value
    $failed = $false
    try { Set-OwnerAdministrators -Path $ownerRoot } catch { $failed = $_.Exception.Message -like '*link is a symbolic link*' }
    Assert-Equal $failed $true 'Set-OwnerAdministrators: a reparse point fails before anything is changed'
    Assert-Equal ((Get-Acl -LiteralPath $ownerRoot).GetOwner([Security.Principal.SecurityIdentifier]).Value) $ownerBefore 'Set-OwnerAdministrators: owner unchanged after the failure'
    [IO.Directory]::Delete((Join-Path $ownerRoot 'link'), $false)

    # New-RestrictedDirectory and Set-OwnerAdministrators set the owner Administrators; only
    # possible when elevated.
    $elevated = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    if ($elevated) {
        $restricted = Join-Path $root 'restricted'
        New-RestrictedDirectory -Path $restricted -UsersReadExecute
        Assert-Equal @(Get-SecurityViolations -Path $restricted -AllowUsersReadExecute -Recurse).Count 0 'New-RestrictedDirectory: passes the allowlist'
        Set-OwnerAdministrators -Path $ownerRoot
        $owners = @($treeItems | ForEach-Object { (Get-Acl -LiteralPath $_).GetOwner([Security.Principal.SecurityIdentifier]).Value } | Sort-Object -Unique)
        Assert-Equal ($owners -join ',') $SidAdministrators 'Set-OwnerAdministrators: every item is owned by Administrators'
    } else {
        Write-Host 'SKIPPED: New-RestrictedDirectory and Set-OwnerAdministrators owner change (need an elevated PowerShell).'
    }

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
