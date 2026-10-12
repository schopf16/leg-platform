<#
.SYNOPSIS
    Tests of the helper functions of setup-dev-windows.ps1 (D-027, D-030).

.DESCRIPTION
    Parses setup-dev-windows.ps1 with the PowerShell parser, fails on syntax errors, loads only
    its function definitions and the constants before them (the setup itself is never run) and
    tests the helpers that do not change the system. Needs no administrator rights, no
    Chocolatey and no MariaDB; creates and removes a temporary directory and a temporary named
    pipe. Runs with Windows PowerShell 5.1 and PowerShell 7.

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
function Assert-Throws {
    param([scriptblock]$Block, [string]$Like, [string]$Name)
    $message = $null
    try { & $Block } catch { $message = $_.Exception.Message }
    Assert-Equal ([bool]($message -and $message -like $Like)) $true $Name
}

$currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$root = Join-Path ([IO.Path]::GetTempPath()) ("leg-setup-test-" + [Guid]::NewGuid().ToString('N'))
try {
    $plainBin = Join-Path $root 'tools\mysql\current\bin'
    $spaceBin = Join-Path $root 'Program Files\MariaDB 12.3\bin'
    New-Item -ItemType Directory -Path $plainBin, $spaceBin | Out-Null
    $plainExe = Join-Path $plainBin 'mysqld.exe'
    $spaceExe = Join-Path $spaceBin 'mysqld.exe'
    Set-Content -LiteralPath $plainExe -Value ''
    Set-Content -LiteralPath $spaceExe -Value ''
    $plainBase = Join-Path $plainBin 'mysqld'
    $spaceBase = Join-Path $spaceBin 'mysqld'
    $spaceIni = Join-Path $root 'Program Files\MariaDB 12.3\data\my.ini'
    # Command line as registered by mariadb-install-db.exe (sql/mysql_install_db.cc).
    $mariaDbPathName = "`"$spaceExe`" `"--defaults-file=$spaceIni`" `"MySQL`""

    # Resolve-ServiceExecutablePath
    $cases = @(
        @{ Name = 'MariaDB service command line'; PathName = $mariaDbPathName; Expected = $spaceExe },
        @{ Name = 'unquoted, no extension (earlier MySQL package)'; PathName = "$plainBase MySQL"; Expected = $plainExe },
        @{ Name = 'unquoted, no extension, no arguments'; PathName = $plainBase; Expected = $plainExe },
        @{ Name = 'unquoted, with .exe'; PathName = "$plainExe --defaults-file=C:\x\my.ini MySQL"; Expected = $plainExe },
        @{ Name = 'quoted, no extension'; PathName = "`"$plainBase`" MySQL"; Expected = $plainExe },
        @{ Name = 'quoted with spaces, no extension'; PathName = "`"$spaceBase`" MySQL"; Expected = $spaceExe },
        @{ Name = 'unquoted with spaces, with .exe'; PathName = "$spaceExe MySQL"; Expected = $spaceExe },
        @{ Name = 'leading white space'; PathName = "  $plainBase MySQL"; Expected = $plainExe },
        @{ Name = 'missing file'; PathName = (Join-Path $root 'missing\mysqld MySQL'); Expected = $null },
        @{ Name = 'directory is not an executable'; PathName = "$plainBin MySQL"; Expected = $null },
        @{ Name = 'unterminated quote'; PathName = "`"$plainBase MySQL"; Expected = $null },
        @{ Name = 'empty'; PathName = ''; Expected = $null }
    )
    foreach ($case in $cases) {
        Assert-Equal (Resolve-ServiceExecutablePath -PathName $case.PathName) $case.Expected "Resolve-ServiceExecutablePath: $($case.Name)"
    }

    # Get-DefaultsFileFromCommandLine
    Assert-Equal (Get-DefaultsFileFromCommandLine -PathName $mariaDbPathName) $spaceIni 'Defaults file: whole argument quoted (MariaDB)'
    Assert-Equal (Get-DefaultsFileFromCommandLine -PathName "`"$spaceExe`" --defaults-file=`"C:\a b\my.ini`" MySQL") 'C:\a b\my.ini' 'Defaults file: quoted value'
    Assert-Equal (Get-DefaultsFileFromCommandLine -PathName "$plainExe --defaults-file=C:\x\my.ini MySQL") 'C:\x\my.ini' 'Defaults file: unquoted value'
    Assert-Equal (Get-DefaultsFileFromCommandLine -PathName "$plainBase MySQL") $null 'Defaults file: absent'

    # Option file as written by mariadb-install-db.exe with --skip-networking and by the MSI
    # (IniFile innodb_buffer_pool_size); WritePrivateProfileString uses "key=value".
    $dataDir = 'C:\Program Files\MariaDB 12.3\data'
    $initial = "[mysqld]`r`ndatadir=C:/Program Files/MariaDB 12.3/data/`r`nskip-networking=ON`r`nnamed-pipe=ON`r`nsocket=MySQL`r`nport=3306`r`ninnodb_buffer_pool_size=2047M`r`n[client]`r`nsocket=MySQL`r`nprotocol=pipe`r`nplugin-dir=C:/Program Files/MariaDB 12.3/lib/plugin`r`n"
    $entries = @(Get-OptionFileEntries -Text $initial)
    Assert-Equal (Get-ServerOptionValue -Entries $entries -Key 'datadir') 'C:/Program Files/MariaDB 12.3/data/' 'Option file: datadir'
    Assert-Equal (Get-ServerOptionValue -Entries $entries -Key 'innodb-buffer-pool-size') '2047M' 'Option file: underscores are dashes'
    Assert-Equal (Get-ServerOptionValue -Entries $entries -Key 'bind-address') $null 'Option file: absent option'
    Assert-Equal @(Get-InitialOptionFileProblems -Entries $entries -DataDirectory $dataDir).Count 0 'Initial option file: accepted'
    $variants = @(
        @{ Name = 'networking not skipped'; Text = $initial.Replace("skip-networking=ON`r`n", ''); Like = '*skip-networking*' },
        @{ Name = 'networking explicitly off'; Text = $initial.Replace('skip-networking=ON', 'skip-networking=OFF'); Like = '*skip-networking*' },
        @{ Name = 'other pipe name'; Text = $initial.Replace("[mysqld]`r`n", "[mysqld]`r`n").Replace("socket=MySQL`r`nport", "socket=Other`r`nport"); Like = "*named pipe is 'Other'*" },
        @{ Name = 'include directive'; Text = $initial + "!includedir C:/conf`r`n"; Like = '*include*' },
        @{ Name = 'feedback plugin'; Text = $initial.Replace('port=3306', "port=3306`r`nfeedback=ON"); Like = '*feedback*' },
        @{ Name = 'other data directory'; Text = $initial.Replace('datadir=C:/Program Files/MariaDB 12.3/data/', 'datadir=D:/data'); Like = '*datadir*' }
    )
    foreach ($variant in $variants) {
        $problems = @(Get-InitialOptionFileProblems -Entries (Get-OptionFileEntries -Text $variant.Text) -DataDirectory $dataDir)
        Assert-Equal ([bool]($problems | Where-Object { $_ -like $variant.Like })) $true "Initial option file refused: $($variant.Name)"
    }
    $quoted = @(Get-OptionFileEntries -Text "[mariadb]`nloose_Skip_Networking`n# comment=1`n; other=2`nsocket = `"MySQL`"`n")
    Assert-Equal (Get-ServerOptionValue -Entries $quoted -Key 'skip-networking') '' 'Option file: loose- prefix, case and option without value'
    Assert-Equal (Get-ServerOptionValue -Entries $quoted -Key 'socket') 'MySQL' 'Option file: quoted value and spaces'
    Assert-Equal @($quoted).Count 2 'Option file: comments are skipped'
    foreach ($value in @(@{ V = $null; E = $false }, @{ V = ''; E = $true }, @{ V = 'ON'; E = $true }, @{ V = '1'; E = $true }, @{ V = 'OFF'; E = $false }, @{ V = '0'; E = $false })) {
        Assert-Equal (Test-IsOptionOn -Value $value.V) $value.E "Test-IsOptionOn: '$($value.V)'"
    }

    # Get-NetworkOptionFileText
    $switched = Get-NetworkOptionFileText -Text ($initial + "[mariadb]`r`nloose-skip-networking`r`nbind_address=0.0.0.0`r`n") -LineBreak "`r`n"
    $switchedEntries = @(Get-OptionFileEntries -Text $switched)
    Assert-Equal (Get-ServerOptionValue -Entries $switchedEntries -Key 'bind-address') '127.0.0.1' 'Switched option file: bind-address'
    Assert-Equal (Get-ServerOptionValue -Entries $switchedEntries -Key 'port') '3306' 'Switched option file: port'
    Assert-Equal (Test-IsOptionOn -Value (Get-ServerOptionValue -Entries $switchedEntries -Key 'skip-networking')) $false 'Switched option file: networking on'
    Assert-Equal (Get-ServerOptionValue -Entries $switchedEntries -Key 'named-pipe') 'OFF' 'Switched option file: named pipe off'
    Assert-Equal (Get-ServerOptionValue -Entries $switchedEntries -Key 'socket') $null 'Switched option file: no pipe name'
    Assert-Equal @($switchedEntries | Where-Object { $_.Key -eq 'bind-address' }).Count 1 'Switched option file: earlier bind-address removed'
    Assert-Equal @($switchedEntries | Where-Object { $_.Key -eq 'skip-networking' }).Count 1 'Switched option file: earlier skip-networking lines removed'
    Assert-Equal @($switchedEntries | Where-Object { $_.Group -eq 'client' -and $_.Key -in @('protocol', 'socket') }).Count 0 'Switched option file: client protocol and socket removed'
    Assert-Equal (Get-ServerOptionValue -Entries $switchedEntries -Key 'innodb-buffer-pool-size') '2047M' 'Switched option file: other settings kept'
    Assert-Equal @($switchedEntries | Where-Object { $_.Group -eq 'client' -and $_.Key -eq 'plugin-dir' }).Count 1 'Switched option file: client plugin-dir kept'
    Assert-Equal ($switched -match "(?<!`r)`n") $false 'Switched option file: line breaks kept (CRLF)'
    Assert-Equal @(Get-InitialOptionFileProblems -Entries $switchedEntries -DataDirectory $dataDir | Where-Object { $_ -like '*datadir*' -or $_ -like '*include*' }).Count 0 'Switched option file: datadir kept, no include'

    # Paths
    Assert-Equal (Test-IsSamePath -First 'C:/Program Files/MariaDB 12.3/data/' -Second $dataDir) $true 'Test-IsSamePath: forward slashes and trailing separator'
    Assert-Equal (Test-IsSamePath -First 'C:\ProgramData\MySQL\data\..' -Second 'C:\ProgramData\MySQL\data') $false 'Test-IsSamePath: parent via ..'
    Assert-Equal (Test-IsSamePath -First 'data' -Second 'data') $false 'Test-IsSamePath: relative paths'
    Assert-Equal (Test-IsPathUnder -Path 'C:\tools\mysql-other\bin\mysqld.exe' -Root 'C:\tools\mysql') $false 'Test-IsPathUnder: sibling with common prefix'
    Assert-Equal (Test-IsPathUnder -Path 'C:\tools\mysql\..\evil\mysqld.exe' -Root 'C:\tools\mysql') $false 'Test-IsPathUnder: escape via ..'
    Assert-Equal (Test-IsStrictlyUnder -Path 'C:\Program Files\MariaDB 12.3' -Root 'C:\Program Files') $true 'Test-IsStrictlyUnder: below'
    Assert-Equal (Test-IsStrictlyUnder -Path 'C:\Program Files\' -Root 'C:\Program Files') $false 'Test-IsStrictlyUnder: same folder'
    Assert-Equal (Test-IsStrictlyUnder -Path 'C:\Program Files (x86)\MariaDB' -Root 'C:\Program Files') $false 'Test-IsStrictlyUnder: other folder with common prefix'

    # Reparse points (a junction needs no administrator rights)
    $target = Join-Path $root 'junction-target'
    $junction = Join-Path $root 'junction'
    New-Item -ItemType Directory -Path (Join-Path $target 'sub') | Out-Null
    New-Item -ItemType Junction -Path $junction -Target $target | Out-Null
    Assert-Equal (Test-IsReparsePoint -Path $junction) $true 'Test-IsReparsePoint: junction'
    Assert-Equal (Test-IsReparsePoint -Path $target) $false 'Test-IsReparsePoint: normal directory'
    Assert-Equal (Get-ReparsePointOnPath -Path (Join-Path $junction 'sub') -Root $root) $junction 'Get-ReparsePointOnPath: junction on the path'
    Assert-Equal (Get-ReparsePointOnPath -Path (Join-Path $target 'sub') -Root $root) $null 'Get-ReparsePointOnPath: no reparse point'
    Assert-Equal ((Get-ReparsePaths -Paths @($target, $junction, (Join-Path $root 'missing'))) -join ',') $junction 'Get-ReparsePaths: only the junction'
    [IO.Directory]::Delete($junction, $false)

    # Rights and the allowlist of access control entries
    $rights = [Security.AccessControl.FileSystemRights]
    Assert-Equal (Test-RightsAllowWrite -Rights ([long]$rights::ReadAndExecute)) $false 'Rights: read and execute'
    Assert-Equal (Test-RightsAllowWrite -Rights ([long]$rights::Modify)) $true 'Rights: modify'
    Assert-Equal (Test-RightsAllowWrite -Rights ([long]$rights::AppendData)) $true 'Rights: append data (add subdirectory)'
    Assert-Equal (Test-RightsAllowWrite -Rights ([long]0x40000000)) $true 'Rights: generic write'
    $full = [long]$rights::FullControl
    $readExecute = [long]$rights::ReadAndExecute
    $userSid = 'S-1-5-21-1000000000-2000000000-3000000000-1001'
    $admins = @($SidAdministrators, $SidSystem)
    Assert-Equal (Get-AceViolation -Sid $SidAdministrators -Rights $full -Type 'Allow' -AllowedSids $admins) $null 'ACE: Administrators full control'
    Assert-Equal (Get-AceViolation -Sid $SidUsers -Rights $readExecute -Type 'Allow' -AllowedSids $admins -AllowReadForOthers) $null 'ACE: Users read in the program folder'
    Assert-Equal ([bool](Get-AceViolation -Sid $SidUsers -Rights $readExecute -Type 'Allow' -AllowedSids $admins)) $true 'ACE: Users read in the data directory is refused'
    Assert-Equal ([bool](Get-AceViolation -Sid $SidUsers -Rights ([long]$rights::Modify) -Type 'Allow' -AllowedSids $admins -AllowReadForOthers)) $true 'ACE: Users modify is refused'
    Assert-Equal ([bool](Get-AceViolation -Sid 'S-1-5-11' -Rights 0x10000000 -Type 'Allow' -AllowedSids $admins -AllowReadForOthers)) $true 'ACE: Authenticated Users GENERIC_ALL is refused'
    Assert-Equal ([bool](Get-AceViolation -Sid $userSid -Rights $full -Type 'Allow' -AllowedSids $admins -AllowReadForOthers)) $true 'ACE: unlisted user with full control is refused'
    Assert-Equal (Get-AceViolation -Sid 'S-1-1-0' -Rights $full -Type 'Deny' -AllowedSids $admins) $null 'ACE: deny entries are allowed'

    # New-DataDirectorySecurity
    $serviceSid = 'S-1-5-80-1-2-3-4-5'
    $security = New-DataDirectorySecurity -ServiceSid $serviceSid
    $rules = @($security.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]))
    Assert-Equal $security.AreAccessRulesProtected $true 'Data directory security: inheritance from the program folder is blocked'
    Assert-Equal (($rules | ForEach-Object { $_.IdentityReference.Value } | Sort-Object) -join ',') ((@($SidAdministrators, $SidSystem, $serviceSid) | Sort-Object) -join ',') 'Data directory security: Administrators, SYSTEM and the service only'
    Assert-Equal @($rules | Where-Object { $_.InheritanceFlags -ne [Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit' }).Count 0 'Data directory security: inherited by files and folders'
    Assert-Equal @((New-DataDirectorySecurity -ServiceSid $SidSystem).GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])).Count 2 'Data directory security: LocalSystem service adds no entry'

    # Protect-DataDirectory and Get-SecurityViolations on a temporary tree owned by the
    # current user; the current user's SID stands in for the service account.
    $data = Join-Path $root 'data'
    New-Item -ItemType Directory -Path (Join-Path $data 'mysql') | Out-Null
    Set-Content -LiteralPath (Join-Path $data 'mysql\global_priv.MAD') -Value ''
    Set-Content -LiteralPath (Join-Path $data 'my.ini') -Value ''
    # An explicit, protected copy of a DACL with Users read, as mariadb-install-db.exe creates.
    $explicit = Get-ItemDacl -Path (Join-Path $data 'mysql')
    $explicit.SetAccessRuleProtection($true, $true)
    $explicit.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule((New-Object Security.Principal.SecurityIdentifier($SidUsers)), 'ReadAndExecute', 'ContainerInherit, ObjectInherit', 'None', 'Allow')))
    Set-ItemDacl -Path (Join-Path $data 'mysql') -Security $explicit
    $allowed = @($SidAdministrators, $SidSystem, $currentSid)
    $before = @(Get-SecurityViolations -Path $data -AllowedOwners $allowed -AllowedSids $allowed)
    Assert-Equal ([bool]($before | Where-Object { $_ -like "*$SidUsers*" })) $true 'Data directory: Users entry is found before the protection'
    Protect-DataDirectory -DataDirectory $data -ServiceSid $currentSid
    $after = @(Get-SecurityViolations -Path $data -AllowedOwners $allowed -AllowedSids $allowed)
    Assert-Equal ($after -join '; ') '' 'Data directory: no violations after the protection'
    $childDacl = Get-ItemDacl -Path (Join-Path $data 'mysql')
    Assert-Equal $childDacl.AreAccessRulesProtected $false 'Data directory: subfolder inherits again'
    Assert-Equal @($childDacl.GetAccessRules($true, $false, [Security.Principal.SecurityIdentifier])).Count 0 'Data directory: explicit entries of the subfolder removed'
    Assert-Equal @((Get-ItemDacl -Path (Join-Path $data 'mysql\global_priv.MAD')).GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]) | Where-Object { $_.IdentityReference.Value -eq $SidUsers }).Count 0 'Data directory: files below have no Users entry'
    # Junctions fail before anything is changed and are reported by the allowlist.
    $dataLink = Join-Path $data 'link'
    New-Item -ItemType Junction -Path $dataLink -Target $target | Out-Null
    Assert-Throws { Protect-DataDirectory -DataDirectory $data -ServiceSid $currentSid } '*link is a symbolic link*' 'Protect-DataDirectory: reparse point fails'
    $withLink = @(Get-SecurityViolations -Path $data -AllowedOwners $allowed -AllowedSids $allowed)
    Assert-Equal ([bool]($withLink | Where-Object { $_ -like '*link is a reparse point*' })) $true 'Get-SecurityViolations: reparse point reported'
    Assert-Equal ([bool]($withLink | Where-Object { $_ -like '*junction-target*' })) $false 'Get-SecurityViolations: junction target not checked'
    [IO.Directory]::Delete($dataLink, $false)
    $owned = @(Get-SecurityViolations -Path $data -AllowedOwners @($SidAdministrators, $SidSystem) -AllowedSids $allowed)
    Assert-Equal ([bool]($owned | Where-Object { $_ -like '*has the owner*' })) $true 'Get-SecurityViolations: other owner reported'
    Assert-Equal @(Get-SecurityViolations -Path $root -AllowedOwners $allowed -AllowedSids $allowed -AllowReadForOthers -ExcludePath $root).Count 0 'Get-SecurityViolations: excluded subtree is skipped'

    # Get-TreeItemsWithoutReparsePoints
    Assert-Equal @(Get-TreeItemsWithoutReparsePoints -Path $data).Count 4 'Tree items: folder, subfolder and two files'

    # Service account and TrustedInstaller
    Assert-Equal (Get-ServiceAccountSid -StartName 'LocalSystem' -Service 'MySQL') $SidSystem 'Service account: LocalSystem'
    Assert-Equal (Get-ServiceAccountSid -StartName 'NT SERVICE\TrustedInstaller' -Service 'TrustedInstaller') (Get-TrustedInstallerSid) 'Service account: virtual account NT SERVICE\<service>'
    Assert-Equal ((Get-TrustedInstallerSid) -like 'S-1-5-80-*') $true 'TrustedInstaller: service SID'
    Assert-Throws { Get-ServiceAccountSid -StartName '.\someone' -Service 'MySQL' } '*expected NT SERVICE\MySQL*' 'Service account: other account is refused'
    Assert-Throws { Get-ServiceAccountSid -StartName 'NT SERVICE\Other' -Service 'MySQL' } '*expected NT SERVICE\MySQL*' 'Service account: virtual account of another service is refused'

    # Named pipe server process: the pipe is served by a child process, which must be reported
    # (not this process). The child accepts connections for up to 60 seconds.
    $pipeName = 'leg-setup-test-' + [Guid]::NewGuid().ToString('N')
    $childScript = "`$end = (Get-Date).AddSeconds(60); while ((Get-Date) -lt `$end) { `$p = New-Object IO.Pipes.NamedPipeServerStream('$pipeName', [IO.Pipes.PipeDirection]::InOut, 1); try { `$p.WaitForConnection() } finally { `$p.Dispose() } }"
    $child = Start-Process -FilePath (Get-Process -Id $PID).Path -ArgumentList @('-NoProfile', '-NonInteractive', '-Command', $childScript) -PassThru -WindowStyle Hidden
    try {
        $serverPid = -1
        $deadline = (Get-Date).AddSeconds(30)
        while ($serverPid -le 0 -and (Get-Date) -lt $deadline) {
            $serverPid = Get-NamedPipeServerProcessId -Name $pipeName
            if ($serverPid -le 0) { Start-Sleep -Milliseconds 200 }
        }
        Assert-Equal $serverPid $child.Id 'Named pipe: server process is the child that created the pipe'
        Assert-Equal ($serverPid -ne $PID) $true 'Named pipe: not the process of the test'
    } finally {
        Stop-Process -Id $child.Id -Force -ErrorAction SilentlyContinue
        $child.WaitForExit(10000) | Out-Null
    }
    Assert-Equal (Get-NamedPipeServerProcessId -Name $pipeName) -1 'Named pipe: missing pipe'
    Assert-Throws { Get-NamedPipeServerProcessId -Name '..\x' } '*Invalid pipe name*' 'Named pipe: invalid name is refused'

    # Listener and server checks
    function New-Listener { param($Address, $Port) [pscustomobject]@{ LocalAddress = $Address; LocalPort = $Port } }
    Assert-Equal @(Get-ListenerProblems -Listeners @((New-Listener '127.0.0.1' 3306))).Count 0 'Listeners: loopback only'
    Assert-Equal ((Get-ListenerProblems -Listeners @()) -join ';') 'does not listen on 127.0.0.1:3306' 'Listeners: none'
    Assert-Equal ((Get-ListenerProblems -Listeners @((New-Listener '127.0.0.1' 3306), (New-Listener '0.0.0.0' 3306))) -join ';') 'listens on 0.0.0.0:3306' 'Listeners: all interfaces'
    Assert-Equal ((Get-ListenerProblems -Listeners @((New-Listener '127.0.0.1' 3306), (New-Listener '::' 3306))) -join ';') 'listens on :::3306' 'Listeners: IPv6'
    Assert-Equal ((Get-ListenerProblems -Listeners @((New-Listener '127.0.0.1' 3306), (New-Listener '127.0.0.1' 33060))) -join ';') 'listens on 127.0.0.1:33060' 'Listeners: other port'
    Assert-Equal @(Get-ListenerProblems -Listeners @() -ExpectNone).Count 0 'Listeners: none expected'
    Assert-Equal ((Get-ListenerProblems -Listeners @((New-Listener '127.0.0.1' 3306)) -ExpectNone) -join ';') 'listens on 127.0.0.1:3306' 'Listeners: TCP while networking is skipped'
    Assert-Equal @(Get-ServerCheckProblems -Line '127.0.0.1|0|0|3306|NONE|0|0|0|FILE|mypc.log|mypc-slow.log|DISABLED').Count 0 'Server check: expected values'
    Assert-Equal @(Get-ServerCheckProblems -Line '127.0.0.1|0|0|3306|DISABLED|0|0|0|FILE|mypc.log|mypc-slow.log|DISABLED').Count 0 'Server check: feedback plugin disabled'
    Assert-Equal ((Get-ServerCheckProblems -Line '*|1|1|3307|ACTIVE|1|1|1|TABLE|C:\x\g.log|/tmp/s.log|ON') -join ';') "bind_address is '*';skip_networking is on;named_pipe is on;port is 3307;the feedback plugin is active;shared_memory is on;general_log is on;slow_query_log is on;log_output is 'TABLE';event_scheduler is 'ON';log file 'C:\x\g.log' is not a plain file name;log file '/tmp/s.log' is not a plain file name" 'Server check: every deviation reported'
    Assert-Equal ((Get-ServerCheckProblems -Line '127.0.0.1|0|0|3306|NONE|0|0|0|FILE|mypc.log|mypc-slow.log|OFF') -join ';') "event_scheduler is 'OFF'" 'Server check: event scheduler must be DISABLED after the restart'
    Assert-Equal ((Get-ServerCheckProblems -Line '127.0.0.1|0|0|3306|NONE') -join ';') "unexpected server check result '127.0.0.1|0|0|3306|NONE'" 'Server check: malformed result'
    Assert-Equal ($ServerCheckSql -like '*@@GLOBAL.bind_address*@@GLOBAL.skip_networking*@@GLOBAL.named_pipe*@@GLOBAL.port*FEEDBACK*@@GLOBAL.shared_memory*@@GLOBAL.general_log*@@GLOBAL.slow_query_log*@@GLOBAL.log_output*@@GLOBAL.general_log_file*@@GLOBAL.slow_query_log_file*@@GLOBAL.event_scheduler*') $true 'Server check: query order matches the parser'
    Assert-Equal ($OtherConnectionsSql -clike "*PROCESSLIST WHERE ID <> CONNECTION_ID() AND COMMAND <> 'Daemon'*") $true 'Other connections: background threads excluded'

    # SQL of the hardening and the application account
    $hardening = Get-HardeningSql -Password 'correct horse battery staple'
    foreach ($expected in @(
        "DELETE FROM mysql.global_priv WHERE User='';",
        "DELETE FROM mysql.global_priv WHERE User='root' AND Host NOT IN ('localhost', '127.0.0.1', '::1');",
        'DROP DATABASE IF EXISTS test;',
        "DELETE FROM mysql.db WHERE Db='test' OR Db='test\\_%';",
        'FLUSH PRIVILEGES;',
        "DELETE FROM mysql.proxies_priv WHERE User='root' AND Host NOT IN ('localhost', '127.0.0.1', '::1');",
        'DELIMITER //',
        'DELIMITER ;'
    )) {
        Assert-Equal ([bool](($hardening -split "`r?`n") -ccontains $expected)) $true "Hardening SQL: $expected"
    }
    Assert-Equal ([regex]::Matches($hardening, "IDENTIFIED BY 'correct horse battery staple'").Count) 3 'Hardening SQL: password for the three root accounts'
    $hardeningLines = @($hardening -split "`r?`n" | Where-Object { $_ })
    Assert-Equal ([bool]($hardeningLines[1] -clike "IF (SELECT COUNT(*) FROM information_schema.PROCESSLIST WHERE ID <> CONNECTION_ID() AND COMMAND <> 'Daemon') > 0 THEN SIGNAL SQLSTATE '45000'*END IF //")) $true 'Hardening SQL: aborts first if another client is connected'
    foreach ($check in @('mysql.tables_priv', 'mysql.columns_priv', 'mysql.procs_priv', 'mysql.roles_mapping', 'mysql.proxies_priv', 'mysql.db', 'mysql.plugin', 'mysql.func', 'mysql.servers','information_schema.EVENTS', 'information_schema.TRIGGERS', 'information_schema.PROCESSLIST', '@@GLOBAL.general_log', '@@GLOBAL.shared_memory', "'`$.account_locked'", "SCHEMA_NAME NOT IN ('information_schema', 'mysql', 'performance_schema', 'sys', 'leg_platform')")) {
        Assert-Equal $HardeningProblemSql.Contains($check) $true "Hardening check SQL: $check"
    }
    $appSql = Get-AppAccountSql -Password 'generated-secret'
    # -like treats the backtick as escape character, so String.Contains is used here.
    Assert-Equal $appSql.Contains("GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, ALTER, INDEX ON ``leg\_platform``.* TO 'leg_platform'@'127.0.0.1';") $true 'Application SQL: escaped grant on leg_platform'
    Assert-Equal $appSql.Contains("ON ``test\_leg\_platform``.* TO 'leg_platform'@'127.0.0.1';") $true 'Application SQL: escaped grant on the test database'
    Assert-Equal ([bool]($appSql -clike '*COLLATE utf8mb4_uca1400_ai_ci;*')) $true 'Application SQL: collation available from MariaDB 10.10.1'
    Assert-Equal $appSql.Contains('CREATE DATABASE IF NOT EXISTS') $false 'Application SQL: the database must not exist yet'
    Assert-Equal ([bool]($appSql -like '*REFERENCES*')) $false 'Application SQL: no unused REFERENCES privilege'
    Assert-Equal ([bool]($AppGrantCheckSql -clike "*Db IN ('leg\\_platform', 'test\\_leg\\_platform')*User = 'leg_platform'*")) $true 'Grant check SQL: escaped names'
    Assert-Equal ([bool]($HardeningProblemSql -clike '*JSON_VALUE(Priv, ''$.authentication_string'')*')) $true 'Hardening check SQL: literal JSON path'

    # Product name, incomplete setup, option file security
    Assert-Equal (Get-MariaDbProductName -Version '12.3.3') 'MariaDB 12.3 (x64)' 'Product name: 12.3.3'
    Assert-Throws { Get-MariaDbProductName -Version 'latest' } '*Invalid MariaDB version*' 'Product name: invalid version'
    foreach ($name in @('MariaDB 12.3 (x64)', 'MariaDB 10.11', 'MariaDB 11.8 (arm64)')) { Assert-Equal ($name -cmatch $MariaDbProductPattern) $true "Product pattern: '$name'" }
    foreach ($name in @('MariaDB Connector C 64-bit', 'MariaDB 12.3 (x64) Debug Symbols', 'mariadb 12.3 (x64)')) { Assert-Equal ($name -cmatch $MariaDbProductPattern) $false "Product pattern: not '$name'" }
    Assert-Equal @(Get-SetupIncompleteReasons -StartType 'Automatic' -Entries $switchedEntries).Count 0 'Incomplete setup: completed installation'
    Assert-Equal ((Get-SetupIncompleteReasons -StartType 'Manual' -Entries $entries) -join ';') 'the service start type is Manual;networking is still skipped;the named pipe is still on' 'Incomplete setup: interrupted before the switch'
    Assert-Equal ((Get-SetupIncompleteReasons -StartType 'Disabled' -Entries $null) -join ';') 'the service start type is Disabled;my.ini could not be read' 'Incomplete setup: unreadable my.ini'
    Assert-Equal ([bool](@(Get-InitialOptionFileProblems -Entries (Get-OptionFileEntries -Text ($initial + "[mysqld]`r`nshared_memory`r`n")) -DataDirectory $dataDir) -like '*shared memory*')) $true 'Initial option file refused: shared memory'
    Assert-Equal (Get-ServerOptionValue -Entries $switchedEntries -Key 'shared-memory') 'OFF' 'Switched option file: shared memory off'
    $fileSecurity = New-AdministratorsFileSecurity
    Assert-Equal $fileSecurity.AreAccessRulesProtected $true 'Option file security: no inheritance'
    Assert-Equal (($fileSecurity.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]) | ForEach-Object { $_.IdentityReference.Value } | Sort-Object) -join ',') ((@($SidAdministrators, $SidSystem) | Sort-Object) -join ',') 'Option file security: Administrators and SYSTEM only'
    Assert-Equal $fileSecurity.GetOwner([Security.Principal.SecurityIdentifier]).Value $SidAdministrators 'Option file security: owner Administrators'

    # Events, triggers, stored objects, authentication
    $hardeningCheckLines = @($HardeningProblemSql -split "`r?`n")
    Assert-Equal ([bool]($hardeningCheckLines -ccontains "SELECT CONCAT('event: ', EVENT_SCHEMA, '.', EVENT_NAME) FROM information_schema.EVENTS;")) $true 'Hardening check SQL: every event, no schema excluded'
    Assert-Equal ([bool]($hardeningCheckLines -ccontains "SELECT CONCAT('trigger: ', TRIGGER_SCHEMA, '.', TRIGGER_NAME) FROM information_schema.TRIGGERS;")) $true 'Hardening check SQL: every trigger, no schema excluded'
    Assert-Equal $HardeningProblemSql.Contains("@@GLOBAL.event_scheduler NOT IN ('OFF', 'DISABLED')") $true 'Hardening check SQL: event scheduler off'
    Assert-Equal $HardeningProblemSql.Contains("JSON_EXISTS(Priv, '`$.auth_or') = 1") $true 'Hardening check SQL: no authentication chain'
    Assert-Equal $HardeningProblemSql.Contains("<> 'mysql_native_password'") $true 'Hardening check SQL: expected plugin'
    Assert-Equal $HardeningProblemSql.Contains("HAVING COUNT(DISTINCT JSON_VALUE(Priv, '`$.authentication_string')) <> 1") $true 'Hardening check SQL: same password for the three root accounts'
    Assert-Equal $HardeningProblemSql.Contains("WHERE User <> 'root' AND COALESCE(JSON_VALUE(Priv, '`$.access'), '0') <> '0'") $true 'Hardening check SQL: no global privileges except root'
    Assert-Equal $HardeningProblemSql.Contains("WHERE NOT (User = 'leg_platform' AND Host = '127.0.0.1' AND Db IN ('leg\\_platform', 'test\\_leg\\_platform'))") $true 'Hardening check SQL: only the application grants'
    Assert-Equal ($HardeningProblemSql -match 'NOT IN \(''sys'', ''mysql''\)') $false 'Hardening check SQL: no schema exclusions'
    Assert-Equal $StoredObjectSql.Contains('FROM information_schema.ROUTINES;') $true 'Stored objects: all routines'
    Assert-Equal $StoredObjectSql.Contains('FROM information_schema.VIEWS;') $true 'Stored objects: all views'
    Assert-Equal @($BootstrapObjects).Count 153 'Bootstrap objects: 153 (1 mysql view, 101 sys views, 29 procedures, 22 functions)'
    Assert-Equal ([bool]($BootstrapObjects -ccontains 'mysql:VIEW:user')) $true 'Bootstrap objects: mysql.user'
    Assert-Equal ([bool]($BootstrapObjects -ccontains 'sys:VIEW:x$host_summary')) $true 'Bootstrap objects: names with $'
    Assert-Equal ([bool]($BootstrapObjects -ccontains 'sys:PROCEDURE:create_synonym_db')) $true 'Bootstrap objects: sys procedure'
    Assert-Equal @(Get-UnexpectedObjects -Lines @('mysql:VIEW:user:mariadb.sys@localhost', 'sys:FUNCTION:format_bytes:mariadb.sys@localhost', '')).Count 0 'Stored objects: bootstrap objects accepted'
    Assert-Equal ((Get-UnexpectedObjects -Lines @('sys:FUNCTION:format_bytes:root@localhost', 'mysql:PROCEDURE:backdoor:mariadb.sys@localhost', 'leg_platform:VIEW:v:root@localhost')) -join ';') 'stored object: sys:FUNCTION:format_bytes:root@localhost;stored object: mysql:PROCEDURE:backdoor:mariadb.sys@localhost;stored object: leg_platform:VIEW:v:root@localhost' 'Stored objects: other definer, unknown name or schema refused'
    $grants = @(
        "GRANT USAGE ON *.* TO ``leg_platform``@``127.0.0.1`` IDENTIFIED BY PASSWORD '*0000'",
        'GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, INDEX, ALTER ON `leg\\_platform`.* TO `leg_platform`@`127.0.0.1`',
        'GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, INDEX, ALTER ON `test\\_leg\\_platform`.* TO `leg_platform`@`127.0.0.1`'
    )
    Assert-Equal @(Get-GrantProblems -Lines $grants).Count 0 'Grants: exactly the application privileges (batch escaping)'
    Assert-Equal @(Get-GrantProblems -Lines @($grants[0], $grants[1].Replace('\\', '\'), $grants[2].Replace('\\', '\'))).Count 0 'Grants: unescaped output'
    Assert-Equal ((Get-GrantProblems -Lines @($grants[0], $grants[1].Replace('INDEX, ALTER', 'INDEX, ALTER, EXECUTE'), $grants[2])) -join ';') 'unexpected privileges on leg\_platform' 'Grants: additional privilege refused'
    Assert-Equal ((Get-GrantProblems -Lines @($grants[0], $grants[1])) -join ';') 'no grant on test_leg_platform' 'Grants: missing database'
    $problems = @(Get-GrantProblems -Lines ($grants + @('GRANT SELECT ON `mysql`.* TO `leg_platform`@`127.0.0.1`')))
    Assert-Equal ([bool]($problems -contains 'unexpected privileges on mysql')) $true 'Grants: other database refused'
    Assert-Equal ([bool](@(Get-GrantProblems -Lines ($grants + @('GRANT PROXY ON ''root''@''localhost'' TO ''leg_platform''@''127.0.0.1''')) ) -contains 'unexpected grant line')) $true 'Grants: other grant refused'
    Assert-Equal ([bool](((Get-GrantProblems -Lines @("GRANT USAGE ON *.* TO x IDENTIFIED BY PASSWORD '*SECRETHASH'", 'GRANT ALL PRIVILEGES ON *.* TO `leg_platform`@`127.0.0.1`')) -join ';') -notmatch 'SECRETHASH')) $true 'Grants: password hash never reported'
    Assert-Equal @(Get-UnexpectedAccounts -Lines @('root@localhost', 'leg_platform@127.0.0.1') -Additional @('leg_platform@127.0.0.1')).Count 0 'Accounts: application account allowed after its creation'
    Assert-Equal ((Get-UnexpectedAccounts -Lines @('root@localhost', 'leg_platform@127.0.0.1')) -join ',') 'leg_platform@127.0.0.1' 'Accounts: application account refused before its creation'
    Assert-Equal (Get-ServerOptionValue -Entries $switchedEntries -Key 'event-scheduler') 'DISABLED' 'Switched option file: event scheduler disabled'

    # Accounts
    Assert-Equal @(Get-UnexpectedAccounts -Lines @('PUBLIC@', 'mariadb.sys@localhost', 'root@127.0.0.1', 'root@::1', 'root@localhost', '')).Count 0 'Accounts: reserved accounts only'
    Assert-Equal ((Get-UnexpectedAccounts -Lines @('root@localhost', 'root@%', 'root@mypc', '@localhost', 'Root@localhost')) -join ',') 'root@%,root@mypc,@localhost,Root@localhost' 'Accounts: other accounts are reported'

    # Client arguments and environment
    $pipeArgs = @(Get-ClientArguments -Protocol Pipe -PipeName 'MySQL' -User 'root')
    Assert-Equal $pipeArgs[0] '--no-defaults' 'Client: --no-defaults comes first'
    Assert-Equal ($pipeArgs -join ' ') '--no-defaults --protocol=PIPE --socket=MySQL --user=root --batch --skip-column-names' 'Client: named pipe arguments'
    $tcpArgs = @(Get-ClientArguments -Protocol Tcp -User 'leg_platform' -OptionFile 'C:\temp\x.cnf')
    Assert-Equal ($tcpArgs -join ' ') "--defaults-file=C:\temp\x.cnf --protocol=TCP --host=127.0.0.1 --port=$DatabasePort --user=leg_platform --batch --skip-column-names" 'Client: TCP arguments with option file first'
    Assert-Throws { Get-ClientArguments -Protocol Pipe -PipeName 'My SQL' -User 'root' } '*Invalid pipe name*' 'Client: invalid pipe name is refused'
    [Environment]::SetEnvironmentVariable('MYSQL_HOST', 'test.invalid', 'Process')
    [Environment]::SetEnvironmentVariable('MARIADB_HOME', 'C:\test.invalid', 'Process')
    Clear-ClientEnvironment
    Assert-Equal (Test-Path -LiteralPath 'Env:MYSQL_HOST') $false 'Client environment: MYSQL_HOST removed'
    Assert-Equal (Test-Path -LiteralPath 'Env:MARIADB_HOME') $false 'Client environment: MARIADB_HOME removed'

    # Installation state (mocked; no real installation is read)
    function New-MockState {
        param($Changes = @{})
        $values = @{
            ServiceExists = $false; ServiceRunning = $false; ServiceIsLegacy = $false; OwnershipProblem = $null
            ServiceIsMariaDb = $false; OtherServices = @(); PackageVersion = $null; OldPackageRootExists = $false
            OldDataDirectoryExists = $false; MariaDbEntries = @(); MariaDbPackageVersion = $null; MariaDbMetaVersion = $null
        }
        foreach ($key in $Changes.Keys) { $values[$key] = $Changes[$key] }
        return [pscustomobject]$values
    }
    function Merge-Table {
        param([hashtable]$First, [hashtable]$Second)
        $result = @{}
        foreach ($table in @($First, $Second)) { foreach ($key in $table.Keys) { $result[$key] = $table[$key] } }
        return $result
    }
    $mariaDbEntry = [pscustomobject]@{ DisplayName = 'MariaDB 12.3 (x64)'; InstallLocation = 'C:\Program Files\MariaDB 12.3\' }
    $installed = @{ ServiceExists = $true; ServiceIsMariaDb = $true; MariaDbEntries = @($mariaDbEntry); MariaDbPackageVersion = '12.3.3' }
    Assert-Equal (Get-InstallationKind -State (New-MockState)) 'None' 'Kind: nothing installed'
    Assert-Equal (Get-InstallationKind -State (New-MockState @{ OldPackageRootExists = $true })) 'Legacy' 'Kind: C:\tools\mysql'
    Assert-Equal (Get-InstallationKind -State (New-MockState @{ OldDataDirectoryExists = $true })) 'Legacy' 'Kind: C:\ProgramData\MySQL\data'
    Assert-Equal (Get-InstallationKind -State (New-MockState @{ PackageVersion = '9.7.2' })) 'Legacy' 'Kind: package mysql'
    Assert-Equal (Get-InstallationKind -State (New-MockState @{ ServiceExists = $true; ServiceIsLegacy = $true })) 'Legacy' 'Kind: service of the earlier installation'
    Assert-Equal (Get-InstallationKind -State (New-MockState @{ ServiceExists = $true })) 'Foreign' 'Kind: other service MySQL'
    Assert-Equal (Get-InstallationKind -State (New-MockState @{ OtherServices = @('MariaDB') })) 'Foreign' 'Kind: service MariaDB'
    Assert-Equal (Get-InstallationKind -State (New-MockState $installed)) 'MariaDb' 'Kind: MariaDB installation'
    Assert-Equal (Get-InstallationKind -State (New-MockState @{ MariaDbEntries = @($mariaDbEntry) })) 'MariaDb' 'Kind: MariaDB without service'
    Assert-Equal (Get-InstallationKind -State (New-MockState (Merge-Table $installed @{ OldDataDirectoryExists = $true }))) 'Legacy' 'Kind: legacy wins over MariaDB'
    $pf = 'C:\Program Files'
    Assert-Equal @(Get-MariaDbRemovalProblems -State (New-MockState $installed) -ProgramFiles $pf).Count 0 'Removal: own MariaDB installation'
    Assert-Equal ([bool](@(Get-MariaDbRemovalProblems -State (New-MockState (Merge-Table $installed @{ MariaDbPackageVersion = $null })) -ProgramFiles $pf) -like '*not installed*')) $true 'Removal: not installed by Chocolatey is refused'
    $elsewhere = [pscustomobject]@{ DisplayName = 'MariaDB 12.3 (x64)'; InstallLocation = 'C:\MariaDB' }
    Assert-Equal ([bool](@(Get-MariaDbRemovalProblems -State (New-MockState (Merge-Table $installed @{ MariaDbEntries = @($elsewhere) })) -ProgramFiles $pf) -like '*not below*')) $true 'Removal: folder outside Program Files is refused'
    Assert-Equal ([bool](@(Get-MariaDbRemovalProblems -State (New-MockState (Merge-Table $installed @{ MariaDbEntries = @($mariaDbEntry, $mariaDbEntry) })) -ProgramFiles $pf) -like '*exactly one*')) $true 'Removal: two installations are refused'
    Assert-Equal ([bool](@(Get-MariaDbRemovalProblems -State (New-MockState (Merge-Table $installed @{ ServiceIsMariaDb = $false })) -ProgramFiles $pf) -like '*does not run*')) $true 'Removal: foreign service is refused'
    Assert-Equal (Test-ServiceBelongsToMariaDb -Executable 'C:\Program Files\MariaDB 12.3\bin\mysqld.exe' -Entries @($mariaDbEntry)) $true 'Service: program of the MariaDB installation'
    Assert-Equal (Test-ServiceBelongsToMariaDb -Executable 'C:\tools\mysql\current\bin\mysqld.exe' -Entries @($mariaDbEntry)) $false 'Service: other program'
    Assert-Equal (Test-ServiceBelongsToMariaDb -Executable $null -Entries @($mariaDbEntry)) $false 'Service: unresolved program'

    # Select-MariaDbUninstallEntries: the hidden MSI entry is a duplicate only next to a matching visible entry.
    $mariaDbName = 'MariaDB 12.3 (x64)'
    $mariaDbLocation = 'C:\Program Files\MariaDB 12.3\'
    $visibleEntry = [pscustomobject]@{ DisplayName = $mariaDbName; InstallLocation = $mariaDbLocation; Hidden = $false }
    $hiddenEntry = [pscustomobject]@{ DisplayName = $mariaDbName; InstallLocation = $mariaDbLocation; Hidden = $true }
    $hiddenNoSlash = [pscustomobject]@{ DisplayName = $mariaDbName; InstallLocation = 'C:\Program Files\MariaDB 12.3'; Hidden = $true }
    $hiddenElsewhere = [pscustomobject]@{ DisplayName = $mariaDbName; InstallLocation = 'D:\MariaDB 12.3\'; Hidden = $true }
    $hiddenNoLocation = [pscustomobject]@{ DisplayName = $mariaDbName; InstallLocation = ''; Hidden = $true }
    $hiddenOtherCase = [pscustomobject]@{ DisplayName = 'MariaDB 12.3 (X64)'; InstallLocation = $mariaDbLocation; Hidden = $true }
    $selectedEntries = @(Select-MariaDbUninstallEntries -Entries @($visibleEntry, $hiddenEntry))
    Assert-Equal $selectedEntries.Count 1 'Uninstall entries: hidden duplicate of a visible entry skipped'
    Assert-Equal (($selectedEntries | ForEach-Object { "$($_.DisplayName)|$($_.InstallLocation)|$(@($_.PSObject.Properties).Count)" }) -join ';') "$mariaDbName|$mariaDbLocation|2" 'Uninstall entries: visible entry returned with DisplayName and InstallLocation only'
    Assert-Equal @(Select-MariaDbUninstallEntries -Entries @($hiddenNoSlash, $visibleEntry)).Count 1 'Uninstall entries: trailing separator ignored'
    Assert-Equal @(Select-MariaDbUninstallEntries -Entries @($visibleEntry, $hiddenElsewhere)).Count 2 'Uninstall entries: hidden entry at another location counted'
    Assert-Equal @(Select-MariaDbUninstallEntries -Entries @($hiddenEntry)).Count 1 'Uninstall entries: hidden entry alone counted'
    Assert-Equal @(Select-MariaDbUninstallEntries -Entries @($visibleEntry, $visibleEntry)).Count 2 'Uninstall entries: two visible entries counted'
    Assert-Equal @(Select-MariaDbUninstallEntries -Entries @($visibleEntry, $hiddenNoLocation)).Count 2 'Uninstall entries: hidden entry without location counted'
    Assert-Equal @(Select-MariaDbUninstallEntries -Entries @($visibleEntry, $hiddenOtherCase)).Count 2 'Uninstall entries: hidden entry with other name case counted'
    Assert-Equal @(Select-MariaDbUninstallEntries -Entries @()).Count 0 'Uninstall entries: none'
    $visibleNoLocation = [pscustomobject]@{ DisplayName = $mariaDbName; InstallLocation = ''; Hidden = $false }
    Assert-Equal @(Select-MariaDbUninstallEntries -Entries @($visibleNoLocation, $hiddenEntry)).Count 2 'Uninstall entries: visible entry without location matches nothing'
    Assert-Equal @(Select-MariaDbUninstallEntries -Entries @($visibleEntry, $hiddenEntry, $hiddenNoSlash)).Count 2 'Uninstall entries: one visible entry absorbs only one hidden entry'
    Assert-Equal @(Select-MariaDbUninstallEntries -Entries @($hiddenEntry, $visibleEntry, $visibleEntry, $hiddenEntry)).Count 2 'Uninstall entries: two installations with two entries each'

    # Earlier MySQL installation: ownership of the service and manual removal commands
    $toolsRoot = Join-Path $root 'tools'
    $ownIni = Join-Path $toolsRoot 'mysql\current\my.ini'
    Assert-Equal (Get-ServiceOwnershipProblem -PathName "$plainBase MySQL" -Executable $plainExe -ToolsRoot $toolsRoot) $null 'Ownership: earlier installation without --defaults-file'
    Assert-Equal (Get-ServiceOwnershipProblem -PathName "`"$plainExe`" --defaults-file=`"$ownIni`" MySQL" -Executable $plainExe -ToolsRoot $toolsRoot) $null 'Ownership: earlier installation, its own --defaults-file'
    Assert-Equal ([bool](Get-ServiceOwnershipProblem -PathName "`"$plainExe`" --defaults-file=C:\other\my.ini MySQL" -Executable $plainExe -ToolsRoot $toolsRoot)) $true 'Ownership: other --defaults-file is refused'
    Assert-Equal ([bool](Get-ServiceOwnershipProblem -PathName $mariaDbPathName -Executable $spaceExe -ToolsRoot $toolsRoot)) $true 'Ownership: MariaDB service is not the earlier installation'
    Assert-Equal ([bool](Get-ServiceOwnershipProblem -PathName 'C:\missing\mysqld MySQL' -Executable $null -ToolsRoot $toolsRoot)) $true 'Ownership: unresolved executable is refused'
    $legacy = New-MockState @{ ServiceExists = $true; ServiceIsLegacy = $true; PackageVersion = '9.7.2'; OldPackageRootExists = $true; OldDataDirectoryExists = $true }
    $commands = @(Get-LegacyRemovalCommands -State $legacy -ReparsePaths @())
    Assert-Equal ([bool]($commands[1] -like "Get-Item -LiteralPath *'C:\ProgramData\MySQL\data'*ReparsePoint*")) $true 'Manual removal: first command checks for reparse points'
    foreach ($expected in @('Stop-Service -Name MySQL -Force', 'sc.exe config MySQL start= disabled', 'choco uninstall mysql -y', 'sc.exe delete MySQL', "[IO.Directory]::Delete('$(Get-OldPackageRoot)', `$true)", "[IO.Directory]::Delete('C:\ProgramData\MySQL\data', `$true)", "[IO.Directory]::Delete('C:\ProgramData\MySQL')")) {
        Assert-Equal ([bool]($commands -ccontains $expected)) $true "Manual removal: $expected"
    }
    Assert-Equal ([bool]($commands -ccontains "[IO.Directory]::Delete('C:\ProgramData\MySQL', `$true)")) $false 'Manual removal: C:\ProgramData\MySQL is never deleted recursively'
    Assert-Equal ([bool]($commands | Where-Object { $_ -like '*Remove-Item*' -or $_ -like '*LEG-MySQL*' })) $false 'Manual removal: no Remove-Item, no C:\LEG-MySQL'
    $foreign = New-MockState @{ ServiceExists = $true; OwnershipProblem = 'other installation'; PackageVersion = '9.7.2'; OldDataDirectoryExists = $true }
    $commands = @(Get-LegacyRemovalCommands -State $foreign -ReparsePaths @())
    Assert-Equal ([bool]($commands | Where-Object { $_ -like '*Stop-Service*' -or $_ -like 'sc.exe*' -or $_ -like 'choco*' })) $false 'Manual removal: no service or package commands for a foreign service'
    Assert-Equal ([bool]($commands -ccontains "[IO.Directory]::Delete('C:\ProgramData\MySQL\data', `$true)")) $true 'Manual removal: data folder of the earlier installation with a foreign service'
    Assert-Equal @(Get-LegacyRemovalCommands -State $legacy -ReparsePaths @('C:\tools')).Count 0 'Manual removal: no commands if a folder is a reparse point'

    # The printed data commands keep other MySQL installations below C:\ProgramData\MySQL
    $mockProgramData = Join-Path $root 'ProgramData\MySQL'
    New-Item -ItemType Directory -Path (Join-Path $mockProgramData 'data\mysql'), (Join-Path $mockProgramData 'MySQL Server 9.7\Data') | Out-Null
    $savedParent = $OldDataParent
    $savedData = $OldDataDirectory
    $OldDataParent = $mockProgramData
    $OldDataDirectory = Join-Path $mockProgramData 'data'
    $dataCommands = @(Get-LegacyRemovalCommands -State (New-MockState @{ OldDataDirectoryExists = $true }) -ReparsePaths @() | Where-Object { $_ -like '`[IO.Directory`]::Delete*' })
    Assert-Equal $dataCommands.Count 2 'Data commands: data folder and optional parent'
    Invoke-Expression $dataCommands[0]
    $parentFailed = $false
    try { Invoke-Expression $dataCommands[1] } catch { $parentFailed = $true }
    Assert-Equal (Test-Path -LiteralPath $OldDataDirectory) $false 'Data commands: data folder deleted'
    Assert-Equal $parentFailed $true 'Data commands: non-empty parent is not deleted'
    Assert-Equal (Test-Path -LiteralPath (Join-Path $mockProgramData 'MySQL Server 9.7\Data')) $true 'Data commands: other installation kept'
    $OldDataParent = $savedParent
    $OldDataDirectory = $savedData
    Assert-Equal (Get-ChocolateyToolsRoot) $(if ([Environment]::GetEnvironmentVariable('ChocolateyToolsLocation', 'Machine')) { [Environment]::GetEnvironmentVariable('ChocolateyToolsLocation', 'Machine') } else { 'C:\tools' }) 'Tools root: machine value or C:\tools'

    # PATH helpers on strings (the registry is not touched)
    $pathValue = 'C:\a;C:\tools\mysql\current\bin;%USERPROFILE%\bin;;C:\TOOLS\MySQL\current\bin\;C:\b;C:\Program Files\MariaDB 12.3\bin'
    Assert-Equal (Remove-PathListEntry -Value $pathValue -Directories @($LegacyPathEntry)) 'C:\a;%USERPROFILE%\bin;;C:\b;C:\Program Files\MariaDB 12.3\bin' 'PATH: legacy entry removed in all spellings, others kept'
    Assert-Equal ((Get-MySqlPathEntries -Value $pathValue) -join '|') 'C:\tools\mysql\current\bin|C:\TOOLS\MySQL\current\bin\' 'PATH: MySQL bin entries are found, MariaDB is not reported'

    # Option files and Chocolatey output
    $presentFile = Join-Path $root 'my.cnf'
    Set-Content -LiteralPath $presentFile -Value ''
    Assert-Equal ((Get-PresentOptionFiles -Paths @($presentFile, (Join-Path $root 'missing.cnf'), '')) -join ',') $presentFile 'Option files: only existing files are reported'
    Assert-Equal (ConvertFrom-ChocolateyListOutput -Lines @('mariadb.install|12.3.3') -Name 'mariadb.install') '12.3.3' 'Chocolatey list: installed'
    Assert-Equal (ConvertFrom-ChocolateyListOutput -Lines @('mariadb|12.3.3') -Name 'mariadb.install') $null 'Chocolatey list: other package only'
    Assert-Equal (ConvertFrom-ChocolateyListOutput -Lines @('Chocolatey v2.2.2', 'mysql|9.7.2') -Name 'mysql') '9.7.2' 'Chocolatey list: ignores other lines'
    Assert-Equal $MsiInstallArguments 'SKIPNETWORKING=1' 'MSI arguments: networking skipped, no password'

    # Get-RootPasswordProblem
    Assert-Equal (Get-RootPasswordProblem -Password 'short') "Use at least 15 characters." 'Password: too short'
    Assert-Equal (Get-RootPasswordProblem -Password 'abcdefghijklmno#') 'The password must not contain # " '' \ or ;.' 'Password: forbidden character'
    Assert-Equal (Get-RootPasswordProblem -Password ' abcdefghijklmnop') 'The password must not start or end with a space.' 'Password: leading space'
    Assert-Equal (Get-RootPasswordProblem -Password "abcdefghijklmnop$([char]0xE4)") 'Use only printable ASCII characters (no umlauts or other non-ASCII characters).' 'Password: non-ASCII'
    Assert-Equal (Get-RootPasswordProblem -Password 'correct horse battery staple') $null 'Password: acceptable'
    Assert-Equal ((New-RandomSecret) -cmatch '^[A-Za-z0-9_-]{43}$') $true 'Random secret: URL-safe Base64 without padding'

    # The temporary option file can only be created with the owner Administrators when elevated.
    $elevated = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    if ($elevated) {
        $tempFile = New-ProtectedTemporaryFile -Content "[client]`npassword=test`n"
        try {
            $tempAcl = Get-Acl -LiteralPath $tempFile
            Assert-Equal $tempAcl.GetOwner([Security.Principal.SecurityIdentifier]).Value $SidAdministrators 'Temporary option file: owner Administrators'
            Assert-Equal (($tempAcl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]) | ForEach-Object { $_.IdentityReference.Value } | Sort-Object) -join ',') ((@($SidAdministrators, $SidSystem) | Sort-Object) -join ',') 'Temporary option file: Administrators and SYSTEM only'
            Assert-Equal ([IO.File]::ReadAllText($tempFile)) "[client]`npassword=test`n" 'Temporary option file: content'
        } finally {
            Remove-Item -LiteralPath $tempFile -Force -ErrorAction SilentlyContinue
        }
    } else {
        Write-Host 'SKIPPED: creation of the temporary option file (needs an elevated PowerShell).'
    }
} finally {
    Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host "$script:passed passed, $script:failures failed."
if ($script:failures -gt 0) { exit 1 }
exit 0
