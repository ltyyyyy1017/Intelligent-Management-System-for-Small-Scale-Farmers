@echo off
chcp 65001 >nul
title 乡村财务智管家
cd /d "%~dp0"

rem 本脚本只做三件事：找 Python、检查依赖为零、启动本地服务。
rem 不写注册表、不改系统设置、不需要管理员权限。

set PY=
if exist ".venv\Scripts\python.exe" set PY=.venv\Scripts\python.exe
if "%PY%"=="" (
  where python >nul 2>nul && set PY=python
)
if "%PY%"=="" (
  echo.
  echo 没找到 Python。请先安装 Python 3.11 或更高版本（安装时勾选 Add to PATH）。
  echo 下载地址：https://www.python.org/downloads/windows/
  echo.
  pause
  exit /b 1
)

echo 正在检查环境...
"%PY%" -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" || (
  echo 需要 Python 3.11 或更高版本。
  pause
  exit /b 1
)

if not exist "data\village.db" (
  echo.
  echo 第一次使用：请先建账。
  echo 下面需要输入村名、区域代码和管理员口令。
  echo.
  "%PY%" -m app.cli init
  if errorlevel 1 (
    pause
    exit /b 1
  )
)

echo.
echo 正在启动，浏览器会自动打开 http://127.0.0.1:8765/
echo 关闭这个黑窗口就会停止服务，账目不会丢。
echo.
"%PY%" -m app.cli serve
pause
