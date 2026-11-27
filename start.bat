@echo off
title AI YouTube Shorts Clipper
echo ==========================================================
echo 🎬 Iniciando AI YouTube Shorts Clipper (Windows)
echo ==========================================================

if not exist venv (
    echo 📦 Criando ambiente virtual Python...
    python -m venv venv
)

call venv\Scripts\activate.bat
pip install -r requirements.txt

python run.py
pause
