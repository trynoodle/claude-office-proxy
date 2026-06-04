@echo off
chcp 65001 >nul
REM Claude for Office Proxy 启动脚本

set "PYTHONPATH=%~dp0src"
set "CONFIG=%~dp0config.yaml"

if not exist "%CONFIG%" (
    echo [ERROR] config.yaml not found.
    echo Copy config.yaml.example to config.yaml and fill in your API keys.
    pause
    exit /b 1
)

echo Starting claude-office-proxy on port 8766...
start /min "office-proxy" python -m claude_office_proxy --config "%CONFIG%" --port 8766

timeout /t 3 /nobreak >nul

echo.
echo Proxy: https://127.0.0.1:8766
echo Gateway URL: https://127.0.0.1:8766
echo Token: kaicode
echo.
echo Press any key to stop...
pause >nul
taskkill /f /fi "WINDOWTITLE eq office-proxy" >nul 2>&1
