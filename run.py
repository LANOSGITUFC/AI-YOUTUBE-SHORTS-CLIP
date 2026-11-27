"""
AI YOUTUBE SHORTS CLIPPER - ENTRYPOINT RUNNER
Inicia a aplicação completa: Banco de Dados, FastAPI Web Server e Pipeline 24/7.
"""

import os
import uvicorn

def main():
    print("=" * 60)
    print("🎬 AI YOUTUBE SHORTS CLIP ENGINE — INICIANDO...")
    print("=" * 60)

    port = int(os.getenv("PORT", 8000))
    host = os.getenv("HOST", "0.0.0.0")
    print(f"🚀 Dashboard Web disponível em: http://localhost:{port}")
    print("🔄 Auto-reload ativo: qualquer 'git pull' atualiza o servidor automaticamente!")
    print("=" * 60)

    uvicorn.run(
        "app.main:app",
        host=host,
        port=port,
        reload=True,
        reload_dirs=["app"],
        # Não espera conexões abertas (ex: painel com stream em tempo real) para reiniciar
        timeout_graceful_shutdown=3,
    )

if __name__ == "__main__":
    main()
