<#
.SYNOPSIS
    Holds the test-run mutex while a disposable D-106 browser fixture is running.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$StateDirectory,
    [int]$Port = 8010,
    [ValidateSet('first-login', 'ready')][string]$AdministratorState = 'first-login'
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location -LiteralPath $root
$mutex = New-Object System.Threading.Mutex($false, 'Global\LegPlatformTestRun')
$held = $false
try {
    try { $held = $mutex.WaitOne(0) }
    catch [System.Threading.AbandonedMutexException] { $held = $true }
    if (-not $held) {
        [Console]::Error.WriteLine('Another test process holds the database lock.')
        exit 3
    }
    $active = @(Get-CimInstance Win32_Process | Where-Object {
        $_.Name -eq 'python.exe' -and $_.CommandLine -match 'manage\.py"?\s+test\b'
    })
    if ($active.Count) {
        [Console]::Error.WriteLine('A manual test run is active; no fixture was started.')
        exit 3
    }
    & "$root\.venv\Scripts\python.exe" "$PSScriptRoot\d106_browser_fixture.py" --state-directory $StateDirectory --port $Port --administrator-state $AdministratorState
    $code = $LASTEXITCODE
}
finally {
    if ($held) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
exit $code
