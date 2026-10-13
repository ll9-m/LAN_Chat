@echo off
setlocal
cd /d "%~dp0"

echo ========================================
echo   LAN_Chat 环境配置
echo ========================================
echo.

rem ---- 1/4 检查 Python ----
python --version >nul 2>&1
if errorlevel 1 (
    echo [1/4] 未找到 Python，请安装 Python 3.10+ 并勾选 "Add to PATH"
    pause
    exit /b 1
)
for /f "delims=" %%v in ('python --version 2^>^&1') do set "PYVER=%%v"
echo [1/4] Python: %PYVER%

rem ---- 2/4 安装依赖 ----
echo [2/4] 安装依赖: pip install -r requirements.txt
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo [2/4] 依赖安装失败，请检查网络或 pip 源后重试
    pause
    exit /b 1
)

rem ---- 3/4 启动 MySQL 服务 ----
echo [3/4] 检查 MySQL 服务...
sc query MySQL80 >nul 2>&1
if errorlevel 1060 (
    echo [3/4] 未找到 MySQL80 服务，请确认已安装 MySQL 8.0
    goto :mysql_warn
)
sc query MySQL80 | find "RUNNING" >nul
if not errorlevel 1 (
    echo [3/4] MySQL80 正在运行
    goto :mysql_done
)
echo [3/4] MySQL80 未运行，正在启动...
net start MySQL80 >nul 2>&1
if errorlevel 1 (
    echo [3/4] 启动失败：请右键"以管理员身份运行"本脚本，或手动启动 MySQL 服务
    goto :mysql_warn
)
echo [3/4] MySQL80 已启动

:mysql_done
rem ---- 4/4 连接测试 ----
echo [4/4] 测试 MySQL 连接（默认 root/root，可用 MYSQL_* 环境变量覆盖）...
python -c "import os, pymysql; pymysql.connect(host=os.environ.get('MYSQL_HOST','127.0.0.1'), port=int(os.environ.get('MYSQL_PORT','3306')), user=os.environ.get('MYSQL_USER','root'), password=os.environ.get('MYSQL_PASSWORD','root'), connect_timeout=5).close(); print('        connect OK')"
if errorlevel 1 (
    echo [4/4] 连接失败：请确认 MySQL 已启动、账号密码正确
    echo        默认 root/root；非默认时先设置 MYSQL_USER / MYSQL_PASSWORD 环境变量
    goto :finish
)

echo.
echo ========================================
echo   环境配置完成
echo   启动命令: python app.py
echo   （首次启动自动建库建表，无需手动执行 schema.sql）
echo ========================================
set "STARTNOW="
set /p STARTNOW="现在启动项目吗? (Y/N): "
if /i not "%STARTNOW%"=="Y" goto :end
echo 正在启动，按 Ctrl+C 停止...
python app.py
goto :end

:mysql_warn
echo.
echo [提示] MySQL 未就绪。依赖已装好，启动 MySQL 后运行: python app.py
goto :finish

:finish
echo.
echo 环境配置结束（存在上述问题，请按提示处理后重新运行本脚本）

:end
pause
endlocal
