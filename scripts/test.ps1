<#
.SYNOPSIS
    Runs only the tests of the area that a change touches (docs/STARTUP.md, section 13; D-071).

.DESCRIPTION
    The areas (source paths -> test labels) are defined in scripts/test_areas.py. This script
    resolves the areas, then runs `manage.py test` with the same interpreter and settings as CI
    and docs/STARTUP.md: .venv\Scripts\python.exe, leg_platform.test_settings, --noinput,
    deprecation warnings as errors.

    The full suite runs only with -Full, or when a changed file is a core file or is not mapped
    to any area (the safe default). A full local run belongs to the team lead, at most once per
    commit block, before a deployment gate, and in CI.

    One test run at a time against the MariaDB server (D-046): two runs drop each other's
    test database. The script takes a machine-wide named mutex (Global\LegPlatformTestRun) for the
    whole run and refuses to start while another run of this script holds it; it also refuses
    while a `manage.py test` process is visible (a run started by hand). Limits that remain
    (D-072): the lock protects only against another start of this script on the same machine; a
    run started by hand with another command line, a run on another machine against the same
    MariaDB server, and the processes of another user that a non-elevated shell cannot read are
    not detected (a warning is printed when such processes are seen). A crashed run releases the
    mutex, because Windows releases it when the process ends.

    Exit codes: 0 all tests and checks passed; 1 a test or a check failed (the exit code of the
    Django test runner is passed through, so is any other non-zero code of Python, for example
    an argument error of the area script); 2 wrong usage or missing virtual environment;
    3 another test run is active (mutex held or process found). Nothing runs in the cases 2 and 3.

.PARAMETER Area
    One or more area names (comma separated). See -List.
.PARAMETER Changed
    Map the changed files (git status: working tree, staged, untracked) to areas.
.PARAMETER Since
    A revision (for example HEAD~3): additionally map the files changed since it.
.PARAMETER List
    Print the areas and exit.
.PARAMETER Full
    Run the whole suite (always with a fresh test database).
.PARAMETER KeepDb
    Local opt-in: pass --keepdb (reuse test_<DB_NAME> between runs). Ignored, with a warning, when
    the plan needs a fresh database (migration areas and the full suite), so that an old test
    database can never mask a migration problem.
.PARAMETER Parallel
    Pass --parallel N. Not supported with the application account of the local setup (see
    docs/STARTUP.md, section 13); only for a MariaDB account that may create test_<DB_NAME>_N.
.PARAMETER Durations
    Pass --durations N (Django 6.1: the N slowest tests).
.PARAMETER Timing
    Pass --timing (database set-up and total time).
.PARAMETER DryRun
    Print the plan and the commands, run nothing.

.EXAMPLE
    .\scripts\test.ps1 -List
.EXAMPLE
    .\scripts\test.ps1 -Changed
.EXAMPLE
    .\scripts\test.ps1 -Area registration,mail -Timing
.EXAMPLE
    .\scripts\test.ps1 -Area translations -KeepDb
.EXAMPLE
    .\scripts\test.ps1 -Full -Durations 50
#>
[CmdletBinding()]
param(
    [string[]]$Area,
    [switch]$Changed,
    [string]$Since,
    [switch]$List,
    [switch]$Full,
    [switch]$KeepDb,
    [int]$Parallel = 0,
    [int]$Durations = 0,
    [switch]$Timing,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

# The documented exit codes: a message on the error stream, then exit. Write-Error would throw
# under $ErrorActionPreference = 'Stop' and the script would end with exit code 1 instead.
function Stop-Script {
    param([string]$Message, [int]$Code)
    [Console]::Error.WriteLine($Message)
    exit $Code
}

$Root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $Root

$Python = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Python)) {
    Stop-Script 'The virtual environment is missing: .venv\Scripts\python.exe (docs/STARTUP.md, section 3).' 2
}
$AreasScript = Join-Path $PSScriptRoot 'test_areas.py'

if ($List) {
    & $Python $AreasScript list
    exit $LASTEXITCODE
}

if (-not ($Area -or $Changed -or $Since -or $Full)) {
    Stop-Script 'Give -Area <name>[,<name>], -Changed, -Since <revision>, -Full or -List. Example: .\scripts\test.ps1 -Changed' 2
}

