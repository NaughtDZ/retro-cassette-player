# 编译 dusk_dsp.dll —— 把 third_party/dusk_ctm 的磁带 DSP 核心编成一个 C ABI 动态库，
# 供 app/tape_fx.py 用 ctypes 加载。
#
# 不需要 CMake / JUCE / ImGui：整个闭包就是 2 个 cpp + 6 个头文件。
# 需要 Visual Studio 的 C++ 工具链（脚本用 vswhere 自动定位，不依赖 PATH）。
#
# 用法（项目根目录）：
#   .\.venv\Scripts\python.exe tools\build_dusk_dsp.ps1
#   .\.venv\Scripts\python.exe tools\build_dusk_dsp.ps1 -Clean
param(
    [switch]$Clean
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$srcDir = Join-Path $root "third_party\dusk_ctm"
$coreDir = Join-Path $srcDir "core"
$outDir = Join-Path $root "tools\dusk"
$dll = Join-Path $outDir "dusk_dsp.dll"

if ($Clean -and (Test-Path $outDir)) {
    Remove-Item $outDir -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $outDir | Out-Null

# ---- 1. 定位 MSVC ----
$vswhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
if (-not (Test-Path $vswhere)) {
    Write-Error "找不到 vswhere.exe：需要装有 C++ 工具链的 Visual Studio（Desktop development with C++）。"
    exit 1
}
$vsPath = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath 2>$null
if (-not $vsPath) {
    Write-Error "没找到带 C++ 工具链的 VS 安装：请在 Visual Studio Installer 里勾选 “使用 C++ 的桌面开发”。"
    exit 1
}
$vcvars = Join-Path $vsPath "VC\Auxiliary\Build\vcvars64.bat"
if (-not (Test-Path $vcvars)) {
    Write-Error "找不到 vcvars64.bat：$vcvars"
    exit 1
}
Write-Host "[build] VS: $vsPath"

# ---- 2. 编译 ----
$sources = @(
    (Join-Path $srcDir "dusk_dsp_shim.cpp"),
    (Join-Path $coreDir "TapeMachineDSP.cpp")
) | ForEach-Object { "`"$_`"" }

$args = @(
    "/nologo", "/utf-8", "/O2", "/std:c++17", "/EHsc",
    "/LD", "/MD",                       # 动态库 + 动态 CRT
    "/DNDEBUG",
    "/I", "`"$coreDir`"",
    "/Fo:`"$outDir\\`"",
    "/Fe:`"$dll`""
) -join " "

$cmd = "call `"$vcvars`" >nul 2>&1 && cl $args $($sources -join ' ')"
Write-Host "[build] 编译中…"
$output = cmd /c $cmd 2>&1
$output | Where-Object { $_ -match 'error|warning C|LNK|dusk_dsp' } | ForEach-Object { Write-Host "  $_" }

if (-not (Test-Path $dll)) {
    Write-Host ""
    Write-Error "编译失败：没有生成 $dll（完整输出见上）。"
    Write-Host "提示：若播放器正在运行，请先关闭它再重新编译（DLL 被占用无法覆盖）。"
    exit 1
}

# ---- 3. 清掉中间产物，只留 DLL ----
Get-ChildItem $outDir -File | Where-Object { $_.Extension -in ".obj", ".lib", ".exp" } | Remove-Item -Force

$kb = [math]::Round((Get-Item $dll).Length / 1KB, 1)
Write-Host "[build] 完成：$dll ($kb KB)"
Write-Host "[build] 上游核心 = dusk-audio-plugins TapeMachine core（GPL-3.0-or-later，见 third_party\dusk_ctm\README.md）"
