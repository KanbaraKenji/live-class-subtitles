@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ===== 1/2 安装 Python 依赖（首次需要几分钟，含约 1GB 的 CUDA 运行库）=====
if not exist .venv (
    py -3.11 -m venv .venv 2>nul || python -m venv .venv
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo Python 依赖安装失败，请检查上面的报错。
    pause
    exit /b 1
)

echo.
echo ===== 2/2 安装界面组件（需要 Node.js）=====
where node >nul 2>nul
if errorlevel 1 (
    echo 没有找到 Node.js，跳过。可以先用经典界面 run_classic.bat，
    echo 或安装 Node.js（https://nodejs.org/）后重新运行本脚本。
) else (
    pushd desktop
    call npm install
    popd
)

echo.
echo ===== 安装完成。双击 run.bat 启动，第一次会弹出设置窗口填写 API Key =====
pause
