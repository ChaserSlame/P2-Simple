$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ffmpegArchiveName = "ffmpeg-n9.0-latest-win64-lgpl-9.0.zip"
$ffmpegReleaseUrl = "https://api.github.com/repos/BtbN/FFmpeg-Builds/releases/tags/latest"
$temporaryRoot = $env:RUNNER_TEMP
if ([string]::IsNullOrWhiteSpace($temporaryRoot)) {
    $temporaryRoot = $env:TEMP
}
if ([string]::IsNullOrWhiteSpace($temporaryRoot)) {
    throw "No se encontró una carpeta temporal para descargar FFmpeg."
}
$downloadDirectory = Join-Path $temporaryRoot "p2viewer-ffmpeg"
$archivePath = Join-Path $downloadDirectory $ffmpegArchiveName
$extractedPath = Join-Path $downloadDirectory "extracted"

Set-Location $repoRoot
python -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) {
    throw "No se pudo actualizar pip."
}
python -m pip install ".[windows-build]"
if ($LASTEXITCODE -ne 0) {
    throw "No se pudieron instalar las dependencias de compilación."
}
python -m unittest discover -s tests
if ($LASTEXITCODE -ne 0) {
    throw "Fallaron las pruebas del proyecto."
}

New-Item -ItemType Directory -Path $downloadDirectory -Force | Out-Null
$headers = @{
    Accept = "application/vnd.github+json"
    "User-Agent" = "P2ViewerPlus-Windows-build"
    "X-GitHub-Api-Version" = "2022-11-28"
}
$release = Invoke-RestMethod -Uri $ffmpegReleaseUrl -Headers $headers
$asset = $release.assets |
    Where-Object { $_.name -eq $ffmpegArchiveName } |
    Select-Object -First 1
if ($null -eq $asset -or $asset.digest -notmatch "^sha256:([0-9a-fA-F]{64})$") {
    throw "No se encontró el checksum SHA-256 publicado para $ffmpegArchiveName."
}
$expectedHash = $asset.digest.Substring("sha256:".Length)

Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $archivePath
$actualHash = (Get-FileHash -Path $archivePath -Algorithm SHA256).Hash
if ($actualHash -ne $expectedHash) {
    throw "El SHA-256 del paquete de FFmpeg no coincide con el publicado."
}

Expand-Archive -LiteralPath $archivePath -DestinationPath $extractedPath -Force
$ffmpeg = Get-ChildItem -Path $extractedPath -Filter "ffmpeg.exe" -File -Recurse |
    Select-Object -First 1
if ($null -eq $ffmpeg) {
    throw "El paquete descargado no contiene ffmpeg.exe."
}
$ffprobe = Join-Path $ffmpeg.DirectoryName "ffprobe.exe"
if (-not (Test-Path -LiteralPath $ffprobe -PathType Leaf)) {
    throw "El paquete descargado no contiene ffprobe.exe junto a ffmpeg.exe."
}

$arguments = @(
    "--noconfirm",
    "--clean",
    "--windowed",
    "--onedir",
    "--distpath", "dist",
    "--workpath", "build\windows\work",
    "--specpath", "build\windows\spec",
    "--name", "P2ViewerPlus",
    "--collect-all", "qdarktheme",
    "--add-binary", "$($ffmpeg.FullName);ffmpeg",
    "--add-binary", "$ffprobe;ffmpeg",
    "src/p2viewer/__main__.py"
)
python -m PyInstaller @arguments
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller falló al empaquetar P2ViewerPlus."
}

$bundleDirectory = Join-Path $repoRoot "dist\P2ViewerPlus"
$bundledTools = Join-Path $bundleDirectory "_internal\ffmpeg"
if (
    -not (Test-Path (Join-Path $bundledTools "ffmpeg.exe") -PathType Leaf) -or
    -not (Test-Path (Join-Path $bundledTools "ffprobe.exe") -PathType Leaf)
) {
    throw "PyInstaller no colocó ffmpeg.exe y ffprobe.exe en la carpeta esperada."
}

Copy-Item `
    -LiteralPath (Join-Path $PSScriptRoot "..\packaging\windows\THIRD-PARTY-NOTICES.txt") `
    -Destination (Join-Path $bundleDirectory "THIRD-PARTY-NOTICES.txt")
Invoke-WebRequest `
    -Uri "https://raw.githubusercontent.com/FFmpeg/FFmpeg/n9.0/COPYING.LGPLv2.1" `
    -OutFile (Join-Path $bundleDirectory "COPYING.LGPLv2.1.txt")
Invoke-WebRequest `
    -Uri "https://www.gnu.org/licenses/lgpl-3.0.txt" `
    -OutFile (Join-Path $bundleDirectory "COPYING.LGPLv3.txt")

$ffmpegBuildInfo = (& (Join-Path $bundledTools "ffmpeg.exe") -version 2>&1 |
    Out-String)
if ($LASTEXITCODE -ne 0 -or $ffmpegBuildInfo -notmatch "LGPL version") {
    throw "ffmpeg.exe no se ejecutó o no reporta una licencia LGPL."
}
$ffprobeBuildInfo = (& (Join-Path $bundledTools "ffprobe.exe") -version 2>&1 |
    Out-String)
if ($LASTEXITCODE -ne 0 -or $ffprobeBuildInfo -notmatch "LGPL version") {
    throw "ffprobe.exe no se ejecutó o no reporta una licencia LGPL."
}
@(
    "ffmpeg.exe:"
    $ffmpegBuildInfo
    "ffprobe.exe:"
    $ffprobeBuildInfo
) | Set-Content -Path (Join-Path $bundleDirectory "FFMPEG-BUILD.txt")

$portableZip = Join-Path $repoRoot "dist\P2ViewerPlus-windows-x64.zip"
Remove-Item -LiteralPath $portableZip -Force -ErrorAction SilentlyContinue
Compress-Archive `
    -Path $bundleDirectory `
    -DestinationPath $portableZip `
    -CompressionLevel Optimal
Write-Host "Portable package created: $portableZip"
