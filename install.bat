@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ===== 创建虚拟环境并安装依赖（首次需要几分钟）=====
if not exist .venv (
    py -3.11 -m venv .venv 2>nul || python -m venv .venv
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo 安装失败，请把上面的报错发给我。
    pause
    exit /b 1
)
if not exist .env (
    copy .env.example .env >nul
    echo.
    echo 已生成 .env，请用记事本打开填入 ANTHROPIC_API_KEY。
)
echo.
echo ===== 安装完成。双击 run.bat 启动 =====
pause
