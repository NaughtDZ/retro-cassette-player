@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo [x] 未找到 .venv，请先执行：
    echo     uv venv --python 3.12 .venv
    echo     uv pip install -r requirements.txt
    pause
    exit /b 1
)
if not exist "tools\ffmpeg\ffmpeg.exe" (
    echo [x] 未找到 tools\ffmpeg\ffmpeg.exe，请先放入 ffmpeg 静态构建（见 README）
    pause
    exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" main.py
