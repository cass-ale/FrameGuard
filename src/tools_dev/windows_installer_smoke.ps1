param(
    [Parameter(Mandatory = $true)][string]$Installer,
    [Parameter(Mandatory = $true)][string]$ArtifactsDir,
    [string]$Python = "python",
    [int]$ProcessTimeoutSeconds = 180
)

$ErrorActionPreference = "Stop"
if ($ProcessTimeoutSeconds -lt 10) {
    throw "ProcessTimeoutSeconds must be at least 10"
}

$artifacts = [System.IO.Path]::GetFullPath($ArtifactsDir)
New-Item -ItemType Directory -Force -Path $artifacts | Out-Null
$root = Join-Path ([System.IO.Path]::GetTempPath()) ("frameguard-installer-smoke-" + [guid]::NewGuid())
$installDir = Join-Path $root "FrameGuard Desktop"
$errors = [System.Collections.Generic.List[string]]::new()
$summary = [ordered]@{
    installer = [System.IO.Path]::GetFullPath($Installer)
    install_dir = $installDir
    install_attempted = $false
    install_completed = $false
    startup_preflight = $false
    uninstall_attempted = $false
    uninstall_completed = $false
    temp_root_removed = $false
    process_timeout_seconds = $ProcessTimeoutSeconds
    status = "running"
}

function Wait-FrameGuardProcess {
    param(
        [Parameter(Mandatory = $true)][System.Diagnostics.Process]$Process,
        [Parameter(Mandatory = $true)][string]$Label
    )

    try {
        if ($Process.WaitForExit($ProcessTimeoutSeconds * 1000)) {
            return $true
        }
    }
    catch {
        $errors.Add("Could not wait for ${Label}: $($_.Exception.Message)")
    }

    $errors.Add("${Label} timed out after $ProcessTimeoutSeconds seconds")
    try {
        if (-not $Process.HasExited) {
            Stop-Process -Id $Process.Id -Force -ErrorAction Stop
        }
    }
    catch {
        $errors.Add("Could not terminate timed-out ${Label}: $($_.Exception.Message)")
    }
    try {
        [void]$Process.WaitForExit(10000)
    }
    catch {
        $errors.Add("Could not reap timed-out ${Label}: $($_.Exception.Message)")
    }
    return $false
}

try {
    $installerPath = (Resolve-Path $Installer).Path
    $summary.installer = $installerPath
    New-Item -ItemType Directory -Force -Path $root | Out-Null
    $summary.install_attempted = $true
    $setup = Start-Process -FilePath $installerPath -ArgumentList @(
        "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-",
        ('/DIR="{0}"' -f $installDir)
    ) -PassThru
    if (-not (Wait-FrameGuardProcess -Process $setup -Label "installer")) {
        throw "Installer did not finish within its bounded lifetime"
    }
    $summary.install_completed = $true
    if ($setup.ExitCode -ne 0) {
        throw "Installer exited with $($setup.ExitCode)"
    }

    $exe = Join-Path $installDir "FrameGuard.exe"
    $uninstaller = Join-Path $installDir "unins000.exe"
    $required = @(
        $exe,
        (Join-Path $installDir "_internal"),
        (Join-Path $installDir "tools\ffmpeg.exe"),
        (Join-Path $installDir "tools\ffprobe.exe"),
        $uninstaller
    )
    foreach ($path in $required) {
        if (-not (Test-Path $path)) { throw "Installed layout is missing: $path" }
    }
    $presets = Get-ChildItem -Path (Join-Path $installDir "_internal\presets") -File -ErrorAction Stop
    if ($presets.Count -eq 0) { throw "Installed bundle has no presets" }

    $association = Get-ItemPropertyValue `
        -Path "HKCU:\Software\Classes\FrameGuard.Preset\shell\open\command" `
        -Name "(default)"
    if ($association -notlike "*$exe*") {
        throw ".fg association does not target the installed executable: $association"
    }

    & $Python "tools_dev\startup_smoke.py" `
        --executable $exe `
        --artifacts-dir $artifacts `
        --label "windows-installed-startup" `
        --expect-frozen `
        --qpa windows `
        --timeout 30
    if ($LASTEXITCODE -ne 0) { throw "Installed startup preflight failed" }
    $summary.startup_preflight = $true
}
catch {
    $errors.Add("Installed-artifact smoke failed: $($_.Exception.Message)")
}
finally {
    try {
        $uninstaller = Join-Path $installDir "unins000.exe"
        $partialInstallExists = Test-Path -LiteralPath $installDir
        if (Test-Path -LiteralPath $uninstaller) {
            $summary.uninstall_attempted = $true
            $uninstall = $null
            try {
                $uninstall = Start-Process -FilePath $uninstaller -ArgumentList @(
                    "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"
                ) -PassThru
            }
            catch {
                $errors.Add("Could not start uninstaller: $($_.Exception.Message)")
            }
            if ($null -ne $uninstall) {
                if (Wait-FrameGuardProcess -Process $uninstall -Label "uninstaller") {
                    if ($uninstall.ExitCode -eq 0) {
                        $summary.uninstall_completed = $true
                    }
                    else {
                        $errors.Add("Uninstaller exited with $($uninstall.ExitCode)")
                    }
                }
            }
        }
        elseif ($partialInstallExists) {
            $summary.uninstall_attempted = $true
            $errors.Add("Partial installation has no uninstaller: $uninstaller")
        }
        elseif ($summary.install_completed) {
            $summary.uninstall_attempted = $true
            $errors.Add("Completed installation has no uninstaller: $uninstaller")
        }
    }
    catch {
        $summary.uninstall_completed = $false
        $errors.Add("Unexpected uninstall cleanup failure: $($_.Exception.Message)")
    }
    finally {
        $summary.temp_root_removed = $false
        try {
            if (Test-Path -LiteralPath $root) {
                Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction Stop
            }
            $summary.temp_root_removed = -not (Test-Path -LiteralPath $root)
        }
        catch {
            $errors.Add("Could not remove temporary root: $($_.Exception.Message)")
        }
        if (-not $summary.temp_root_removed) {
            $errors.Add("Temporary root still exists: $root")
        }

        $complete = (
            $summary.install_attempted -and
            $summary.install_completed -and
            $summary.startup_preflight -and
            $summary.uninstall_attempted -and
            $summary.uninstall_completed -and
            $summary.temp_root_removed -and
            $errors.Count -eq 0
        )
        if ($complete) {
            $summary.status = "passed"
        }
        else {
            $summary.status = "failed"
        }
        if ($errors.Count -gt 0) {
            $summary.error = $errors -join "; "
        }

        $evidencePath = Join-Path $artifacts "windows-installer-smoke.json"
        $temporaryEvidence = "$evidencePath.$PID.tmp"
        $summary | ConvertTo-Json -Depth 4 | Set-Content `
            -Path $temporaryEvidence -Encoding utf8
        Move-Item -LiteralPath $temporaryEvidence -Destination $evidencePath -Force
    }
}

if ($summary.status -ne "passed") {
    throw "Windows installer smoke failed: $($summary.error)"
}
