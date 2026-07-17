@echo off
cd /d %~dp0
echo === Tizkoran server ===
python -m uvicorn main:app --host 0.0.0.0 --port 8787
pause
