# Install-media-tools.ps1
# Run in elevated PowerShell:
# Inside the VM:

# Open Start

# Type PowerShell

# Right-click Windows PowerShell → Run as administrator

# chdir to the location of this script

#   powershell -ExecutionPolicy Bypass -File .\Install-media-tools.ps1

$ErrorActionPreference = "Stop"

# Where to install (bin folder on PATH)
$BinDir = "C:\Tools\bin"
$ToolsRoot = "C:\Tools"
$SubtitleEditDir = Join-Path $ToolsRoot "SubtitleEdit"
$WorkDir = Join-Path $env:TEMP ("media-tools-" + [guid]::NewGuid().ToString("N"))

New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null
New-Item -ItemType Directory -Force -Path $SubtitleEditDir | Out-Null

function Download-File([string]$Url, [string]$OutFile) {
  Write-Host "Downloading: $Url"
  Invoke-WebRequest -Uri $Url -OutFile $OutFile -UseBasicParsing
}

function Add-ToMachinePath([string]$PathToAdd) {
  $current = [Environment]::GetEnvironmentVariable("Path", "Machine")
  if (-not $current) { $current = "" }

  # Compare case-insensitively on Windows
  $parts = $current.Split(";", [System.StringSplitOptions]::RemoveEmptyEntries)
  $exists = $parts | Where-Object { $_.TrimEnd("\") -ieq $PathToAdd.TrimEnd("\") }

  if (-not $exists) {
    $newPath = ($current.TrimEnd(";") + ";" + $PathToAdd).TrimStart(";")
    [Environment]::SetEnvironmentVariable("Path", $newPath, "Machine")
    Write-Host "Added to MACHINE PATH: $PathToAdd"
    Write-Host "Restart your terminal/session to pick it up."
  } else {
    Write-Host "MACHINE PATH already contains: $PathToAdd"
  }
}

try {
  Push-Location $WorkDir

  # -----------------------------
  # Install FFmpeg (full build, Windows x64) from GitHub ZIP
  # -----------------------------
  $ffmpegZip = Join-Path $WorkDir "ffmpeg-full_build.zip"
  Download-File `
    "https://github.com/GyanD/codexffmpeg/releases/download/2025-12-14-git-3332b2db84/ffmpeg-2025-12-14-git-3332b2db84-full_build.zip" `
    $ffmpegZip

  $ffmpegExtract = Join-Path $WorkDir "ffmpeg"
  New-Item -ItemType Directory -Force -Path $ffmpegExtract | Out-Null
  Expand-Archive -Path $ffmpegZip -DestinationPath $ffmpegExtract -Force

  # Find the extracted "bin" folder (contains ffmpeg.exe etc.)
  $ffmpegBin = Get-ChildItem $ffmpegExtract -Recurse -Directory |
               Where-Object { $_.Name -ieq "bin" } |
               Select-Object -First 1

  if (-not $ffmpegBin) {
    throw "FFmpeg bin directory not found after extraction."
  }

  Get-ChildItem $ffmpegBin.FullName -File |
    Where-Object { $_.Name -match '^ff(mpeg|probe|play)\.exe$' } |
    ForEach-Object {
      Copy-Item -Force $_.FullName (Join-Path $BinDir $_.Name)
    }

  Write-Host "FFmpeg installed to $BinDir"


  # -----------------------------
  # Install Bento4 (mp4decrypt.exe, etc.)
  # -----------------------------
  $bentoZip = Join-Path $WorkDir "Bento4.zip"
  Download-File `
    "https://www.bok.net/Bento4/binaries/Bento4-SDK-1-6-0-641.x86_64-microsoft-win32.zip" `
    $bentoZip
  Expand-Archive -Path $bentoZip -DestinationPath (Join-Path $WorkDir "bento") -Force

  Get-ChildItem -Path (Join-Path $WorkDir "bento") -Recurse -File |
    Where-Object { $_.FullName -match "\\bin\\.*\.exe$" } |
    ForEach-Object { Copy-Item -Force $_.FullName $BinDir }

  # -----------------------------
  # Install MKVToolNix (Windows x64)
  # -----------------------------
  $mkvInstaller = Join-Path $WorkDir "mkvtoolnix-64-bit-96.0-setup.exe"
  Download-File `
    "https://mkvtoolnix.download/windows/releases/96.0/mkvtoolnix-64-bit-96.0-setup.exe" `
    $mkvInstaller

  # Silent install (NSIS): /S = silent
  # Note: no UI will appear; wait for it to finish.
  Start-Process -FilePath $mkvInstaller -ArgumentList "/S" -Wait

  # Optional: add MKVToolNix install dir to PATH if present
  $mkvDefaultPath = "C:\Program Files\MKVToolNix"
  if (Test-Path $mkvDefaultPath) {
    # If you kept Add-ToMachinePath, use that:
    Add-ToMachinePath $mkvDefaultPath

    # Or if you switched to Add-ToBestPath, use that instead:
    # Add-ToBestPath $mkvDefaultPath
  } else {
    Write-Host "MKVToolNix installed, but default path not found: $mkvDefaultPath (maybe different install location)"
  }
  
  # -----------------------------
  # Install N_m3u8DL-RE (Windows x64)
  # -----------------------------
  $nreZip = Join-Path $WorkDir "N_m3u8DL-RE.zip"
  Download-File `
    "https://github.com/nilaoda/N_m3u8DL-RE/releases/download/v0.3.0-beta/N_m3u8DL-RE_v0.3.0-beta_win-x64_20241203.zip" `
    $nreZip
  Expand-Archive -Path $nreZip -DestinationPath (Join-Path $WorkDir "nre") -Force

  Get-ChildItem (Join-Path $WorkDir "nre") -Recurse -File |
    Where-Object { $_.Name -match "^N_m3u8DL-RE(\.exe)?$" } |
    Select-Object -First 1 |
    ForEach-Object { Copy-Item -Force $_.FullName (Join-Path $BinDir "N_m3u8DL-RE.exe") }

  '''# -----------------------------
  # Install Git for Windows (64-bit)
  # -----------------------------
  $gitInstaller = Join-Path $WorkDir "Git-2.52.0-64-bit.exe"
  Download-File `
    "https://github.com/git-for-windows/git/releases/download/v2.52.0.windows.1/Git-2.52.0-64-bit.exe" `
    $gitInstaller

  # Silent install (NSIS): /S = silent
  Start-Process -FilePath $gitInstaller -ArgumentList "/S" -Wait

  # Add the typical Git cmd folder to MACHINE PATH if present
  $gitDefaultPath = "C:\Program Files\Git\cmd"
  if (Test-Path $gitDefaultPath) {
    Add-ToMachinePath $gitDefaultPath
  } else {
    Write-Host "Git installed, but default path not found: $gitDefaultPath (maybe different install location)"
  }'''

  # -----------------------------
  # Install Shaka Packager (Windows x64)
  # -----------------------------
  $shakaExe = Join-Path $BinDir "shaka-packager.exe"
  Download-File `
    "https://github.com/shaka-project/shaka-packager/releases/download/v2.6.1/packager-win-x64.exe" `
    $shakaExe

  # -----------------------------
  # Install dovi_tool (Windows x64)
  # -----------------------------
  $doviZip = Join-Path $WorkDir "dovi_tool.zip"
  Download-File `
    "https://github.com/quietvoid/dovi_tool/releases/download/2.3.1/dovi_tool-2.3.1-x86_64-pc-windows-msvc.zip" `
    $doviZip
  Expand-Archive -Path $doviZip -DestinationPath (Join-Path $WorkDir "dovi") -Force

  Get-ChildItem (Join-Path $WorkDir "dovi") -Recurse -File |
    Where-Object { $_.Name -ieq "dovi_tool.exe" } |
    Select-Object -First 1 |
    ForEach-Object { Copy-Item -Force $_.FullName (Join-Path $BinDir "dovi_tool.exe") }

  # -----------------------------
  # Install hdr10plus_tool (Windows x64)
  # -----------------------------
  $hdrZip = Join-Path $WorkDir "hdr10plus_tool.zip"
  Download-File `
    "https://github.com/quietvoid/hdr10plus_tool/releases/download/1.7.1/hdr10plus_tool-1.7.1-x86_64-pc-windows-msvc.zip" `
    $hdrZip
  Expand-Archive -Path $hdrZip -DestinationPath (Join-Path $WorkDir "hdr") -Force

  Get-ChildItem (Join-Path $WorkDir "hdr") -Recurse -File |
    Where-Object { $_.Name -ieq "hdr10plus_tool.exe" } |
    Select-Object -First 1 |
    ForEach-Object { Copy-Item -Force $_.FullName (Join-Path $BinDir "hdr10plus_tool.exe") }

  # -----------------------------
  # Install SubtitleEdit permanently (Portable zip)
  # -----------------------------
  $seZip = Join-Path $WorkDir "SubtitleEdit.zip"
  Download-File `
    "https://github.com/SubtitleEdit/subtitleedit/releases/download/4.0.14/SE4014.zip" `
    $seZip

  # Replace any previous install cleanly
  if (Test-Path $SubtitleEditDir) { Remove-Item -Recurse -Force $SubtitleEditDir }
  New-Item -ItemType Directory -Force -Path $SubtitleEditDir | Out-Null

  Expand-Archive -Path $seZip -DestinationPath $SubtitleEditDir -Force

  # Launcher in bin (stable path)
  $launcher = Join-Path $BinDir "SubtitleEdit.cmd"
  @(
    '@echo off'
    '"C:\Tools\SubtitleEdit\SubtitleEdit.exe" %*'
  ) | Set-Content -Encoding ASCII $launcher

  # -----------------------------
  # Install uv (Python required)
  # -----------------------------
  python -m pip install --upgrade uv

  # PATH (Machine)
  Add-ToMachinePath $BinDir

  Write-Host "`nDone. Open a NEW terminal and try:"
  Write-Host "  mp4decrypt.exe --help"
  Write-Host "  N_m3u8DL-RE.exe --help"
  Write-Host "  shaka-packager.exe --help"
  Write-Host "  dovi_tool.exe --help"
  Write-Host "  hdr10plus_tool.exe --help"
  Write-Host "  SubtitleEdit.cmd"
  Write-Host "  uv --version"

} finally {
  Pop-Location
  # Clean up temp work dir
  if (Test-Path $WorkDir) { Remove-Item -Recurse -Force $WorkDir }
}
