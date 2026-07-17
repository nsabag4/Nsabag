@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYEXE=%LOCALAPPDATA%\Programs\Python\Python312\python.exe
if not exist "%PYEXE%" set PYEXE=py
"%PYEXE%" main.py %*
if errorlevel 1 pause
