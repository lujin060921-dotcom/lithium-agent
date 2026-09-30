@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo =============================================
echo    产业链影响分析智能体 - Web 界面
echo    默认离线模式（免 API Key）
echo =============================================
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [错误] 未找到虚拟环境 .venv。
    echo       请先在本目录执行：
    echo         python -m venv .venv
    echo         .venv\Scripts\python.exe -m pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

echo [1/2] 正在启动 Web 界面...
echo       浏览器会自动打开: http://localhost:8501
echo       关闭本窗口（或按 Ctrl+C）即可停止服务。
echo.

set "STREAMLIT_BROWSER_GATHER_USAGE_STATS=false"
".venv\Scripts\python.exe" -m streamlit run streamlit_app.py --server.headless true --server.port 8501

echo.
echo 服务已停止。
pause