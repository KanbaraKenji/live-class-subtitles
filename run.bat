@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem 新界面需要 Node.js；没有的话自动改用经典界面
where node >nul 2>nul || goto classic
if not exist "desktop\node_modules\electron\dist\electron.exe" (
    echo 首次运行，正在安装界面组件（约 100MB）……
    pushd desktop
    call npm install
    popd
    if not exist "desktop\node_modules\electron\dist\electron.exe" goto classic
)
start "" "%~dp0desktop\node_modules\electron\dist\electron.exe" "%~dp0desktop"
exit /b 0

:classic
echo 没有找到 Node.js，使用经典界面。安装 Node.js（https://nodejs.org/）后可以使用新界面。
call "%~dp0run_classic.bat" %*
