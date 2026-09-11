@echo off
chcp 65001 >nul
cd /d "%~dp0"
title ETF PEG 分析工具 - 服务运行中（请勿关闭本窗口）

set "PY="

REM 1) 优先用常见安装路径里的 python
if exist "%LOCALAPPDATA%\Programs\Python\Python314\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python314\python.exe"
if not defined PY if exist "%LOCALAPPDATA%\Python\pythoncore-3.14-64\python.exe" set "PY=%LOCALAPPDATA%\Python\pythoncore-3.14-64\python.exe"

REM 2) 其次 py 启动器
if not defined PY (
  where py >nul 2>&1 && set "PY=py"
)

REM 3) 最后 PATH 里的 python（需真的可执行，排除 Microsoft Store 占位符）
if not defined PY (
  python -c "import sys" >nul 2>&1 && set "PY=python"
)

if not defined PY (
  echo.
  echo   [错误] 没有找到 Python。
  echo   请先安装 Python 3.9 及以上版本：https://www.python.org/downloads/
  echo   安装时务必勾选 "Add Python to PATH"。
  echo.
  pause
  exit /b 1
)

echo.
echo   使用解释器: %PY%
echo   正在启动服务，浏览器会自动打开...
echo.

"%PY%" -X utf8 run.py

echo.
echo   服务已停止。
pause
