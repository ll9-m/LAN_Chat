@echo off
rem =====================================================================
rem  LAN_Chat 一键配置环境和运行
rem  幂等脚本：可以反复双击运行。
rem    - 第一次运行：检查 Python / 安装依赖 / 启动 MySQL / 测试连接 / 启动项目
rem    - 环境已配好时：跳过安装，直接测试连接并启动项目
rem  MySQL 账号密码在第 4 步询问，直接回车使用默认 root / root。
rem =====================================================================

rem 先切到 UTF-8 代码页并重新执行自身，保证中文在任何 Windows 上都按 UTF-8 解析
if not "%~1"=="__reexec__" (
    chcp 65001 >nul
    cmd /c ""%~f0" __reexec__"
    exit /b %errorlevel%
)
setlocal
cd /d "%~dp0"

rem DID_SETUP 为空表示本轮没有做任何配置动作（即环境本来就配好了）
set "DID_SETUP="

echo ========================================
echo   LAN_Chat 一键配置环境和运行
echo ========================================
echo.

rem ---- 1/5 检查 Python ----
python --version >nul 2>&1
if errorlevel 1 (
    echo [1/5] 未找到 Python，请安装 Python 3.10+ 并勾选 "Add to PATH"
    pause
    exit /b 1
)
for /f "delims=" %%v in ('python --version 2^>^&1') do set "PYVER=%%v"
echo [1/5] Python: %PYVER%

rem ---- 2/5 安装依赖（已装好则跳过，保证幂等） ----
python -c "import flask, pymysql, qrcode, PIL" >nul 2>&1
if not errorlevel 1 (
    echo [2/5] 依赖已安装，跳过安装
    goto :deps_done
)
echo [2/5] 安装依赖: pip install -r requirements.txt
set "DID_SETUP=1"
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo [2/5] 依赖安装失败，请检查网络或 pip 源后重试
    pause
    exit /b 1
)

:deps_done
rem ---- 3/5 启动 MySQL 服务 ----
echo [3/5] 检查 MySQL 服务...
sc query MySQL80 >nul 2>&1
if errorlevel 1060 (
    echo [3/5] 未找到 MySQL80 服务，请确认已安装 MySQL 8.0
    goto :mysql_warn
)
sc query MySQL80 | findstr /c:"RUNNING" >nul
if not errorlevel 1 (
    echo [3/5] MySQL80 正在运行
    goto :mysql_done
)
echo [3/5] MySQL80 未运行，正在启动...
set "DID_SETUP=1"
net start MySQL80 >nul 2>&1
if errorlevel 1 (
    echo [3/5] 启动失败：请右键"以管理员身份运行"本脚本，或手动启动 MySQL 服务
    goto :mysql_warn
)
echo [3/5] MySQL80 已启动

:mysql_done
rem ---- 4/5 测试 MySQL 连接（询问账号密码，回车取默认值） ----
rem 未被用户显式设置时补齐默认连接参数；这里 set 的变量会被 app.py 继承
if not defined MYSQL_HOST set "MYSQL_HOST=127.0.0.1"
if not defined MYSQL_PORT set "MYSQL_PORT=3306"
if not defined MYSQL_DB set "MYSQL_DB=lan_chat"
rem 只有系统环境变量里本来就给了账号密码时才跳过询问（用 ENV_AUTH 标记，避免自己 set 后误判）
set "ENV_AUTH="
if defined MYSQL_USER if defined MYSQL_PASSWORD set "ENV_AUTH=1"
set /a TRY=0

:ask_auth
set /a TRY+=1
echo [4/5] 测试 MySQL 连接 %MYSQL_HOST%:%MYSQL_PORT%（第 %TRY% 次）
if defined ENV_AUTH (
    echo        使用环境变量中的账号 %MYSQL_USER%
    goto :do_connect
)
set "ANS_USER="
set /p "ANS_USER=       MySQL 用户名（直接回车默认 root）: "
if not defined ANS_USER set "ANS_USER=root"
set "ANS_PWD="
set /p "ANS_PWD=       MySQL 密码（直接回车默认 root）: "
if not defined ANS_PWD set "ANS_PWD=root"
set "MYSQL_USER=%ANS_USER%"
set "MYSQL_PASSWORD=%ANS_PWD%"

:do_connect
python -c "import os,pymysql; pymysql.connect(host=os.environ['MYSQL_HOST'], port=int(os.environ['MYSQL_PORT']), user=os.environ['MYSQL_USER'], password=os.environ['MYSQL_PASSWORD'], connect_timeout=5).close(); print('        connect OK')"
if not errorlevel 1 goto :conn_ok
echo        连接失败：请确认 MySQL 已启动、账号密码正确
if %TRY% GEQ 3 (
    echo        已连续 3 次失败，请确认账号密码后重新运行本脚本
    goto :finish
)
set "RETRY="
set /p "RETRY=       重新输入账号密码? (Y/N，直接回车按 Y 处理): "
if /i not "%RETRY%"=="N" goto :ask_auth
echo        跳过连接测试，可按提示处理后重新运行本脚本
goto :finish

:conn_ok
echo.
echo ========================================
if defined DID_SETUP (
    echo   环境配置完成
) else (
    echo   环境已就绪，无需重新配置
)
echo   启动命令: python app.py
echo   （首次启动自动建库建表，无需手动执行 schema.sql）
echo ========================================

rem ---- 5/5 启动项目 ----
if defined DID_SETUP goto :ask_start
echo.
echo [5/5] 检测到环境已配置完成，直接启动项目（按 Ctrl+C 停止）...
goto :launch

:ask_start
echo.
set "STARTNOW="
set /p "STARTNOW=[5/5] 现在启动项目吗? (Y/N): "
if /i not "%STARTNOW%"=="Y" goto :skip_start
echo 正在启动，按 Ctrl+C 停止...

:launch
python app.py
goto :end

:skip_start
echo 稍后可运行: python app.py 或在资源管理器双击本脚本重新启动

goto :end

:mysql_warn
echo.
echo [提示] MySQL 未就绪。依赖已装好，启动 MySQL 后双击本脚本或运行: python app.py
goto :finish

:finish
echo.
echo 环境配置结束（存在上述问题，请按提示处理后重新运行本脚本）

:end
pause
endlocal
