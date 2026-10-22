@echo off
title Jarvis Persistent Channel Agent
cd /d "%~dp0"
uv run python -m friday.services.agent_host --check
if errorlevel 1 (
    echo.
    echo Setup is incomplete. Add the missing WhatsApp and local LLM settings to friday\.env.
    echo See docs\PERSISTENT_AGENT.md for Windows setup instructions.
    pause
    exit /b 1
)
uv run python -m friday.services.agent_host
if errorlevel 1 pause