$planArguments = @('plan')
foreach ($name in @($Area)) { if ($name) { $planArguments += @('--area', $name) } }
if ($Changed) { $planArguments += '--changed' }
if ($Since) { $planArguments += @('--since', $Since) }
if ($Full) { $planArguments += '--full' }

$json = & $Python $AreasScript @planArguments
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$plan = $json | ConvertFrom-Json

if ($plan.mode -eq 'nothing') {
    Write-Host 'No changed files: nothing to run.'
    exit 0
}

Write-Host ('Mode: {0}' -f $plan.mode)
if ($plan.mode -eq 'full') {
    foreach ($reason in @($plan.reasons)) { Write-Host ('  reason: {0}' -f $reason) }
}
else {
    Write-Host ('Areas: {0}' -f $(if (@($plan.areas).Count) { (@($plan.areas) -join ', ') } else { '(changed test modules only)' }))
    Write-Host ('Labels: {0}' -f (@($plan.labels) -join ' '))
}

$useKeepDb = $false
if ($KeepDb) {
    if ($plan.fresh_db) {
        Write-Warning '-KeepDb ignored: this run needs a fresh test database (migrations or the full suite).'
    }
    else {
        $useKeepDb = $true
    }
}

$testArguments = @('-W', 'error::DeprecationWarning', '-W', 'error::PendingDeprecationWarning', 'manage.py', 'test')
if ($plan.mode -eq 'scoped') { $testArguments += @($plan.labels) }
$testArguments += @('--settings=leg_platform.test_settings', '--noinput')
if ($useKeepDb) { $testArguments += '--keepdb' }
if ($Parallel -gt 0) { $testArguments += @('--parallel', "$Parallel") }
if ($Durations -gt 0) { $testArguments += @('--durations', "$Durations") }
if ($Timing) { $testArguments += '--timing' }

if ($DryRun) {
    Write-Host ('python {0}' -f ($testArguments -join ' '))
    foreach ($check in @($plan.checks)) { Write-Host ('python {0}' -f $check) }
    exit 0
}

# One test process at a time (D-046): two runs drop each other's test database. The named mutex
# is the lock between starts of this script (a start at the same moment cannot pass both); the
# process list also catches a run started by hand with `manage.py test`.
$mutex = $null
$lockHeld = $false
try {
    $mutex = New-Object System.Threading.Mutex($false, 'Global\LegPlatformTestRun')
    try {
        $lockHeld = $mutex.WaitOne(0)
    }
    catch [System.Threading.AbandonedMutexException] {
        $lockHeld = $true  # The run that held it ended without releasing it; the lock is ours now.
    }
}
catch {
    Stop-Script ('The test lock could not be taken ({0}); no test run was started (D-046).' -f $_.Exception.GetType().Name) 3
}
if (-not $lockHeld) {
    $mutex.Dispose()
    Stop-Script 'Another test run of this script is active on this machine; wait for it, or stop it if you started it yourself (D-046).' 3
}

$exitCode = 1
try {
    $processes = @(Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' })
    $active = @($processes | Where-Object { $_.CommandLine -match 'manage\.py"?\s+test\b' })
    $unreadable = @($processes | Where-Object { -not $_.CommandLine })
    if ($unreadable.Count -gt 0) {
        Write-Warning ('{0} python process(es) have no readable command line (another user, or no permission): a test run among them is not detected.' -f $unreadable.Count)
    }
    if ($active.Count -gt 0) {
        $exitCode = 3
        [Console]::Error.WriteLine(('Another test run is active (process {0}); wait for it, or stop it if you started it yourself (D-046).' -f $active[0].ProcessId))
    }
    else {
        $stopwatch = [System.Diagnostics.Stopwatch]::StartNew()
        & $Python @testArguments
        $exitCode = $LASTEXITCODE
        if ($plan.mode -eq 'scoped') {
            foreach ($check in @($plan.checks)) {
                Write-Host ('Check: python {0}' -f $check)
                & $Python @($check -split ' ')
                if ($LASTEXITCODE -ne 0) { $exitCode = $LASTEXITCODE }
            }
        }
        Write-Host ('Elapsed: {0:N1} seconds, exit code {1}' -f $stopwatch.Elapsed.TotalSeconds, $exitCode)
    }
}
finally {
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}
exit $exitCode
