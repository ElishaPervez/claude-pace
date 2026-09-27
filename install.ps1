<#
claude-pace - installer for Windows (PowerShell 5.1 or newer).

    irm https://github.com/ElishaPervez/claude-pace/releases/latest/download/install.ps1 | iex

Uninstall:

    $env:CLAUDE_PACE_UNINSTALL=1; irm https://github.com/ElishaPervez/claude-pace/releases/latest/download/install.ps1 | iex

or, from a downloaded copy:  .\install.ps1 -Uninstall [-Purge]

Downloads claude_pace.py into %USERPROFILE%\.claude-pace, checks it against
the release's SHA256SUMS, then runs `claude_pace.py install`, which points
Claude Code's status line at it and adds a `claude-pace` command
(`claude-pace install --desktop` adds a desktop shortcut too). No admin
rights needed.

Settings (environment variables):
    CLAUDE_PACE_VERSION=v1.0.0     install that release instead of the latest
    CLAUDE_PACE_HOME=DIR           where the script lives (default %USERPROFILE%\.claude-pace)
    CLAUDE_PACE_SOURCE=FILE        copy this local claude_pace.py instead of downloading (testing)
    CLAUDE_PACE_SKIP_CHECKSUM=1    don't verify the download (testing)
    CLAUDE_PACE_PYTHON=PATH        use this python.exe instead of searching
    CLAUDE_PACE_BASE_URL=URL       download from here instead of GitHub (testing)
    CLAUDE_PACE_UNINSTALL=1        uninstall instead of installing
    CLAUDE_PACE_ARGS="--no-launcher"  extra options passed to install / uninstall
#>
param(
    [switch]$Uninstall,
    [switch]$Purge,
    [switch]$NoLauncher
)

