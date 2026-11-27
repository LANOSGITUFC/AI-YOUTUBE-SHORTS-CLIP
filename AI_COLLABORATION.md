# 🤖 Guia de Colaboração Multi-IA (Claude Code, Open Code & Agentes)

Este projeto foi desenhado desde a sua raiz para permitir a colaboração contínua entre múltiplos agentes de Inteligência Artificial (**Claude Code**, **Open Code**, **Antigravity**, **Gemini**, etc.) e humanos, com **memória persistente em banco de dados** para evitar qualquer reutilização ou duplicidade de trechos cortados.

---

## ⚡ Como o Claude Code e o Open Code se Conectam

Existem 3 formas de comunicação suportadas:

### 1. Protocolo Nativo MCP (Model Context Protocol) — *Recomendado*
Tanto o **Claude Code** quanto o **Open Code** suportam servidores MCP nativamente.

Para ativar no seu Claude Code ou Open Code, adicione ao seu arquivo de configuração MCP (`mcp.json` ou `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "shorts-clipper": {
      "command": "python3",
      "args": ["-m", "app.ai_bus.mcp_server"],
      "cwd": "/caminho/para/AI-YOUTUBE-SHORTS-CLIP"
    }
  }
}
```

#### Ferramentas MCP Expostas:
- `get_system_status()`: Retorna contagem de canais ativos, vídeos processados, clipes salvos e agendamentos.
- `list_videos(status)`: Lista os vídeos recentes de Alanzoka, Paulinho Loko, LipaoGamer, etc.
- `get_unused_windows(video_id)`: **Crucial:** Retorna os intervalos exatos de tempo (em segundos) que estão LIVRES (sem nenhum corte prévio).
- `propose_clip(...)`: Registra um clipe. O banco rejeitará automaticamente se houver qualquer sobreposição com cortes anteriores.
- `get_schedule_matrix()`: Retorna a grade de 4 vídeos/dia para 7 dias por canal de destino.

---

### 2. Linha de Comando (CLI para Bash / Terminal)
Se estiver usando o Claude Code ou Open Code via terminal bash, use o módulo CLI:

```bash
# 1. Ver status geral
python3 -m app.ai_bus.cli status

# 2. Listar vídeos descobertos/transcritos
python3 -m app.ai_bus.cli list-videos

# 3. Ver trechos NÃO USADOS de um vídeo (evita duplicidade)
python3 -m app.ai_bus.cli unused-windows --video-id="7mgGgcrCwvQ"

# 4. Propor e salvar um novo corte validado
python3 -m app.ai_bus.cli propose-clip \
  --agent="claude-code" \
  --video-id="7mgGgcrCwvQ" \
  --start=125.0 \
  --end=178.5 \
  --title="ALANZOKA QUASE MORREU NO GTA 6!" \
  --hook="Você não vai acreditar no que aconteceu agora!" \
  --score=95 \
  --crop="split_screen"

# 5. Listar clipes registrados no banco
python3 -m app.ai_bus.cli list-clips
```

---

### 3. Barramento Compartilhado em Arquivo JSON (`data/agent_bus.json`)
Qualquer agente pode ler `data/agent_bus.json` para inspecionar eventos recentes disparados por outros agentes (ex: Antigravity registrando download, Claude Code aprovando corte, Editor renderizando vídeo).

---

## 🛡️ Regras do Motor de Deduplicação (Memória de Trechos Usados)

1. **Tabela `clip_segments`:** Guarda o `video_id`, `start_time` e `end_time` exatos de cada corte gerado.
2. **Buffer de Respiro:** Nenhum corte pode iniciar ou terminar a menos de 10 segundos de um corte anterior do mesmo vídeo.
3. **Bloqueio de Sobreposição:** Se um corte tiver mais de 5% de sobreposição com um trecho existente, o banco rejeita a operação com erro detalhado.
4. **Intervalos Livres (`available_unused_windows`):** Sempre consulte esta função antes de pedir para um LLM analisar a transcrição, para alimentar o LLM apenas com momentos que ainda não viraram Shorts!
