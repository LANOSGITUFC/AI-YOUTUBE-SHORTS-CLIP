# 🤖 CLAUDE.md — Diretrizes de Colaboração para o Claude Code

Este arquivo é lido automaticamente pelo **Claude Code** ao iniciar neste repositório ou na pasta `/home/ldks/Documents/work-claude/`.

---

## 🏛️ Papéis e Arquitetura de Colaboração

- **Antigravity (Google DeepMind Agent):** Arquiteto Geral e Orquestrador Central. Define a visão do sistema, desenha os esquemas de banco de dados, cria as esteiras de 24/7 e distribui missões.
- **Claude Code (Anthropic Agent):** Especialista em Refatoração, Otimização de Código, Implementação de Módulos e Testes.
- **Open Code (Local Interpreter Agent):** Execução local e validações rápidas.

---

## 🎯 Como o Claude Code Opera Neste Projeto

1. **Memória de Deduplicação de Trechos (LEI MÁXIMA):**
   - Nunca proponha ou corte um trecho de vídeo sem consultar o motor de deduplicação!
   - Consulte sempre os intervalos vagos:
     ```bash
     python3 -m app.ai_bus.cli unused-windows --video-id="VIDEO_ID"
     ```
   - O banco `clip_segments` bloqueia qualquer corte que tenha sobreposição com clipes passados.

2. **Como Receber e Executar Missões Enviadas pelo Antigravity:**
   - Verifique a pasta `data/claude_inbox/` para novas missões JSON (ex: `task_*.json`).
   - Cada missão contém:
     - `objective`: O que precisa ser feito.
     - `target_files`: Arquivos que você deve editar.
     - `instructions`: Detalhes específicos e critérios de aceite.
   - Após executar a tarefa, salve um breve relatório em `data/claude_outbox/report_<mission_id>.json`.

3. **Como Usar o Servidor MCP Nativo:**
   - O projeto possui um servidor MCP pronto em `app/ai_bus/mcp_server.py`.
   - Você pode chamar as ferramentas:
     - `get_system_status()`
     - `get_unused_windows(video_id)`
     - `propose_clip(video_id, start_time, end_time, title, hook_text, ...)`
     - `get_schedule_matrix()`

4. **Commits e Sincronização em Tempo Real:**
   - Faça commits atômicos com mensagens no padrão Conventional Commits:
     ```bash
     git add .
     git commit -m "feat(claude): <descrição clara do que foi feito>"
     git push origin main
     ```
   - Isso garante que tanto o Antigravity quanto o seu ambiente local fiquem sincronizados a cada passo.

---

## 🚀 Comandos Rápidos do Projeto

- **Iniciar API e Painel Web:** `python3 run.py` (Acessível em `http://localhost:8000`)
- **CLI do Barramento Multi-IA:** `python3 -m app.ai_bus.cli status`
- **Varrer Canais 24/7:** `python3 -c "from app.services.monitor import ChannelMonitor; from app.database.session import SessionLocal; ChannelMonitor(SessionLocal()).check_all_active_channels()"`
- **Verificar Matriz de Agendamento (4 vídeos/dia):** `python3 -c "from app.services.scheduler import PostScheduler; from app.database.session import SessionLocal; print(PostScheduler(SessionLocal()).get_calendar_matrix(1))"`
