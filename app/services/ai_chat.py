"""
STUDIO AI CHAT ENGINE (CHAT DIRETO COM AS IAs NO PAINEL)
Conecta o usuário diretamente com Groq, OpenAI, Gemini ou Anthropic (usando AIKeyPool com failover),
com contexto ao vivo dos melhores clipes (score >= 88), canais e agendamentos,
e gera propostas executáveis em 1 clique para Vídeos Longos (16:9), Shorts Compilados (9:16) e limpeza de agenda.
"""

import os
import re
import json
import logging
import datetime
from typing import Any, Dict, List, Optional, Tuple

import httpx
from sqlalchemy.orm import Session, joinedload

from app.database.models import ClipSegment, DestinationChannel, ScheduledPost, SystemSetting
from app.services.key_pool import execute_with_key_pool, parse_api_keys

logger = logging.getLogger("StudioAIChat")

CHAT_HISTORY_KEY = "AI_CHAT_HISTORY"


class StudioAIChat:
    def __init__(self, db: Session):
        self.db = db

    def _get_setting(self, key: str, env_fallback: Optional[str] = None, default: str = "") -> str:
        row = self.db.query(SystemSetting).filter(SystemSetting.key == key).first()
        if row and row.value and row.value.strip():
            return row.value.strip()
        if env_fallback:
            val = os.getenv(env_fallback, "").strip()
            if val:
                return val
        return default

    def get_history(self) -> List[Dict[str, Any]]:
        row = self.db.query(SystemSetting).filter(SystemSetting.key == CHAT_HISTORY_KEY).first()
        if not row or not row.value:
            return []
        try:
            data = json.loads(row.value)
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def save_history(self, messages: List[Dict[str, Any]]) -> None:
        trimmed = messages[-40:]
        payload = json.dumps(trimmed, ensure_ascii=False)
        row = self.db.query(SystemSetting).filter(SystemSetting.key == CHAT_HISTORY_KEY).first()
        if row:
            row.value = payload
        else:
            self.db.add(SystemSetting(key=CHAT_HISTORY_KEY, value=payload))
        self.db.commit()

    def clear_history(self) -> None:
        self.save_history([])

    def _build_studio_context(self) -> Tuple[str, List[Dict[str, Any]]]:
        min_score = 88
        ms_row = self.db.query(SystemSetting).filter(SystemSetting.key == "MIN_VIRALITY_SCORE").first()
        if ms_row and ms_row.value:
            try:
                min_score = max(1, min(100, int(str(ms_row.value).strip())))
            except Exception:
                min_score = 88

        top_clips = (
            self.db.query(ClipSegment)
            .options(joinedload(ClipSegment.video))
            .filter(ClipSegment.status != "discarded")
            .filter(ClipSegment.virality_score >= min_score)
            .order_by(ClipSegment.virality_score.desc(), ClipSegment.created_at.desc())
            .limit(25)
            .all()
        )
        all_sched = (
            self.db.query(ScheduledPost)
            .options(joinedload(ScheduledPost.clip))
            .filter(ScheduledPost.status.in_(["pending", "awaiting_oauth", "scheduled_on_youtube"]))
            .all()
        )
        low_score_sched = sum(1 for p in all_sched if p.clip and int(p.clip.virality_score or 0) < min_score)
        channels = self.db.query(DestinationChannel).filter(DestinationChannel.is_active == True).all()

        clip_summaries = []
        lines = []
        for c in top_clips:
            streamer = c.video.channel.name if (c.video and c.video.channel) else "Streamer"
            item = {
                "id": c.id,
                "title": c.title,
                "score": int(c.virality_score or 0),
                "duration": round(float(c.duration or 0), 1),
                "streamer": streamer,
                "status": c.status,
            }
            clip_summaries.append(item)
            lines.append(f"- Clipe #{c.id} [Score {item['score']} | {item['duration']}s | {item['status']}] ({streamer}): {c.title}")

        ctx = (
            f"ESTADO ATUAL DO ESTÚDIO:\n"
            f"- Score mínimo exigido para agendamento: {min_score}\n"
            f"- Posts agendados atualmente: {len(all_sched)} (sendo {low_score_sched} com score abaixo de {min_score})\n"
            f"- Canais de destino ativos: {', '.join(ch.name for ch in channels) or 'Nenhum'}\n"
            f"- Melhores clipes disponíveis (score >= {min_score}):\n"
            + ("\n".join(lines) if lines else "(Nenhum clipe com score >= 88 ainda)")
        )
        return ctx, clip_summaries

    def _detect_actions_from_intent(self, user_message: str, reply_text: str, top_clips: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        low = (user_message or "").lower()
        actions: List[Dict[str, Any]] = []

        # Extrai tema limpo da mensagem do usuário
        theme = re.sub(r"\b(faz|faça|cria|crie|monta|monte|gera|gere|um|uma|uns|umas|video|vídeo|longo|compilado|compilados|short|shorts|por favor|pra mim|de|do|da|sobre|com)\b", " ", user_message, flags=re.IGNORECASE)
        theme = re.sub(r"\s+", " ", theme).strip(" .,!?-:")
        if len(theme) < 3:
            theme = "Melhores Momentos GTA 6"

        wants_long = any(k in low for k in ("longo", "compilado", "compilação", "compilacao", "16:9", "horizontal", "tema"))
        wants_short = any(k in low for k in ("short", "shorts", "9:16", "vertical", "compilado", "tema"))
        wants_cancel = any(k in low for k in ("cancelar", "cancela", "limpar agenda", "apagar agendado", "score 66", "score baixo", "abaixo de 88"))

        if wants_cancel:
            actions.append({
                "type": "cancel_scheduled",
                "label": "🗑 Cancelar TODOS os agendados",
                "payload": {"only_below_score": None, "delete_from_youtube": False},
            })
            actions.append({
                "type": "cancel_scheduled",
                "label": "🧹 Cancelar só agendados com score < 88",
                "payload": {"only_below_score": 88, "delete_from_youtube": False},
            })

        if wants_long:
            actions.append({
                "type": "build_compilation",
                "label": f"🎬 Renderizar Vídeo Longo 16:9 — {theme[:36]}",
                "payload": {
                    "theme": theme,
                    "mode": "long_16_9",
                    "title": f"COMPILADO: {theme} (Melhores Momentos)",
                    "min_score": 88,
                },
            })

        if wants_short:
            actions.append({
                "type": "build_compilation",
                "label": f"⚡ Renderizar Short Compilado 9:16 — {theme[:34]}",
                "payload": {
                    "theme": theme,
                    "mode": "short_9_16",
                    "title": f"{theme} — Melhores Cortes #shorts",
                    "min_score": 88,
                },
            })

        return actions[:4]

    def _call_provider(self, provider: str, system_prompt: str, messages: List[Dict[str, str]]) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """Tenta chamar um provedor via AIKeyPool. Retorna (texto, provider_usado, erro)."""
        if provider == "groq":
            raw_keys = self._get_setting("GROQ_API_KEY", env_fallback="GROQ_API_KEY")
            if not parse_api_keys(raw_keys):
                return None, None, "GROQ_API_KEY não configurada"
            model = self._get_setting("GROQ_MODEL", default="llama-3.3-70b-versatile")
            def _do(key: str) -> str:
                with httpx.Client(timeout=35.0) as client:
                    r = client.post(
                        "https://api.groq.com/openai/v1/chat/completions",
                        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                        json={
                            "model": model,
                            "temperature": 0.7,
                            "messages": [{"role": "system", "content": system_prompt}, *messages],
                        },
                    )
                    r.raise_for_status()
                    return r.json()["choices"][0]["message"]["content"]
            res, err = execute_with_key_pool("groq", raw_keys, _do)
            return res, ("groq" if res else None), (str(err) if err else None)

        if provider == "openai":
            raw_keys = self._get_setting("OPENAI_API_KEY", env_fallback="OPENAI_API_KEY")
            if not parse_api_keys(raw_keys):
                return None, None, "OPENAI_API_KEY não configurada"
            model = self._get_setting("OPENAI_MODEL", default="gpt-4o-mini")
            def _do(key: str) -> str:
                with httpx.Client(timeout=35.0) as client:
                    r = client.post(
                        "https://api.openai.com/v1/chat/completions",
                        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                        json={
                            "model": model,
                            "temperature": 0.7,
                            "messages": [{"role": "system", "content": system_prompt}, *messages],
                        },
                    )
                    r.raise_for_status()
                    return r.json()["choices"][0]["message"]["content"]
            res, err = execute_with_key_pool("openai", raw_keys, _do)
            return res, ("openai" if res else None), (str(err) if err else None)

        if provider == "gemini":
            raw_keys = self._get_setting("GEMINI_API_KEY", env_fallback="GEMINI_API_KEY")
            if not parse_api_keys(raw_keys):
                return None, None, "GEMINI_API_KEY não configurada"
            model = self._get_setting("GEMINI_MODEL", default="gemini-2.5-flash")
            contents = [
                {"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["content"]}]}
                for m in messages
            ]
            def _do(key: str) -> str:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
                with httpx.Client(timeout=35.0) as client:
                    r = client.post(
                        url,
                        json={
                            "system_instruction": {"parts": [{"text": system_prompt}]},
                            "contents": contents,
                            "generationConfig": {"temperature": 0.7},
                        },
                    )
                    r.raise_for_status()
                    return r.json()["candidates"][0]["content"]["parts"][0]["text"]
            res, err = execute_with_key_pool("gemini", raw_keys, _do)
            return res, ("gemini" if res else None), (str(err) if err else None)

        if provider == "anthropic":
            raw_keys = self._get_setting("ANTHROPIC_API_KEY", env_fallback="ANTHROPIC_API_KEY")
            if not parse_api_keys(raw_keys):
                return None, None, "ANTHROPIC_API_KEY não configurada"
            model = self._get_setting("ANTHROPIC_MODEL", default="claude-3-5-sonnet-latest")
            def _do(key: str) -> str:
                with httpx.Client(timeout=35.0) as client:
                    r = client.post(
                        "https://api.anthropic.com/v1/messages",
                        headers={
                            "x-api-key": key,
                            "anthropic-version": "2023-06-01",
                            "Content-Type": "application/json",
                        },
                        json={
                            "model": model,
                            "max_tokens": 1200,
                            "system": system_prompt,
                            "messages": messages,
                        },
                    )
                    r.raise_for_status()
                    return r.json()["content"][0]["text"]
            res, err = execute_with_key_pool("anthropic", raw_keys, _do)
            return res, ("anthropic" if res else None), (str(err) if err else None)

        return None, None, f"Provedor desconhecido: {provider}"

    def send_message(self, user_message: str, provider: str = "auto") -> Dict[str, Any]:
        text = (user_message or "").strip()
        if not text:
            raise ValueError("Digite uma mensagem para a IA.")

        ctx, top_clips = self._build_studio_context()
        system_prompt = (
            "Você é o Diretor Criativo e Estrategista de YouTube do Clipper Studio (PT-BR). "
            "Você ajuda o criador a escolher temas de Vídeos Longos compilados (16:9), Shorts compilados (9:16), "
            "melhorar títulos/SEO/tags e manter apenas clipes de alta qualidade (score >= 88). "
            "Responda de forma direta, prática e criativa em PT-BR, citando IDs reais dos clipes disponíveis quando relevante.\n\n"
            + ctx
        )

        history = self.get_history()
        recent_for_llm = [
            {"role": m["role"], "content": m["content"]}
            for m in history[-10:]
            if m.get("role") in ("user", "assistant") and m.get("content")
        ]
        recent_for_llm.append({"role": "user", "content": text})

        pref = (provider or "auto").strip().lower()
        if pref == "auto":
            configured = self._get_setting("AI_PROVIDER", default="groq").lower()
            order = []
            for p in [configured, "groq", "gemini", "openai", "anthropic"]:
                if p in ("groq", "gemini", "openai", "anthropic") and p not in order:
                    order.append(p)
        else:
            order = [pref]

        reply_text: Optional[str] = None
        used_provider = "offline"
        last_err = None
        for prov in order:
            res, used, err = self._call_provider(prov, system_prompt, recent_for_llm)
            if res:
                reply_text = res.strip()
                used_provider = used or prov
                break
            last_err = err

        if not reply_text:
            top_preview = "\n".join(
                f"- Clipe #{c['id']} (Score {c['score']} · {c['streamer']}): {c['title']}"
                for c in top_clips[:6]
            ) or "Nenhum clipe com score >= 88 encontrado ainda."
            reply_text = (
                f"Montei uma proposta baseada no seu estúdio (modo local — configure uma chave Groq/OpenAI/Gemini em IA & Configurações para respostas avançadas).\n\n"
                f"**Melhores clipes disponíveis (Score ≥ 88):**\n{top_preview}\n\n"
                f"Use os botões abaixo para renderizar direto o **Vídeo Longo (16:9)** ou o **Short Compilado (9:16)** com esse tema!"
            )

        actions = self._detect_actions_from_intent(text, reply_text, top_clips)

        now_iso = datetime.datetime.now().isoformat()
        user_entry = {"role": "user", "content": text, "ts": now_iso}
        ai_entry = {
            "role": "assistant",
            "content": reply_text,
            "provider": used_provider,
            "actions": actions,
            "ts": now_iso,
        }
        history.extend([user_entry, ai_entry])
        self.save_history(history)

        return {
            "reply": ai_entry,
            "provider": used_provider,
            "warning": last_err if used_provider == "offline" else None,
            "history": history[-40:],
        }
