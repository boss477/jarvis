@echo off
title Jarvis WhatsApp Webhook Proxy
cd /d "%~dp0"
uv run python -m friday.services.whatsapp_proxy
if errorlevel 1 pause
