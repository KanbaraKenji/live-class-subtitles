@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 经典界面（tkinter），不需要 Node.js
if exist .venv\Scripts\activate.bat call .venv\Scripts\activate.bat
python main.py %*
if errorlevel 1 pause
