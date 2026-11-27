"""
AI YOUTUBE SHORTS CLIPPER - ENTRYPOINT RUNNER
Inicia a aplicação completa: Banco de Dados, FastAPI Web Server e Pipeline 24/7.
"""

import sys
import os
import threading
import uvicorn
from app.database.session import init_db
from app.services.pipeline_worker import PipelineWorker

def start_background_worker():
    worker = PipelineWorker()
    worker.start_loop(interval_seconds=180) # Executa ciclo a cada 3 minutos

def main():
    print("=" * 60)
    print("🎬 AI YOUTUBE SHORTS CLIP ENGINE — INICIANDO...")
    print("=" * 60)

    # 1. Inicializar banco e sementes
    print("📦 Inicializando banco de dados e sementes de canais...")
    init_db()
    print("✅ Banco pronto com motor de deduplicação ativo!")

    # 2. Iniciar Worker 24/7 em background thread
    worker_thread = threading.Thread(target=start_background_worker, daemon=True)
    worker_thread.start()
    print("🤖 Worker 24/7 rodando em segundo plano (monitorando 9 canais)!")

    # 3. Iniciar Servidor Web
    port = int(os.getenv("PORT", 8000))
    host = os.getenv("HOST", "0.0.0.0")
    print(f"🚀 Dashboard Web disponível em: http://localhost:{port}")
    print("=" * 60)

    uvicorn.run("app.main:app", host=host, port=port, reload=False)

if __name__ == "__main__":
    main()
