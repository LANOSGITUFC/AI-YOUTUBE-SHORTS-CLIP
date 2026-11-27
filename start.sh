#!/usr/bin/env bash
set -e

echo "=========================================================="
echo "🎬 Iniciando AI YouTube Shorts Clipper (Linux / macOS)"
echo "=========================================================="

# Cria ambiente virtual se não existir
if [ ! -d "venv" ]; then
    echo "📦 Criando ambiente virtual Python..."
    python3 -m venv venv
fi

source venv/bin/activate
pip install -r requirements.txt

python3 run.py
