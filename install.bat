@echo off
setlocal
rem ferry Windows 安装脚本：复制到 %USERPROFILE%\.ferry\install，
rem 并在 %USERPROFILE%\.ferry\bin 生成 ferry.cmd 包装器
rem 用法：install.bat            安装/升级
rem       install.bat --uninstall 卸载
rem 说明：不修改系统 PATH、不动注册表、不需要管理员权限。

set "SRC=%~dp0"
set "SRC=%SRC:~0,-1%"
set "DEST=%USERPROFILE%\.ferry\install"
set "BIN=%USERPROFILE%\.ferry\bin"

if /i "%~1"=="--uninstall" (
  if exist "%BIN%\ferry.cmd" del /q "%BIN%\ferry.cmd"
  if exist "%DEST%" rmdir /s /q "%DEST%"
  echo 已卸载 ferry（%DEST% 与 %BIN%\ferry.cmd 已删除）
  exit /b 0
)

rem 找一个可用的 Python：优先 py launcher，其次 PATH 里的 python
set "PYCMD="
where py >nul 2>&1 && set "PYCMD=py -3"
if not defined PYCMD (
  where python >nul 2>&1 && set "PYCMD=python"
)
if not defined PYCMD (
  echo 未找到 Python，请先安装 Python 3.9+ 并确保在 PATH 中
  exit /b 1
)

echo ==^> 安装到 %DEST%
if not exist "%DEST%\data" mkdir "%DEST%\data"
if not exist "%BIN%" mkdir "%BIN%"

copy /y "%SRC%\ferry.py" "%DEST%\ferry.py" >nul
if exist "%DEST%\data" del /q "%DEST%\data\*.jsonc" 2>nul
copy /y "%SRC%\data\*.jsonc" "%DEST%\data\" >nul
if exist "%SRC%\README.md" copy /y "%SRC%\README.md" "%DEST%\README.md" >nul
if exist "%SRC%\docs" (
  if not exist "%DEST%\docs" mkdir "%DEST%\docs"
  copy /y "%SRC%\docs\*.md" "%DEST%\docs\" >nul 2>&1
)

rem 生成包装器：把 Python 启动器和脚本路径写死，避免依赖 PATH
> "%BIN%\ferry.cmd" echo @echo off
>> "%BIN%\ferry.cmd" echo %PYCMD% "%DEST%\ferry.py" %%*

echo ==^> 自检
%PYCMD% "%DEST%\ferry.py" doctor
if errorlevel 1 (
  echo 自检失败，安装可能不完整
  exit /b 1
)

echo.
echo 完成。当前窗口可直接用（三选一）：
echo   1. 绝对路径：  "%BIN%\ferry.cmd" translate "ls -la" --to windows
echo   2. 临时加 PATH：set PATH=%%USERPROFILE%%\.ferry\bin;%%PATH%%
echo   3. 永久加 PATH：setx PATH "%%PATH%%;%%USERPROFILE%%\.ferry\bin"  （需重开终端）
echo.
echo 本脚本不改系统 PATH、不写注册表，卸载：install.bat --uninstall
exit /b 0
