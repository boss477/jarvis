@echo off
title Jarvis Multi-Channel Composer
cd /d "%~dp0"
uv run python -m friday.services.compose_window
if errorlevel 1 pause
