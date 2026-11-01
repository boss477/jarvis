@echo off
title Jarvis Persistent Channel Agent
cd /d "%~dp0"
uv run python -m friday.services.agent_host --check
if errorlevel 1 (
    echo.
    echo Agent host readiness check failed. See the error above.
    echo If it reports missing WhatsApp or local LLM settings, update friday\.env.
    echo See docs\PERSISTENT_AGENT.md for Windows setup instructions.
    pause
    exit /b 1
)
uv run python -m friday.services.agent_host
if errorlevel 1 pause
