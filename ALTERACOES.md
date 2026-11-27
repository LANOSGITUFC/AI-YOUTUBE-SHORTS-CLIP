# Resumo da sessão (27/11/2026)

## O que o tlas' pediu
- Checkup completo do projeto: o que funciona, o que está quebrado, testes com Playwright.
- Editar posts (clipes) e agendados.
- Rank de prioridade de arrastar e soltar, para os streamers (canais) e para os clipes (fila de postagem).
- Arrastar os agendados entre horários, dias e canais (drag and drop de verdade, com mouse e toque).
- Editor visual de layout do Short: selecionar, arrastar e redimensionar a câmera (facecam) e a gameplay, e arrastar a legenda.
- Visão geral: um "SaaS de cortes automáticos completo".
- Análise de 30 Shorts de exemplo (estilo, legendas, ganchos, ritmo).
- Depois: "sem testes por enquanto".

## O que foi alterado e entregue
Tudo está no `main`: PR #2 no commit `412f520` e PR #3 no commit `0984084`.

**Correções** (parte veio dos commits do Antigravity no `main`, parte do PR #2):
- Render FFmpeg com `-map [v]`: acabou o MP4 de 0 bytes marcado como "rendered".
- Fuso do agendamento: o `publishAt` agora vai em UTC correto (antes saía 3h adiantado).
- Segurança do painel: o texto da API é escapado no HTML (havia XSS).
- O servidor não trava mais no reload com o painel aberto (SSE limitado a 25s e shutdown de 3s).
- Layout no celular sem estourar a largura da tela.

**Novo** (PR #2):
- Editar clipes e editar ou remover agendados. Clicar no horário abre o editor de data, hora e canal.
- Rank de streamers por arrastar e soltar. O 1º recebe mais vídeos (pesos N..1).
- Fila de clipes por arrastar e soltar. A ordem define o que é renderizado e agendado primeiro.
- Arrastar agendados para outro horário ou dia (se estiver ocupado, os dois trocam de lugar) ou para outro canal.
- Upload que já foi de verdade para o YouTube fica travado.
- Endpoints: `PUT /api/channels/priority`, `GET`/`PUT /api/clips/queue`, `GET`/`PATCH /api/scheduled-posts/{id}`, `POST /api/scheduled-posts/{id}/move`.

**Editor de layout** (PR #3):
- Câmera e gameplay selecionáveis, arrastáveis e redimensionáveis, e legenda arrastável.
- Detecção de facecam, modelos prontos e controle deslizante de tamanho.

## Pendente
- Análise dos 30 Shorts de exemplo. Precisa do PC, porque o YouTube é bloqueado na nuvem.
- O arrastar com o dedo no celular não foi confirmado nos testes (19 de 21 passaram; os testes foram pausados).
- Os analisadores Gemini, Groq e OpenAI ainda são só heurística de palavras-chave.
- No PC, os pesos dos canais de destino dão 100% ao canal sem token. É preciso ajustar no painel.
