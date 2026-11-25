# Install ffmpeg/ffprobe for Windows CI without depending on Chocolatey.
#
# Chocolatey returned 503/504 from community.chocolatey.org for over an hour on
# 2026-11-25. The old workflow steps exited 0 even then, so the next step failed
# with FileNotFoundError and the required windows check could not pass.
#
# Order: use executables already on PATH, else try Chocolatey once with a hard
# time bound, else download a pinned GyanD/codexffmpeg release zip over HTTPS,
# verify its pinned SHA-256 before extracting, extract under RUNNER_TEMP, and
# put its bin directory on GITHUB_PATH and this process's PATH. Every failure
# throws: the step must never continue with a missing or mismatched install.

[CmdletBinding()]
param(
    [int]$ChocoTimeoutSeconds = 180,
    [int]$DownloadTimeoutSeconds = 180,
    [int]$DownloadAttempts = 3
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$ProgressPreference = 'SilentlyContinue'

$FfmpegVersion = '9.1'
$ArchiveName = 'ffmpeg-9.1-essentials_build.zip'
$ArchiveUrl = 'https://github.com/GyanD/codexffmpeg/releases/download/9.1/ffmpeg-9.1-essentials_build.zip'
$ArchiveSha256 = 'b9bbbb3acece000cbeec994f856649354e52c773807841e78908374b24d658fd'

function Test-FfmpegTools {
    $ffmpeg = Get-Command ffmpeg -CommandType Application -ErrorAction SilentlyContinue
    $ffprobe = Get-Command ffprobe -CommandType Application -ErrorAction SilentlyContinue
    if (-not $ffmpeg -or -not $ffprobe) {
        return $false
    }
    foreach ($tool in @($ffmpeg, $ffprobe)) {
        try {
            & $tool.Source -version *> $null
        } catch {
            return $false
        }
        if ($LASTEXITCODE -ne 0) {
            return $false
        }
    }
    return $true
}

function Add-ToProcessPath([string]$Directory) {
    $entries = @($env:Path -split ';' | Where-Object { $_ })
    if ($entries -notcontains $Directory) {
        $env:Path = "$Directory;$env:Path"
    }
}

function Update-ProcessPathFromChocolatey {
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user = [Environment]::GetEnvironmentVariable('Path', 'User')
    foreach ($directory in (($machine, $user) -join ';') -split ';') {
        if ($directory) {
            Add-ToProcessPath $directory
        }
    }
}

if (Test-FfmpegTools) {
    Write-Host 'ffmpeg and ffprobe are already runnable; nothing to install.'
    exit 0
}

$choco = Get-Command choco -CommandType Application -ErrorAction SilentlyContinue
$choco = $null  # TEMPORARY CI proof: force the pinned fallback
if ($choco) {
    Write-Host "ffmpeg/ffprobe are missing; trying Chocolatey once (bounded at ${ChocoTimeoutSeconds}s)."
    $process = $null
    try {
        $process = Start-Process -FilePath $choco.Source `
            -ArgumentList @('install', 'ffmpeg', '--no-progress', '-y') `
            -NoNewWindow -PassThru
        if (-not $process.WaitForExit($ChocoTimeoutSeconds * 1000)) {
            Write-Warning "Chocolatey ffmpeg install exceeded ${ChocoTimeoutSeconds}s; stopping it."
            try {
                $process.Kill()
                $process.WaitForExit(10000) | Out-Null
            } catch {
                Write-Warning "Could not stop Chocolatey cleanly: $_"
            }
        } else {
            Write-Host "Chocolatey ffmpeg install exited with code $($process.ExitCode)."
        }
    } catch {
        Write-Warning "Chocolatey ffmpeg install failed: $_"
    }
    Update-ProcessPathFromChocolatey
    if (Test-FfmpegTools) {
        Write-Host 'Chocolatey provided ffmpeg and ffprobe.'
        exit 0
    }
    Write-Warning 'Chocolatey did not make ffmpeg and ffprobe runnable; using the pinned fallback.'
} else {
    Write-Host 'Chocolatey is not available; using the pinned fallback.'
}

if (-not $env:RUNNER_TEMP) {
    throw 'RUNNER_TEMP is not set; cannot install the pinned ffmpeg fallback.'
}

$archivePath = Join-Path $env:RUNNER_TEMP $ArchiveName
$extractRoot = Join-Path $env:RUNNER_TEMP "aa2-ffmpeg-$FfmpegVersion"
Remove-Item -Recurse -Force $extractRoot -ErrorAction SilentlyContinue
Remove-Item -Force $archivePath -ErrorAction SilentlyContinue

Write-Host "Downloading pinned ffmpeg $FfmpegVersion from $ArchiveUrl"
$downloaded = $false
for ($attempt = 1; $attempt -le $DownloadAttempts; $attempt++) {
    try {
        Invoke-WebRequest -Uri $ArchiveUrl -OutFile $archivePath -UseBasicParsing `
            -MaximumRedirection 5 -TimeoutSec $DownloadTimeoutSeconds
        $downloaded = $true
        break
    } catch {
        Write-Warning "Download attempt $attempt/$DownloadAttempts failed: $_"
        Remove-Item -Force $archivePath -ErrorAction SilentlyContinue
        if ($attempt -lt $DownloadAttempts) {
            Start-Sleep -Seconds (5 * $attempt)
        }
    }
}
if (-not $downloaded) {
    throw "Failed to download $ArchiveUrl after $DownloadAttempts attempts."
}

$actualSha256 = (Get-FileHash -Algorithm SHA256 -Path $archivePath).Hash.ToLowerInvariant()
if ($actualSha256 -ne $ArchiveSha256) {
    Remove-Item -Force $archivePath -ErrorAction SilentlyContinue
    throw "SHA-256 mismatch for $ArchiveName`: expected $ArchiveSha256, got $actualSha256. Refusing to extract."
}
Write-Host "Verified pinned SHA-256 $ArchiveSha256."

try {
    Expand-Archive -Path $archivePath -DestinationPath $extractRoot -Force
} catch {
    throw "Failed to extract $($archivePath): $_"
}

$ffmpeg = Get-ChildItem -Path $extractRoot -Recurse -Filter 'ffmpeg.exe' -File |
    Select-Object -First 1
if (-not $ffmpeg) {
    throw "The pinned ffmpeg archive did not contain ffmpeg.exe."
}
$binDirectory = $ffmpeg.Directory.FullName
$ffprobe = Join-Path $binDirectory 'ffprobe.exe'
if (-not (Test-Path -LiteralPath $ffprobe -PathType Leaf)) {
    throw "The pinned ffmpeg archive did not contain ffprobe.exe beside ffmpeg.exe."
}

Add-Content -Path $env:GITHUB_PATH -Value $binDirectory
Add-ToProcessPath $binDirectory

if (-not (Test-FfmpegTools)) {
    throw "ffmpeg and ffprobe are still not runnable after installing the pinned fallback from $binDirectory."
}

Write-Host "Pinned ffmpeg fallback installed from $binDirectory"
& ffmpeg -version | Select-Object -First 1
& ffprobe -version | Select-Object -First 1
