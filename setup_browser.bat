@echo off
title Browser Setup
set PYTHONIOENCODING=utf-8
call .venv\Scripts\activate.bat
python flow_automator.py
pause
