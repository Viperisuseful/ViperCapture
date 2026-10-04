# Install the vipercapture command for the current Windows user.
#
# PowerShell:
#   irm https://raw.githubusercontent.com/Viperisuseful/ViperCapture/master/scripts/install.ps1 | iex
#
# Re-running the command updates the app files and keeps an existing .venv.
# $env:VIPERCAPTURE_REF selects a GitHub branch. $env:VIPERCAPTURE_ARCHIVE_URL
# overrides the zip. $env:VIPERCAPTURE_SOURCE installs a local checkout instead.
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$Ref = if ($env:VIPERCAPTURE_REF) { $env:VIPERCAPTURE_REF } else { "master" }
$Prefix = if ($env:VIPERCAPTURE_HOME) { $env:VIPERCAPTURE_HOME } else { Join-Path $env:LOCALAPPDATA "ViperCapture" }
$App = Join-Path $Prefix "app"
$BinDir = if ($env:VIPERCAPTURE_BIN_DIR) { $env:VIPERCAPTURE_BIN_DIR } else { Join-Path $Prefix "bin" }
$ArchiveUrl = if ($env:VIPERCAPTURE_ARCHIVE_URL) {
    $env:VIPERCAPTURE_ARCHIVE_URL
} else {
    "https://github.com/Viperisuseful/ViperCapture/archive/refs/heads/$Ref.zip"
}

function Test-Python311 {
    param([string]$Executable, [string[]]$PrefixArgs = @())
    & $Executable @PrefixArgs -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
    return $LASTEXITCODE -eq 0
}

function Resolve-Python {
    if ($env:VIPERCAPTURE_PYTHON) {
        if (-not (Test-Python311 $env:VIPERCAPTURE_PYTHON @())) {
            throw "VIPERCAPTURE_PYTHON is not Python 3.11 or newer: $($env:VIPERCAPTURE_PYTHON)"
        }
        return $env:VIPERCAPTURE_PYTHON
    }
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py -and (Test-Python311 $py.Source @("-3"))) {
        $resolved = & $py.Source -3 -c "import sys; print(sys.executable)"
        if ($LASTEXITCODE -eq 0 -and $resolved) { return $resolved.Trim() }
    }
    foreach ($name in @("python", "python3")) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd -and (Test-Python311 $cmd.Source @())) { return $cmd.Source }
    }
    Write-Host "  Python 3.11+ was not found. Installing uv to provide Python 3.12..."
    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if (-not $uv) {
        irm https://astral.sh/uv/install.ps1 | iex
        $uvPath = Join-Path $env:USERPROFILE ".local\bin\uv.exe"
        if (Test-Path $uvPath) { $uv = Get-Command $uvPath }
    }
    if (-not $uv) { throw "Install Python 3.11 or newer, then run this installer again." }
    & $uv.Source python install 3.12
    $found = (& $uv.Source python find 3.12).Trim()
    if (-not $found) { throw "uv did not provide Python 3.12." }
    return $found
}

function Copy-FilteredTree {
    param([string]$Source, [string]$Destination)
    $exclude = @(".git", ".venv", "node_modules", "__pycache__", ".pytest_cache")
    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    Get-ChildItem -Force $Source | Where-Object { $exclude -notcontains $_.Name } | ForEach-Object {
        $target = Join-Path $Destination $_.Name
        if ($_.PSIsContainer) {
            Copy-FilteredTree $_.FullName $target
        } else {
            Copy-Item $_.FullName -Destination $target -Force
        }
    }
}

function Copy-SourceTree {
    param([string]$Source, [string]$Destination)
    $saved = $null
    if (Test-Path (Join-Path $Destination ".venv")) {
        $saved = Join-Path ([System.IO.Path]::GetTempPath()) ("vipercapture-venv-" + [guid]::NewGuid().ToString("n"))
        New-Item -ItemType Directory -Path $saved | Out-Null
        Move-Item (Join-Path $Destination ".venv") (Join-Path $saved "venv")
    }
    try {
        if (Test-Path $Destination) { Remove-Item -Recurse -Force $Destination }
        Copy-FilteredTree $Source $Destination
        if ($saved) {
            Move-Item (Join-Path $saved "venv") (Join-Path $Destination ".venv")
            Remove-Item -Recurse -Force $saved
            $saved = $null
        }
    } catch {
        if ($saved) {
            New-Item -ItemType Directory -Force -Path $Destination | Out-Null
            if (-not (Test-Path (Join-Path $Destination ".venv"))) {
                Move-Item (Join-Path $saved "venv") (Join-Path $Destination ".venv")
            }
            Remove-Item -Recurse -Force $saved -ErrorAction SilentlyContinue
        }
        throw
    }
    if (-not (Test-Path (Join-Path $Destination "launch.py"))) {
        throw "The installed files do not include launch.py."
    }
}

Write-Host ""
Write-Host "  ViperCapture installer"
Write-Host "  ----------------------"
Write-Host ""

$Python = Resolve-Python
$Temp = Join-Path ([System.IO.Path]::GetTempPath()) ("vipercapture-install-" + [guid]::NewGuid().ToString("n"))
New-Item -ItemType Directory -Path $Temp | Out-Null
try {
    if ($env:VIPERCAPTURE_SOURCE) {
        $Source = $env:VIPERCAPTURE_SOURCE
        if (-not (Test-Path (Join-Path $Source "launch.py"))) {
            throw "VIPERCAPTURE_SOURCE does not contain launch.py: $Source"
        }
        Write-Host "  Installing from $Source"
    } elseif ($PSCommandPath -and (Test-Path (Join-Path (Split-Path $PSCommandPath -Parent) "..\launch.py"))) {
        $Source = (Resolve-Path (Join-Path (Split-Path $PSCommandPath -Parent) "..")).Path
        Write-Host "  Installing from $Source"
    } else {
        Write-Host "  Downloading ViperCapture ($Ref)..."
        $Zip = Join-Path $Temp "src.zip"
        Invoke-WebRequest -Uri $ArchiveUrl -OutFile $Zip
        Expand-Archive -Path $Zip -DestinationPath $Temp
        $Extracted = Get-ChildItem $Temp -Directory | Select-Object -First 1
        if (-not $Extracted -or -not (Test-Path (Join-Path $Extracted.FullName "launch.py"))) {
            throw "The downloaded archive does not include launch.py."
        }
        $Source = $Extracted.FullName
    }
    Copy-SourceTree $Source $App
} finally {
    if (Test-Path $Temp) { Remove-Item -Recurse -Force $Temp }
}

New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
$Shim = Join-Path $BinDir "vipercapture.cmd"
$Launch = Join-Path $App "launch.py"
@(
    "@echo off"
    "`"$Python`" `"$Launch`" %*"
) | Set-Content -Path $Shim -Encoding Default

$UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
if (-not $UserPath) { $UserPath = "" }
$Parts = $UserPath -split ";" | Where-Object { $_ -and ($_.TrimEnd("\") -ne $BinDir.TrimEnd("\")) }
$Updated = (@($BinDir) + $Parts) -join ";"
if ($Updated -ne $UserPath) {
    [Environment]::SetEnvironmentVariable("Path", $Updated, "User")
    Write-Host "  Added $BinDir to your user Path."
    Write-Host "  Open a new terminal so vipercapture is on Path."
}

Write-Host ""
Write-Host "  Installed the vipercapture command."
Write-Host "  Run: vipercapture"
Write-Host "  The first launch installs dependencies and browsers, then starts the API."
Write-Host ""
