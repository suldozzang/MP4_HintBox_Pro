# =====================================================================
#  MP4 HintBox Pro 설치형 빌드 스크립트
#  1) PyInstaller --onedir 로 일반판 / Tiny 판 빌드
#  2) Inno Setup 으로 설치 파일(Setup.exe) 생성
#  사용법:  powershell -ExecutionPolicy Bypass -File installer\build_installer.ps1
#  결과물:  build_out\installer\MP4_HintBox_Pro_Setup_v<버전>.exe
#           build_out\installer\MP4_HintBox_Pro_Tiny_Setup_v<버전>.exe
# =====================================================================
param(
    [switch]$FullOnly,
    [switch]$TinyOnly
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$iscc = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe") |
        Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw "Inno Setup 6 (ISCC.exe) 를 찾을 수 없습니다." }

# 버전은 소스의 APP_VERSION 을 그대로 사용
$ver = (Select-String -Path "$root\mp4hint_2.0.0.py" -Pattern '^APP_VERSION\s*=\s*"([^"]+)"').Matches[0].Groups[1].Value
Write-Host "버전: $ver"

$out  = Join-Path $root "build_out"
$dist = Join-Path $out "dist"
$work = Join-Path $out "work"
$spec = Join-Path $out "spec"

$excludes = @("tkinter", "matplotlib", "numpy", "pandas", "scipy",
              "PyQt5.QtWebEngineCore", "PyQt5.QtWebEngineWidgets", "PyQt5.QtMultimedia",
              "PyQt5.QtMultimediaWidgets", "PyQt5.QtQml", "PyQt5.QtQuick") |
            ForEach-Object { "--exclude-module=$_" }

function Invoke-PyInstaller([string]$name, [string]$script, [string[]]$extra) {
    Write-Host "`n=== PyInstaller: $name ===" -ForegroundColor Cyan
    $pyArgs = @("--noconfirm", "--clean", "--onedir", "--noconsole",
              "--name", $name,
              "--icon", "$root\app_icon.ico",
              "--distpath", $dist, "--workpath", $work, "--specpath", $spec,
              "--add-data", "$root\app_icon.ico;.",
              "--hidden-import=PyQt5.sip") + $excludes + $extra + @("$root\$script")
    & pyinstaller @pyArgs
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller 실패: $name" }
}

function Invoke-Iscc([string[]]$defs) {
    Write-Host "`n=== Inno Setup: $($defs -join ' ') ===" -ForegroundColor Cyan
    & $iscc "/Qp" "/DAppVer=$ver" @defs "$PSScriptRoot\mp4hintbox.iss"
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup 실패" }
}

if (-not $TinyOnly) {
    $bins = @("ffmpeg.exe", "ffprobe.exe", "mp4box.exe", "libgpac.dll", "MediaInfo.dll",
              "libeay32.dll", "ssleay32.dll", "js.dll", "js32.dll") |
            ForEach-Object { "--add-binary"; "$root\$_;." }
    $datas = @("license.txt", "README.txt") | ForEach-Object { "--add-data"; "$root\$_;." }
    Invoke-PyInstaller "MP4_HintBox_Pro" "mp4hint_2.0.0.py" (@("--hidden-import=psutil") + $bins + $datas)
    Invoke-Iscc @()
}
if (-not $FullOnly) {
    Invoke-PyInstaller "MP4_HintBox_Pro_Tiny" "mp4hint_2.0.0_tiny.py" @()
    Invoke-Iscc @("/DTiny")
}

Write-Host "`n완료:" -ForegroundColor Green
Get-ChildItem "$out\installer" -Filter "*_v$ver.exe" | ForEach-Object {
    "{0}  ({1:N1} MB)" -f $_.FullName, ($_.Length / 1MB)
}
