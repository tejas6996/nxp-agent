@echo off
REM Double-click to start the News Digest web UI.
cd /d "%~dp0"
uv run app/serve.py
pause
