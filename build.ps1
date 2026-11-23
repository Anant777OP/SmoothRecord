# Builds SmoothRecord.exe and the release ZIP.   Usage:  .\build.ps1 -Version 1.0.0
param([string]$Version = "1.0.0")
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$ffmpeg = (Get-Command ffmpeg -ErrorAction SilentlyContinue).Source
if (-not $ffmpeg) { throw "FFmpeg not found. Install it first:  winget install Gyan.FFmpeg" }
if (-not (& $ffmpeg -hide_banner -filters 2>&1 | Select-String "gfxcapture")) {
    throw "This FFmpeg has no 'gfxcapture' filter (needed for colour-exact capture). Install FFmpeg 8 or newer."
}

Write-Host "Installing Python dependencies..." -ForegroundColor Cyan
python -m pip install --upgrade -r requirements.txt pyinstaller | Out-Null

Write-Host "Building SmoothRecord.exe..." -ForegroundColor Cyan
python -m PyInstaller --noconfirm --onefile --windowed --name SmoothRecord `
    --icon "$PSScriptRoot\SmoothRecord.ico" --add-data "$PSScriptRoot\SmoothRecord.ico;." `
    --collect-all soundcard --hidden-import numpy --hidden-import PIL.ImageTk `
    --distpath "$PSScriptRoot\dist" --workpath "$PSScriptRoot\build" --specpath "$PSScriptRoot\build" `
    "$PSScriptRoot\SmoothRecord.pyw"
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

Write-Host "Packaging release..." -ForegroundColor Cyan
$stage = "$PSScriptRoot\build\SmoothRecord-v$Version-win64"
Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $stage | Out-Null
Copy-Item "$PSScriptRoot\dist\SmoothRecord.exe", $ffmpeg, "$PSScriptRoot\LICENSE", "$PSScriptRoot\THIRD_PARTY_NOTICES.md" $stage
Copy-Item $ffmpeg "$PSScriptRoot\dist\ffmpeg.exe" -Force
@"
SmoothRecord v$Version  -  by Anant Rastogi
=======================================
1. Keep SmoothRecord.exe and ffmpeg.exe in the same folder.
2. Run SmoothRecord.exe. (If SmartScreen warns: More info -> Run anyway.)
3. Ctrl+Alt+R starts/stops recording. Right-click the bar for quick options.

Recordings: Videos\SmoothRecord    Settings: %APPDATA%\SmoothRecord
"@ | Set-Content "$stage\README.txt" -Encoding utf8
$zip = "$PSScriptRoot\dist\SmoothRecord-v$Version-win64.zip"
Remove-Item $zip -ErrorAction SilentlyContinue
Compress-Archive -Path "$stage\*" -DestinationPath $zip -CompressionLevel Optimal
Write-Host "Done: $zip" -ForegroundColor Green
