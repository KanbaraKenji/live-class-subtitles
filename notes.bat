@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist .venv\Scripts\activate.bat call .venv\Scripts\activate.bat
echo 正在为最新一节课生成复习笔记，需要一两分钟……
python main.py --review %*
pause
