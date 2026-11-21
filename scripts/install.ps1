# Install the vipercapture command for the current Windows user.
#
# PowerShell:
#   irm https://raw.githubusercontent.com/Viperisuseful/ViperCapture/master/scripts/install.ps1 | iex
#
# Re-running the command updates the app files and keeps an existing .venv.
# $env:VIPERCAPTURE_REF selects a GitHub branch. $env:VIPERCAPTURE_ARCHIVE_URL
# overrides the zip. $env:VIPERCAPTURE_SOURCE installs a local checkout instead.
& {
    param([string]$ScriptPath)

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

    function Split-PathEntries {
        param([string]$PathValue)
        if (-not $PathValue) { return @() }
        return @($PathValue -split ";" | Where-Object { $_ -and $_.Trim() })
    }

    function Format-NormalizedPathEntry {
        param([string]$Entry)
        return $Entry.Trim().Trim('"').TrimEnd([char[]]@('\', '/'))
    }

    function Test-PathContains {
        param([string[]]$Entries, [string]$Directory)
        $target = Format-NormalizedPathEntry $Directory
        foreach ($entry in $Entries) {
            if ((Format-NormalizedPathEntry $entry) -ieq $target) {
                return $true
            }
        }
        return $false
    }

    function Add-SessionPath {
        param([string]$Directory)
        $entries = Split-PathEntries $env:Path
        if (Test-PathContains $entries $Directory) {
            return $false
        }
        $env:Path = if ($entries.Count -gt 0) {
            (@($Directory) + $entries) -join ";"
        } else {
            $Directory
        }
        return $true
    }

    function Test-Python311 {
        param([string]$Executable, [string[]]$PrefixArgs = @())
        $previousPreference = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        try {
            $global:LASTEXITCODE = 1
            & $Executable @PrefixArgs -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" *> $null
            return $LASTEXITCODE -eq 0
        } catch {
            return $false
        } finally {
            $ErrorActionPreference = $previousPreference
        }
    }

    function Resolve-Python {
        if ($env:VIPERCAPTURE_PYTHON) {
            if (-not (Test-Python311 $env:VIPERCAPTURE_PYTHON @())) {
                throw "VIPERCAPTURE_PYTHON is not Python 3.11 or newer: $($env:VIPERCAPTURE_PYTHON)"
            }
            return $env:VIPERCAPTURE_PYTHON
        }
        $py = Get-Command py -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($py -and $py.Source -and (Test-Python311 $py.Source @("-3"))) {
            $resolved = & $py.Source -3 -c "import sys; print(sys.executable)" | Select-Object -First 1
            if ($LASTEXITCODE -eq 0 -and $resolved) { return ([string]$resolved).Trim() }
        }
        foreach ($name in @("python", "python3")) {
            $commands = Get-Command $name -CommandType Application -All -ErrorAction SilentlyContinue
            foreach ($cmd in $commands) {
                if ($cmd -and $cmd.Source -and (Test-Python311 $cmd.Source @())) {
                    return $cmd.Source
                }
            }
        }
        $uv = Get-Command uv -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if (-not $uv) {
            $uvPath = Join-Path $env:USERPROFILE ".local\bin\uv.exe"
            if (Test-Path $uvPath) { $uv = Get-Command $uvPath -ErrorAction SilentlyContinue }
        }
        if (-not $uv) {
            Write-Host "  Python 3.11+ was not found. Installing uv to provide Python 3.12..."
            irm https://astral.sh/uv/install.ps1 | iex | Out-Host
            $uv = Get-Command uv -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
            if (-not $uv) {
                $uvPath = Join-Path $env:USERPROFILE ".local\bin\uv.exe"
                if (Test-Path $uvPath) { $uv = Get-Command $uvPath -ErrorAction SilentlyContinue }
            }
        } else {
            Write-Host "  Python 3.11+ was not found. Using uv to install Python 3.12..."
        }
        if (-not $uv -or -not $uv.Source) { throw "Install Python 3.11 or newer, then run this installer again." }
        Add-SessionPath (Split-Path $uv.Source -Parent) | Out-Null
        & $uv.Source python install 3.12 | Out-Host
        if ($LASTEXITCODE -ne 0) { throw "uv failed to install Python 3.12." }
        $found = & $uv.Source python find 3.12 | Select-Object -First 1
        if ($LASTEXITCODE -ne 0 -or -not $found) { throw "uv did not provide Python 3.12." }
        return ([string]$found).Trim()
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
        } elseif ($ScriptPath -and (Test-Path (Join-Path (Split-Path $ScriptPath -Parent) "..\launch.py"))) {
            $Source = (Resolve-Path (Join-Path (Split-Path $ScriptPath -Parent) "..")).Path
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

    function Find-CscCompiler {
        foreach ($frameworkDir in @("Microsoft.NET\Framework64", "Microsoft.NET\Framework")) {
            $root = Join-Path $env:SystemRoot $frameworkDir
            if (-not (Test-Path -LiteralPath $root)) { continue }
            $candidate = Get-ChildItem -LiteralPath $root -Directory -Filter "v4.*" -ErrorAction SilentlyContinue |
                Sort-Object Name -Descending |
                ForEach-Object { Join-Path $_.FullName "csc.exe" } |
                Where-Object { Test-Path -LiteralPath $_ } |
                Select-Object -First 1
            if ($candidate) { return $candidate }
        }
        return $null
    }

    function Write-WindowsShim {
        param([string]$BinDir, [string]$Python, [string]$Launch)

        $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
        $exePath = Join-Path $BinDir "vipercapture.exe"
        $configPath = Join-Path $BinDir "vipercapture.shim"
        $cmdPath = Join-Path $BinDir "vipercapture.cmd"

        $csc = Find-CscCompiler
        if ($csc) {
            [System.IO.File]::WriteAllLines($configPath, [string[]]@($Python, $Launch), $utf8NoBom)
            $buildTemp = Join-Path ([System.IO.Path]::GetTempPath()) ("vipercapture-shim-" + [guid]::NewGuid().ToString("n"))
            New-Item -ItemType Directory -Path $buildTemp | Out-Null
            try {
                $csPath = Join-Path $buildTemp "ViperCaptureShim.cs"
                $builtExe = Join-Path $buildTemp "vipercapture.exe"
                $csSource = @'
using System;
using System.Diagnostics;
using System.IO;
using System.Text;

internal static class ViperCaptureShim
{
    private static string QuoteArg(string arg)
    {
        if (arg == null) return "\"\"";
        var sb = new StringBuilder();
        sb.Append('"');
        int backslashes = 0;
        foreach (char c in arg)
        {
            if (c == '\\')
            {
                backslashes++;
            }
            else if (c == '"')
            {
                sb.Append('\\', backslashes * 2 + 1);
                sb.Append('"');
                backslashes = 0;
            }
            else
            {
                if (backslashes > 0)
                {
                    sb.Append('\\', backslashes);
                    backslashes = 0;
                }
                sb.Append(c);
            }
        }
        if (backslashes > 0)
        {
            sb.Append('\\', backslashes * 2);
        }
        sb.Append('"');
        return sb.ToString();
    }

    public static int Main(string[] args)
    {
        string exeDir = Path.GetDirectoryName(typeof(ViperCaptureShim).Assembly.Location);
        string configPath = Path.Combine(exeDir, "vipercapture.shim");
        if (!File.Exists(configPath))
        {
            Console.Error.WriteLine("Missing ViperCapture shim config: " + configPath);
            return 1;
        }
        string[] lines = File.ReadAllLines(configPath, new UTF8Encoding(false));
        if (lines.Length < 2 || string.IsNullOrWhiteSpace(lines[0]) || string.IsNullOrWhiteSpace(lines[1]))
        {
            Console.Error.WriteLine("Invalid ViperCapture shim config: " + configPath);
            return 1;
        }
        string python = lines[0].Trim();
        string launchScript = lines[1].Trim();

        var sb = new StringBuilder();
        sb.Append(QuoteArg(launchScript));
        for (int i = 0; i < args.Length; i++)
        {
            sb.Append(' ');
            sb.Append(QuoteArg(args[i]));
        }

        var psi = new ProcessStartInfo
        {
            FileName = python,
            Arguments = sb.ToString(),
            UseShellExecute = false,
        };
        Console.CancelKeyPress += (sender, e) => { e.Cancel = true; };
        try
        {
            using (var proc = Process.Start(psi))
            {
                if (proc == null) return 1;
                proc.WaitForExit();
                return proc.ExitCode;
            }
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine("Failed to launch ViperCapture: " + ex.Message);
            return 1;
        }
    }
}
'@
                [System.IO.File]::WriteAllText($csPath, $csSource, $utf8NoBom)
                $previousPreference = $ErrorActionPreference
                $ErrorActionPreference = "Continue"
                try {
                    $global:LASTEXITCODE = 1
                    & $csc /nologo /optimize+ /target:exe "/out:$builtExe" $csPath *> $null
                    $compiled = ($LASTEXITCODE -eq 0) -and (Test-Path -LiteralPath $builtExe)
                } finally {
                    $ErrorActionPreference = $previousPreference
                }
                if ($compiled) {
                    try {
                        Copy-Item -LiteralPath $builtExe -Destination $exePath -Force
                    } catch {
                        if (-not (Test-Path -LiteralPath $exePath)) { throw }
                    }
                    if (Test-Path -LiteralPath $cmdPath) {
                        Remove-Item -LiteralPath $cmdPath -Force -ErrorAction SilentlyContinue
                    }
                    return
                }
            } finally {
                if (Test-Path -LiteralPath $buildTemp) {
                    Remove-Item -LiteralPath $buildTemp -Recurse -Force -ErrorAction SilentlyContinue
                }
            }
        }

        # Fallback when .NET Framework csc.exe is unavailable: remove any stale
        # .exe shim so Windows does not prefer it over the .cmd fallback, and
        # encode the batch file in the OEM code page used by cmd.exe.
        if (Test-Path -LiteralPath $exePath) {
            Remove-Item -LiteralPath $exePath -Force -ErrorAction SilentlyContinue
        }
        if (Test-Path -LiteralPath $configPath) {
            Remove-Item -LiteralPath $configPath -Force -ErrorAction SilentlyContinue
        }
        $oemCodePage = [System.Globalization.CultureInfo]::CurrentCulture.TextInfo.OEMCodePage
        $oemEncoding = [System.Text.Encoding]::GetEncoding($oemCodePage)
        $cmdText = "@echo off`r`n`"$Python`" `"$Launch`" %*`r`n"
        [System.IO.File]::WriteAllText($cmdPath, $cmdText, $oemEncoding)
    }

    New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
    $BinDir = (Get-Item -LiteralPath $BinDir).FullName
    $Launch = Join-Path $App "launch.py"
    Write-WindowsShim -BinDir $BinDir -Python $Python -Launch $Launch

    $AddedUserPath = $false
    if ($env:VIPERCAPTURE_SKIP_USER_PATH -ne "1") {
        $UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
        $UserEntries = Split-PathEntries $UserPath
        if (-not (Test-PathContains $UserEntries $BinDir)) {
            $Updated = if ($UserEntries.Count -gt 0) {
                (@($BinDir) + $UserEntries) -join ";"
            } else {
                $BinDir
            }
            [Environment]::SetEnvironmentVariable("Path", $Updated, "User")
            $AddedUserPath = $true
        }
    }

    $AddedSessionPath = Add-SessionPath $BinDir
    if ($AddedUserPath -and $AddedSessionPath) {
        Write-Host "  Added $BinDir to your user Path and this PowerShell session."
    } elseif ($AddedUserPath) {
        Write-Host "  Added $BinDir to your user Path."
    } elseif ($AddedSessionPath) {
        Write-Host "  Added $BinDir to this PowerShell session's Path."
    }

    Write-Host ""
    Write-Host "  Installed the vipercapture command."
    Write-Host "  Run: vipercapture"
    Write-Host "  Update later with: vipercapture update"
    Write-Host "  Stop running instances with: vipercapture kill"
    Write-Host "  Capture a page with: vipercapture capture --help"
    Write-Host "  The first launch installs dependencies and browsers, then starts the API."
    Write-Host ""
} $PSCommandPath