function Install-ClaudePace {
    param([bool]$DoUninstall, [string[]]$Extra)
    # Set here, not at the top, so `irm | iex` doesn't change the caller's session.
    $ErrorActionPreference = 'Stop'

    $repo = 'ElishaPervez/claude-pace'
    $appDir = if ($env:CLAUDE_PACE_HOME) { $env:CLAUDE_PACE_HOME } else { Join-Path $env:USERPROFILE '.claude-pace' }
    $script = Join-Path $appDir 'claude_pace.py'

    # -- find Python 3.9 or newer, as the full path of the real python.exe --
    # `py -3` is the Python launcher from python.org. Plain `python` may be the
    # Microsoft Store placeholder, which fails when given arguments - asking
    # each candidate for its own path weeds that out.
    # (No quotes inside: PowerShell 5.1 mangles them when passing arguments.)
    $probe = 'import sys; sys.version_info >= (3, 9) and print(sys.executable)'
    $candidates = @()
    if ($env:CLAUDE_PACE_PYTHON) { $candidates += @{ Exe = $env:CLAUDE_PACE_PYTHON; Args = @() } }
    $candidates += @(
        @{ Exe = 'py'; Args = @('-3') },
        @{ Exe = 'python3'; Args = @() },
        @{ Exe = 'python'; Args = @() }
    )
    $py = $null
    foreach ($c in $candidates) {
        if (-not (Get-Command $c.Exe -CommandType Application -ErrorAction SilentlyContinue)) { continue }
        try {
            $out = & $c.Exe @($c.Args + @('-c', $probe)) 2>$null
            if ($LASTEXITCODE -eq 0 -and $out) {
                $path = ([string]($out | Select-Object -Last 1)).Trim()
                if ($path -and (Test-Path -LiteralPath $path)) { $py = $path; break }
            }
        } catch { continue }
    }
    if (-not $py) {
        Write-Host 'Python 3.9 or newer is needed and was not found.' -ForegroundColor Yellow
        Write-Host '  Install it with:  winget install Python.Python.3.13'
        Write-Host '  or from https://www.python.org/downloads/windows/'
        Write-Host 'Then open a new terminal and run this installer again.'
        throw 'Python 3.9+ not found.'
    }

    # -- uninstall --
    if ($DoUninstall) {
        if (Test-Path -LiteralPath $script) {
            & $py $script uninstall @Extra
            if ($LASTEXITCODE -ne 0) { throw "uninstall failed (exit $LASTEXITCODE)." }
        } else {
            Write-Host "Nothing to uninstall: $script isn't there."
        }
        if (Test-Path -LiteralPath $appDir) { Remove-Item -LiteralPath $appDir -Recurse -Force }
        Write-Host "Removed $appDir."
        return
    }

    # -- download --
    # PowerShell 5.1 defaults to old TLS versions that GitHub refuses.
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    $base = if ($env:CLAUDE_PACE_BASE_URL) {
        $env:CLAUDE_PACE_BASE_URL
    } elseif ($env:CLAUDE_PACE_VERSION) {
        "https://github.com/$repo/releases/download/$($env:CLAUDE_PACE_VERSION)"
    } else {
        "https://github.com/$repo/releases/latest/download"
    }

    $madeDir = -not (Test-Path -LiteralPath $appDir)
    New-Item -ItemType Directory -Force -Path $appDir | Out-Null
    $tmp = "$script.download.tmp"
    $sums = Join-Path $appDir 'SHA256SUMS.tmp'
    try {
        if ($env:CLAUDE_PACE_SOURCE) {
            Copy-Item -LiteralPath $env:CLAUDE_PACE_SOURCE -Destination $tmp -Force
        } else {
            Write-Host "Downloading claude_pace.py from $base ..."
            Invoke-WebRequest -UseBasicParsing -Uri "$base/claude_pace.py" -OutFile $tmp
        }

        if ($env:CLAUDE_PACE_SKIP_CHECKSUM -eq '1') {
            Write-Host 'Skipping the checksum check (CLAUDE_PACE_SKIP_CHECKSUM=1).'
        } else {
            Invoke-WebRequest -UseBasicParsing -Uri "$base/SHA256SUMS" -OutFile $sums
            $want = $null
            foreach ($line in Get-Content -LiteralPath $sums) {
                $parts = $line.Trim() -split '\s+'
                if ($parts.Count -ge 2 -and $parts[1].TrimStart('*') -eq 'claude_pace.py') { $want = $parts[0].ToLower() }
            }
            if (-not $want) { throw 'SHA256SUMS has no entry for claude_pace.py.' }
            $got = (Get-FileHash -Algorithm SHA256 -LiteralPath $tmp).Hash.ToLower()
            if ($want -ne $got) { throw 'Checksum mismatch for claude_pace.py - not installing.' }
        }

        Move-Item -LiteralPath $tmp -Destination $script -Force
    } finally {
        foreach ($f in @($tmp, $sums)) {
            if (Test-Path -LiteralPath $f) { Remove-Item -LiteralPath $f -Force }
        }
        # A failed first install leaves no empty folder behind.
        if ($madeDir -and -not (Test-Path -LiteralPath $script) -and
            -not (Get-ChildItem -LiteralPath $appDir -Force -ErrorAction SilentlyContinue)) {
            Remove-Item -LiteralPath $appDir -Force -ErrorAction SilentlyContinue
        }
    }

    # -- hook it into Claude Code --
    & $py $script install --python $py @Extra
    if ($LASTEXITCODE -ne 0) { throw "install failed (exit $LASTEXITCODE)." }
}

$extra = @()
if ($env:CLAUDE_PACE_ARGS) { $extra += ($env:CLAUDE_PACE_ARGS -split '\s+' | Where-Object { $_ }) }
if ($Purge) { $extra += '--purge' }
if ($NoLauncher) { $extra += '--no-launcher' }
$doUninstall = $Uninstall -or $env:CLAUDE_PACE_UNINSTALL -eq '1'
try {
    Install-ClaudePace -DoUninstall $doUninstall -Extra $extra
} finally {
    # These only apply to this one run: left set, running the install line
    # again in the same window would uninstall instead.
    Remove-Item Env:CLAUDE_PACE_UNINSTALL, Env:CLAUDE_PACE_ARGS -ErrorAction SilentlyContinue
}
