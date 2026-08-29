[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$repoRoot = $PSScriptRoot
$installRoot = Join-Path $env:LOCALAPPDATA "ZCode"
$venvRoot = Join-Path $installRoot "venv"
$binRoot = Join-Path $installRoot "bin"
$zcodeExe = Join-Path $venvRoot "Scripts\zcode.exe"
$shimPath = Join-Path $binRoot "zcode.cmd"

New-Item -ItemType Directory -Force -Path $installRoot, $binRoot | Out-Null

if (-not (Test-Path -LiteralPath (Join-Path $venvRoot "Scripts\python.exe"))) {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3 -m venv $venvRoot
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        & python -m venv $venvRoot
    } else {
        throw "Python 3.11 or newer was not found."
    }
}

& (Join-Path $venvRoot "Scripts\python.exe") -m pip install --upgrade $repoRoot
if ($LASTEXITCODE -ne 0) {
    throw "ZCode installation failed."
}

$shim = "@echo off`r`n`"$zcodeExe`" %*`r`n"
Set-Content -LiteralPath $shimPath -Value $shim -Encoding Ascii

$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
$pathParts = @($userPath -split ";" | Where-Object { $_ })
if ($pathParts -notcontains $binRoot) {
    $newPath = (($pathParts + $binRoot) -join ";")
    [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
}

Write-Host ""
Write-Host "ZCode installed successfully." -ForegroundColor Green
Write-Host "Open a new terminal in any project and type: zcode"
Write-Host "The first launch will securely ask for your DeepSeek API key."
