#Requires -Version 5.1
<#
ferry Windows 安装脚本
  安装：powershell -ExecutionPolicy Bypass -File install.ps1
  卸载：powershell -ExecutionPolicy Bypass -File install.ps1 -Uninstall
  加 PATH：安装时加 -AddPath（写入用户级 PATH，不用管理员权限）

行为：
  - 复制 ferry.py 与 data/*.jsonc 到 %USERPROFILE%\.ferry\install
  - 在 %USERPROFILE%\.ferry\bin 生成 ferry.cmd 包装器
  - 默认不改系统 PATH、不写注册表；-AddPath 才写用户级 PATH
#>
param(
    [switch]$Uninstall,
    [switch]$AddPath
)

$ErrorActionPreference = 'Stop'

$Src  = $PSScriptRoot
$Dest = Join-Path $env:USERPROFILE '.ferry\install'
$Bin  = Join-Path $env:USERPROFILE '.ferry\bin'
$Link = Join-Path $Bin 'ferry.cmd'

if ($Uninstall) {
    if (Test-Path $Link) { Remove-Item $Link -Force }
    if (Test-Path $Dest) { Remove-Item $Dest -Recurse -Force }
    Write-Host "已卸载 ferry（$Dest 与 $Link 已删除）"
    return
}

# 找 Python：优先 py launcher，其次 PATH 里的 python
$pyCmd = $null
if (Get-Command py -ErrorAction SilentlyContinue) {
    $pyCmd = 'py -3'
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $pyCmd = 'python'
}
if (-not $pyCmd) {
    Write-Error '未找到 Python，请先安装 Python 3.9+ 并确保在 PATH 中'
    return
}

Write-Host "==> 安装到 $Dest"
New-Item -ItemType Directory -Force -Path (Join-Path $Dest 'data') | Out-Null
New-Item -ItemType Directory -Force -Path $Bin | Out-Null

Copy-Item (Join-Path $Src 'ferry.py') (Join-Path $Dest 'ferry.py') -Force
Get-ChildItem (Join-Path $Dest 'data') -Filter '*.jsonc' -ErrorAction SilentlyContinue | Remove-Item -Force
Copy-Item (Join-Path $Src 'data\*.jsonc') (Join-Path $Dest 'data') -Force
if (Test-Path (Join-Path $Src 'README.md')) {
    Copy-Item (Join-Path $Src 'README.md') $Dest -Force
}
if (Test-Path (Join-Path $Src 'docs')) {
    $docsDest = Join-Path $Dest 'docs'
    New-Item -ItemType Directory -Force -Path $docsDest | Out-Null
    Copy-Item (Join-Path $Src 'docs\*.md') $docsDest -Force -ErrorAction SilentlyContinue
}

# 包装器：把解释器与脚本路径写死，运行时不需要依赖 PATH
$scriptPath = Join-Path $Dest 'ferry.py'
$wrapper = @"
@echo off
$pyCmd "$scriptPath" %*
"@
Set-Content -Path $Link -Value $wrapper -Encoding ASCII

# POSIX wrapper: Git Bash / MSYS / WSL cannot resolve the bare name "ferry"
# to ferry.cmd, so ship an extension-less sh script alongside it.
$shLink = Join-Path $Bin 'ferry'
$shWrapper = @"
#!/bin/sh
# POSIX wrapper for ferry (Git Bash / WSL / MSYS) - same behavior as ferry.cmd
exec $pyCmd "`$USERPROFILE/.ferry/install/ferry.py" "`$@"
"@
Set-Content -Path $shLink -Value ($shWrapper -replace "`r`n", "`n") -Encoding ASCII -NoNewline
Add-Content -Path $shLink -Value "`n" -Encoding ASCII

Write-Host '==> 自检'
& py -3 $scriptPath doctor 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Warning '自检失败，安装可能不完整'
}

if ($AddPath) {
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    if ($userPath -notlike "*$Bin*") {
        $newPath = if ([string]::IsNullOrEmpty($userPath)) { $Bin } else { "$userPath;$Bin" }
        [Environment]::SetEnvironmentVariable('Path', $newPath, 'User')
        Write-Host "==> 已把 $Bin 写入用户级 PATH（新终端生效）"
    } else {
        Write-Host "==> 用户级 PATH 已包含 $Bin"
    }
}

Write-Host ''
Write-Host '完成。当前窗口可直接用（三选一）：'
Write-Host "  1. 绝对路径：  ""$Link"" translate ""ls -la"" --to windows"
Write-Host "  2. 临时加 PATH：`$env:PATH = ""$Bin;`$env:PATH"""
Write-Host "  3. 永久加 PATH：重跑本脚本并加 -AddPath"
Write-Host ''
Write-Host "卸载：powershell -ExecutionPolicy Bypass -File install.ps1 -Uninstall"
