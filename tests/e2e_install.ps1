# End to end on Windows: run install.ps1 the way the one-line install does
# (piped into Invoke-Expression), with a local claude_pace.py and a fake
# user profile, run the status line it installed through Git Bash and
# PowerShell, then uninstall and check everything is gone.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File tests\e2e_install.ps1 [-Python C:\path\python.exe]
#   pwsh -NoProfile -File tests\e2e_install.ps1
param([string]$Python = 'python')

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$py = (Get-Command $Python -CommandType Application | Select-Object -First 1).Source
$fake = Join-Path ([IO.Path]::GetTempPath()) ("cu-e2e-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
$saved = @{}
foreach ($k in 'USERPROFILE', 'HOME', 'CLAUDE_CONFIG_DIR', 'CLAUDE_PACE_DIR', 'CLAUDE_PACE_SOURCE',
               'CLAUDE_PACE_SKIP_CHECKSUM', 'CLAUDE_PACE_PYTHON', 'CLAUDE_PACE_NO_ACCOUNT',
               'CLAUDE_PACE_ARGS', 'CLAUDE_PACE_UNINSTALL') {
    $saved[$k] = [Environment]::GetEnvironmentVariable($k)
}
try {
    New-Item -ItemType Directory -Force -Path $fake | Out-Null
    $env:USERPROFILE = $fake
    $env:HOME = $fake
    $env:CLAUDE_CONFIG_DIR = $null
    $env:CLAUDE_PACE_DIR = $null
    $env:CLAUDE_PACE_SOURCE = Join-Path $root 'claude_pace.py'
    $env:CLAUDE_PACE_SKIP_CHECKSUM = '1'
    $env:CLAUDE_PACE_PYTHON = $py
    $env:CLAUDE_PACE_NO_ACCOUNT = '1'
    # The desktop shortcut may land on the real desktop, so skip it unless on CI.
    $env:CLAUDE_PACE_ARGS = if ($env:CI) { '' } else { '--no-launcher' }

    Get-Content -Raw -LiteralPath (Join-Path $root 'install.ps1') | Invoke-Expression

    $settings = Join-Path $fake '.claude\settings.json'
    if (-not (Test-Path -LiteralPath $settings)) { throw 'no settings.json written' }
    & $py (Join-Path $root 'tests\run_status_command.py') $settings (Join-Path $fake '.claude')
    if ($LASTEXITCODE -ne 0) { throw 'installed status line failed' }

    # The one-run settings must not stay set in this window afterwards.
    if ($env:CLAUDE_PACE_ARGS) { throw 'CLAUDE_PACE_ARGS left set after install' }

    # Uninstall the way the README says: the setting, then the same one-liner.
    $env:CLAUDE_PACE_UNINSTALL = '1'
    Get-Content -Raw -LiteralPath (Join-Path $root 'install.ps1') | Invoke-Expression
    if ($env:CLAUDE_PACE_UNINSTALL) { throw 'CLAUDE_PACE_UNINSTALL left set - the next install would uninstall' }
    if (Test-Path -LiteralPath $settings) {
        $d = Get-Content -Raw -LiteralPath $settings | ConvertFrom-Json
        if ($d.PSObject.Properties.Name -contains 'statusLine') { throw 'statusLine left behind after uninstall' }
    }
    if (Test-Path -LiteralPath (Join-Path $fake '.claude-pace')) { throw '.claude-pace left behind' }
    Write-Output 'e2e install/uninstall ok'
} finally {
    foreach ($k in $saved.Keys) { [Environment]::SetEnvironmentVariable($k, $saved[$k]) }
    Remove-Item -LiteralPath $fake -Recurse -Force -ErrorAction SilentlyContinue
}
