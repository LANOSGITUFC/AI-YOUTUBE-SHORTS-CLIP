"""
VIRAL SHORTS SEO ENGINE (TÍTULOS, DESCRIÇÕES, TAGS & HASHTAGS)
Gera metadados otimizados para CTR e algoritmo do YouTube Shorts, TikTok e Reels:
- Usa LLM real (Gemini, Groq, OpenAI, Claude ou Ollama) se configurado no banco
- Motor copywriter PT-BR inteligente offline (detecta streamer, jogo/tema, emoção e fala real)
- Nunca gera títulos genéricos como "DESTAQUE #1" ou frases cortadas no meio
- Respeita limites ideais do YouTube: título <= 78 chars, 4-6 hashtags, 12-15 tags de busca
"""

import os
import re
import json
import random
import logging
from typing import Dict, Any, List, Optional
import httpx
from sqlalchemy.orm import Session
from app.database.models import ClipSegment, SystemSetting

logger = logging.getLogger("ViralSEOEngine")

KNOWN_GAMES = [
    ("GTA 6", ["gta 6", "gta vi", "grand theft auto 6", "grand theft auto vi"]),
    ("GTA RP", ["gta rp", "cidade alta", "complexo", "paulinho", "rp", "five m", "fivem"]),
    ("GTA V", ["gta 5", "gta v", "grand theft auto v", "grand theft auto 5"]),
    ("Fortnite", ["fortnite"]),
    ("Valorant", ["valorant"]),
    ("CS2", ["cs2", "cs:go", "csgo", "counter-strike"]),
    ("Minecraft", ["minecraft"]),
    ("Resident Evil", ["resident evil", "re4", "re7", "re8", "re9"]),
    ("Silent Hill", ["silent hill"]),
    ("Lethal Company", ["lethal company"]),
    ("R.E.P.O.", ["r.e.p.o", "repo"]),
    ("Elden Ring", ["elden ring"]),
    ("Roblox", ["roblox"]),
    ("Free Fire", ["free fire"]),
    ("EA FC", ["ea fc", "fifa"]),
    ("Five Nights at Freddy's", ["fnaf", "five nights"]),
    ("Phasmophobia", ["phasmophobia"]),
    ("Outlast", ["outlast"]),
    ("Subnautica", ["subnautica"]),
    ("Red Dead", ["red dead", "rdr2"]),
    ("Cyberpunk", ["cyberpunk"]),
    ("God of War", ["god of war"]),
    ("React", ["reage", "react", "reagindo"]),
]

SEO_SYSTEM_PROMPT = """Você é um estrategista de SEO e editor brasileiro especialista em YouTube Shorts de streamers e gamers.

REGRAS OBRIGATÓRIAS:
- NUNCA repita o título genérico da live inteira! Cada corte TEM que ter um título ÚNICO e chamativo baseado no que REALMENTE é falado ou acontece naquele trecho específico.
- NUNCA invente fatos que não estejam na fala transcrita ou no contexto do vídeo.
- NUNCA corte palavras no meio e NÃO use TUDO EM MAIÚSCULO.
- Varie a estrutura do título: use citações reais da fala, perguntas instigantes, contraste de situação, humor direto ou mini-história.

Formato obrigatório:
1. "title": 30 a 72 caracteres, forte e único em PT-BR (máximo 1 emoji). Mencione o streamer e/ou o jogo de forma natural.
2. "description": 2 a 3 frases envolventes dando contexto real do corte + uma pergunta para gerar comentários.
3. "tags": lista de 22 a 30 tags de busca em minúsculas (sem #), misturando cauda curta e cauda longa (streamer, jogo, assunto falado no trecho, cortes, lives, shorts brasil, gameplay).
4. "hashtags": lista de 12 a 15 hashtags (com #), começando com #shorts + streamer + jogo + tema do trecho + nicho gamer.

Retorne APENAS JSON válido:
{
  "title": "...",
  "description": "...",
  "tags": ["tag1", "tag2"],
  "hashtags": ["#shorts", "#alanzoka", "#gta6", "#cortes", "#gaming"]
}
"""


def _clean_streamer_name(raw_name: str) -> str:
    name = (raw_name or "Streamer").strip()
    name = re.sub(r"\s*(CORTES|Lives|OFICIAL|Clips)\s*$", "", name, flags=re.IGNORECASE).strip()
    if name.lower() == "alanzoka":
        return "Alanzoka"
    if name.lower() == "piuzinho":
        return "Piuzinho"
    if name.lower() == "loudcoringa":
        return "LOUD Coringa"
    return name or "Streamer"


def _detect_game_or_topic(video_title: str) -> str:
    low = (video_title or "").lower()
    for label, patterns in KNOWN_GAMES:
        if any(p in low for p in patterns):
            return label
    return ""


def _slug_tag(text: str) -> str:
    s = (text or "").lower().strip()
    replacements = {
        "á": "a", "à": "a", "ã": "a", "â": "a",
        "é": "e", "ê": "e", "í": "i",
        "ó": "o", "ô": "o", "õ": "o",
        "ú": "u", "ç": "c",
    }
    for k, v in replacements.items():
        s = s.replace(k, v)
    return re.sub(r"[^a-z0-9]", "", s)


def _extract_json_object(raw_text: str) -> Optional[Dict[str, Any]]:
    text = (raw_text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        try:
            parsed = json.loads(m.group(0))
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    return None


class ViralSEOEngine:
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

    def _clip_transcript_excerpt(self, clip: ClipSegment) -> str:
        if not clip.video or not clip.video.transcript_path or not os.path.exists(clip.video.transcript_path):
            return clip.hook_text or ""
        try:
            with open(clip.video.transcript_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            parts = []
            for seg in data.get("segments", []):
                if float(seg.get("end", 0)) >= float(clip.start_time) and float(seg.get("start", 0)) <= float(clip.end_time):
                    txt = str(seg.get("text") or "").strip()
                    if txt:
                        parts.append(txt)
            return " ".join(parts)[:600] or (clip.hook_text or "")
        except Exception:
            return clip.hook_text or ""

    def generate_for_clip(
        self,
        clip: ClipSegment,
        prefer_llm: bool = True,
        style_angle: Optional[str] = None,
        variation_nonce: Optional[int] = None,
    ) -> Dict[str, Any]:
        raw_ch = clip.video.channel.name if (clip.video and clip.video.channel) else "Streamer"
        streamer = _clean_streamer_name(raw_ch)
        video_title = clip.video.title if clip.video else ""
        related_video_url = ""
        if clip.video:
            raw_url = (clip.video.url or "").strip()
            if raw_url.startswith("http"):
                related_video_url = raw_url
            elif clip.video.id and not str(clip.video.id).startswith("local_"):
                related_video_url = f"https://www.youtube.com/watch?v={clip.video.id}"
        game = _detect_game_or_topic(video_title)
        category = (clip.category or "highlight").lower()
        transcript_excerpt = self._clip_transcript_excerpt(clip)
        angle = (style_angle or "").strip().lower() or None

        result = None
        if prefer_llm:
            result = self._try_llm_seo(streamer, video_title, game, category, transcript_excerpt, style_angle=angle)

        if not result:
            nonce = int(variation_nonce) if variation_nonce is not None else random.randint(1, 999999)
            result = self._build_smart_ptbr_seo(
                streamer=streamer,
                video_title=video_title,
                game=game,
                category=category,
                transcript_excerpt=transcript_excerpt,
                seed=(clip.id or int(clip.start_time or 1)) + nonce + (len(angle) * 17 if angle else 0),
                style_angle=angle,
                related_video_url=related_video_url,
            )

        normalized = self._normalize_seo(
            result,
            streamer,
            game,
            related_video_url=related_video_url,
            related_video_title=video_title,
        )
        if angle:
            normalized["style_angle"] = angle
        return normalized

    def _try_llm_seo(
        self,
        streamer: str,
        video_title: str,
        game: str,
        category: str,
        transcript_excerpt: str,
        style_angle: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        provider = self._get_setting("AI_PROVIDER", default="offline").lower()
        if provider == "offline":
            return None

        angle_hints = {
            "curiosidade": "Ângulo obrigatório: CURIOSIDADE / OPEN LOOP — crie tensão sem entregar o desfecho no título.",
            "choque_reacao": "Ângulo obrigatório: REAÇÃO / CHOQUE DIRETO — foque na expressão ou reação extrema do streamer.",
            "pergunta_debate": "Ângulo obrigatório: PERGUNTA / DEBATE — faça uma pergunta direta no título ou gancho que provoque comentários.",
            "direto_curto": "Ângulo obrigatório: ULTRA-CURTO — título de no máximo 42 caracteres, seco, direto ao ponto e sem enfeites.",
            "storytelling": "Ângulo obrigatório: STORYTELLING — abra como uma mini-história ('Tudo ia bem até...', 'Em 5 segundos...').",
        }
        angle_line = f"\n{angle_hints[style_angle]}" if style_angle in angle_hints else ""

        user_prompt = (
            f"Streamer: {streamer}\n"
            f"Jogo/Tema: {game or 'Gameplay / Live'}\n"
            f"Título original do vídeo: {video_title}\n"
            f"Categoria do momento: {category}\n"
            f"Trecho falado no corte: {transcript_excerpt or '(sem transcrição — use o contexto do título)'}"
            f"{angle_line}"
        )

        try:
            if provider == "gemini":
                key = self._get_setting("GEMINI_API_KEY", env_fallback="GEMINI_API_KEY")
                if not key:
                    return None
                model = self._get_setting("GEMINI_MODEL", default="gemini-2.5-flash")
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
                payload = {
                    "system_instruction": {"parts": [{"text": SEO_SYSTEM_PROMPT}]},
                    "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
                    "generationConfig": {"temperature": 0.7, "responseMimeType": "application/json"},
                }
                with httpx.Client(timeout=25.0) as client:
                    r = client.post(url, json=payload)
                    r.raise_for_status()
                    return _extract_json_object(r.json()["candidates"][0]["content"]["parts"][0]["text"])

            if provider == "groq":
                key = self._get_setting("GROQ_API_KEY", env_fallback="GROQ_API_KEY")
                if not key:
                    return None
                model = self._get_setting("GROQ_MODEL", default="llama-3.3-70b-versatile")
                with httpx.Client(timeout=25.0) as client:
                    r = client.post(
                        "https://api.groq.com/openai/v1/chat/completions",
                        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                        json={
                            "model": model,
                            "temperature": 0.7,
                            "messages": [
                                {"role": "system", "content": SEO_SYSTEM_PROMPT},
                                {"role": "user", "content": user_prompt},
                            ],
                        },
                    )
                    r.raise_for_status()
                    return _extract_json_object(r.json()["choices"][0]["message"]["content"])

            if provider == "openai":
                key = self._get_setting("OPENAI_API_KEY", env_fallback="OPENAI_API_KEY")
                if not key:
                    return None
                model = self._get_setting("OPENAI_MODEL", default="gpt-4o-mini")
                with httpx.Client(timeout=25.0) as client:
                    r = client.post(
                        "https://api.openai.com/v1/chat/completions",
                        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                        json={
                            "model": model,
                            "temperature": 0.7,
                            "messages": [
                                {"role": "system", "content": SEO_SYSTEM_PROMPT},
                                {"role": "user", "content": user_prompt},
                            ],
                        },
                    )
                    r.raise_for_status()
                    return _extract_json_object(r.json()["choices"][0]["message"]["content"])

            if provider == "claude":
                key = self._get_setting("ANTHROPIC_API_KEY", env_fallback="ANTHROPIC_API_KEY")
                if not key:
                    return None
                model = self._get_setting("CLAUDE_MODEL", default="claude-3-5-haiku-latest")
                with httpx.Client(timeout=25.0) as client:
                    r = client.post(
                        "https://api.anthropic.com/v1/messages",
                        headers={
                            "x-api-key": key,
                            "anthropic-version": "2023-06-01",
                            "Content-Type": "application/json",
                        },
                        json={
                            "model": model,
                            "max_tokens": 512,
                            "system": SEO_SYSTEM_PROMPT,
                            "messages": [{"role": "user", "content": user_prompt}],
                        },
                    )
                    r.raise_for_status()
                    return _extract_json_object(r.json()["content"][0]["text"])

            if provider == "ollama":
                base = self._get_setting("OLLAMA_URL", env_fallback="OLLAMA_URL", default="http://localhost:11434")
                model = self._get_setting("OLLAMA_MODEL", default="llama3.1")
                with httpx.Client(timeout=45.0) as client:
                    r = client.post(
                        f"{base.rstrip('/')}/api/chat",
                        json={
                            "model": model,
                            "stream": False,
                            "messages": [
                                {"role": "system", "content": SEO_SYSTEM_PROMPT},
                                {"role": "user", "content": user_prompt},
                            ],
                        },
                    )
                    r.raise_for_status()
                    return _extract_json_object(r.json()["message"]["content"])
        except Exception as exc:
            logger.warning("SEO LLM (%s) falhou, usando motor PT-BR offline: %s", provider, exc)

        return None

    @staticmethod
    def _clean_word_truncate(text: str, max_len: int) -> str:
        cleaned = re.sub(r"[<>]+", " ", (text or ""))
        s = re.sub(r"\s+", " ", cleaned).strip(" -|:;,.")
        if len(s) <= max_len:
            return s
        return s[:max_len].rsplit(" ", 1)[0].strip(" -|:;,.")

    def _extract_spoken_keywords(self, text: str) -> List[str]:
        stop = {
            "para", "como", "mais", "muito", "isso", "esse", "essa", "aqui", "agora",
            "tava", "estava", "entao", "então", "porque", "quando", "onde", "quem",
            "qual", "sobre", "tudo", "nada", "cara", "mano", "gente", "você", "voce",
            "eles", "elas", "nosso", "nossa", "pelo", "pela", "comigo", "ainda", "assim",
            "pode", "fazer", "feito", "aqui", "dessa", "desse", "disso", "mesmo", "nunca",
        }
        words = re.findall(r"[A-Za-zÀ-ÿ0-9]{4,}", (text or "").lower())
        freq: Dict[str, int] = {}
        for w in words:
            if w in stop or w.isdigit():
                continue
            freq[w] = freq.get(w, 0) + 1
        return [w for w, _ in sorted(freq.items(), key=lambda kv: (-kv[1], -len(kv[0])))[:10]]

    def _build_smart_ptbr_seo(
        self,
        streamer: str,
        video_title: str,
        game: str,
        category: str,
        transcript_excerpt: str,
        seed: int = 1,
        style_angle: Optional[str] = None,
        related_video_url: str = "",
    ) -> Dict[str, Any]:
        rng = random.Random(seed)
        in_game = f" no {game}" if (game and game != "React") else (" reagindo" if game == "React" else " na live")

        clean_vt = re.sub(r"#[A-Za-zÀ-ÿ0-9_]+", "", video_title or "")
        clean_vt = re.sub(r"\s+", " ", clean_vt).strip(" -|!?:")
        if clean_vt and sum(1 for c in clean_vt if c.isupper()) > len(clean_vt) * 0.55:
            clean_vt = clean_vt.capitalize()
        topic = self._clean_word_truncate(clean_vt, 42)

        # Escolhe uma frase ÚNICA da transcrição deste corte (usando seed para não repetir a mesma frase)
        sentences = [
            s.strip() for s in re.split(r"[.!?\n]+", transcript_excerpt or "")
            if 14 <= len(s.strip()) <= 68
        ]
        spoken_snippet = ""
        if sentences:
            chosen_s = sentences[seed % len(sentences)]
            spoken_snippet = self._clean_word_truncate(chosen_s, 46)

        spoken_kws = self._extract_spoken_keywords(f"{transcript_excerpt} {video_title}")
        kw_focus = spoken_kws[seed % len(spoken_kws)].capitalize() if spoken_kws else ""

        if spoken_snippet:
            base_candidates = [
                f'{streamer}{in_game}: "{spoken_snippet}"',
                f'"{spoken_snippet}" — {streamer}{in_game}',
                f"{spoken_snippet} | {streamer}",
                f"Quando o {streamer} mandou: \"{self._clean_word_truncate(spoken_snippet, 34)}\"",
                f"{streamer} {in_game.strip()} — {spoken_snippet}",
                f"Olha o papo do {streamer}: \"{self._clean_word_truncate(spoken_snippet, 36)}\"",
            ]
        elif topic:
            base_candidates = [
                f"{streamer} — {topic}",
                f"{topic} ({streamer}{in_game})",
                f"O momento de {kw_focus or topic} do {streamer}{in_game}",
                f"{streamer} enfrentando {kw_focus or topic}{in_game}",
                f"Como o {streamer} lidou com {kw_focus or topic}{in_game}",
                f"{topic} | Cortes do {streamer}",
            ]
        else:
            base_candidates = [
                f"{streamer} jogando{in_game} — melhor corte",
                f"Momento da gameplay do {streamer}{in_game}",
                f"O que rolou na live do {streamer}{in_game}",
                f"Corte selecionado: {streamer}{in_game}",
            ]

        angle_templates = {
            "curiosidade": [
                f"Como o {streamer} saiu dessa{in_game}?" + (f" ({kw_focus})" if kw_focus else ""),
                f"O detalhe que quase passou batido: {streamer}{in_game}",
                f'"{spoken_snippet}" — o que aconteceu depois? ({streamer})' if spoken_snippet else f"{streamer}: {topic}",
            ],
            "choque_reacao": [
                f"A reação do {streamer} com {kw_focus or 'isso'}{in_game}",
                f'{streamer} reagindo{in_game}: "{self._clean_word_truncate(spoken_snippet, 34)}"' if spoken_snippet else f"{streamer} ao vivo{in_game}: {topic}",
            ],
            "pergunta_debate": [
                f"O que você faria no lugar do {streamer}{in_game}?",
                f"Você concordou com o {streamer} nessa?{in_game}",
                f"Faria igual ao {streamer} ou diferente?{in_game}",
            ],
            "direto_curto": [
                f'{streamer}: "{self._clean_word_truncate(spoken_snippet, 24)}"' if spoken_snippet else f"{streamer}{in_game}",
                f"{streamer} · {kw_focus or self._clean_word_truncate(topic, 24)}",
            ],
            "storytelling": [
                f"Tudo parecia normal até {kw_focus or 'esse momento'} ({streamer}{in_game})",
                f"Em poucos segundos na live do {streamer}{in_game}: {self._clean_word_truncate(spoken_snippet or topic, 28)}",
            ],
        }

        pool = angle_templates.get(style_angle or "") or base_candidates
        title = self._clean_word_truncate(rng.choice(pool), 44 if style_angle == "direto_curto" else 72)

        context_line = (
            f"Corte selecionado da live de {streamer}{in_game}"
            + (f" — {topic}." if topic else ".")
        )
        cta_line = rng.choice([
            "O que você achou desse momento? Comenta aí embaixo!",
            "Você faria diferente nessa jogada? Deixa sua opinião nos comentários!",
            "Se curtiu o corte, deixa o like e se inscreve para mais momentos de lives!",
        ])
        description = f"{context_line}\n{cta_line}"

        streamer_low = streamer.lower()
        tags = [
            streamer_low,
            f"cortes {streamer_low}",
            f"{streamer_low} live",
            f"{streamer_low} cortes",
            f"{streamer_low} gameplay",
            f"melhores momentos {streamer_low}",
            "cortes de lives",
            "cortes de streamers",
            "clips gamers",
            "clips brasil",
            "youtube shorts",
            "shorts brasil",
            "shorts gamer",
            "gameplay brasil",
            "melhores momentos",
            "lives brasil",
            "twitch brasil",
            "gaming br",
        ]
        if game:
            g_low = game.lower()
            tags.extend([
                g_low,
                f"{g_low} brasil",
                f"{g_low} gameplay",
                f"{g_low} shorts",
                f"{streamer_low} {g_low}",
                f"cortes {g_low}",
                f"melhores momentos {g_low}",
            ])
        for kw in spoken_kws[:8]:
            tags.append(kw)
            if game:
                tags.append(f"{game.lower()} {kw}")

        hashtags = [
            "#shorts",
            f"#{_slug_tag(streamer)}",
            f"#cortes{_slug_tag(streamer)}",
        ]
        if game:
            g_slug = _slug_tag(game)
            hashtags.extend([f"#{g_slug}", f"#{g_slug}brasil", f"#{g_slug}gameplay"])
        for kw in spoken_kws[:4]:
            ks = _slug_tag(kw)
            if ks and len(ks) >= 4:
                hashtags.append(f"#{ks}")
        hashtags.extend([
            "#cortes",
            "#cortesdelives",
            "#gaming",
            "#gameplay",
            "#clips",
            "#streamer",
            "#youtubeshorts",
            "#viral",
        ])

        return {
            "title": title,
            "description": description,
            "tags": tags,
            "hashtags": hashtags,
            "related_video_url": related_video_url,
        }

    def _normalize_seo(
        self,
        raw: Dict[str, Any],
        streamer: str,
        game: str,
        related_video_url: str = "",
        related_video_title: str = "",
    ) -> Dict[str, Any]:
        title = self._clean_word_truncate(str(raw.get("title") or f"Corte da live de {streamer}"), 76)

        base_desc = str(raw.get("description") or f"Corte da live de {streamer}.").strip()[:1400]

        raw_tags = raw.get("tags") or []
        if isinstance(raw_tags, str):
            raw_tags = [t.strip() for t in raw_tags.split(",")]
        seen_t = set()
        clean_tags: List[str] = []
        for t in raw_tags:
            ct = str(t).strip().lstrip("#").lower()
            if ct and len(ct) <= 45 and ct not in seen_t:
                seen_t.add(ct)
                clean_tags.append(ct)
        # Garante conjunto robusto de tags (20-30 tags)
        fallback_tags = [
            streamer.lower(),
            f"cortes {streamer.lower()}",
            f"{streamer.lower()} live",
            "cortes de lives",
            "clips gamers",
            "youtube shorts",
            "shorts brasil",
            "gameplay brasil",
            "melhores momentos",
            "streamers brasil",
            "gaming br",
        ]
        if game:
            fallback_tags.extend([game.lower(), f"{game.lower()} brasil", f"{streamer.lower()} {game.lower()}", f"cortes {game.lower()}"])
        for ft in fallback_tags:
            if ft not in seen_t:
                seen_t.add(ft)
                clean_tags.append(ft)

        # Limita tags respeitando o teto oficial do YouTube (500 chars combinados, até 30 tags)
        yt_tags: List[str] = []
        total_tag_chars = 0
        for t in clean_tags[:30]:
            if total_tag_chars + len(t) + 2 > 460:
                break
            yt_tags.append(t)
            total_tag_chars += len(t) + 2

        raw_hash = raw.get("hashtags") or []
        if isinstance(raw_hash, str):
            raw_hash = re.findall(r"#[A-Za-zÀ-ÿ0-9_]+", raw_hash) or [f"#{_slug_tag(x)}" for x in raw_hash.split() if x.strip()]
        seen_h = set()
        clean_hash: List[str] = []
        for h in raw_hash:
            slug = _slug_tag(str(h).lstrip("#"))
            if slug and slug not in seen_h:
                seen_h.add(slug)
                clean_hash.append(f"#{slug}")
        must_hash = ["#shorts", f"#{_slug_tag(streamer)}", "#cortes", "#cortesdelives", "#gaming", "#gameplay", "#clips", "#youtubeshorts"]
        if game:
            must_hash.insert(2, f"#{_slug_tag(game)}")
            must_hash.insert(3, f"#{_slug_tag(game)}brasil")
        for mh in must_hash:
            if mh and mh != "#" and mh not in seen_h:
                seen_h.add(mh)
                clean_hash.append(mh)
        if "#shorts" in clean_hash:
            clean_hash.remove("#shorts")
        clean_hash.insert(0, "#shorts")
        clean_hash = clean_hash[:15]

        # Monta descrição completa com Vídeo Longo Relacionado + Créditos + Bloco de Tags + 12-15 Hashtags
        desc_parts = [base_desc]
        rel_url = (related_video_url or raw.get("related_video_url") or "").strip()
        if rel_url and rel_url not in base_desc:
            rel_label = self._clean_word_truncate(related_video_title, 70) if related_video_title else "Assista ao vídeo completo"
            desc_parts.append(f"🎬 Vídeo longo relacionado ({rel_label}):\n{rel_url}")
        if f"Créditos: {streamer}" not in base_desc:
            desc_parts.append(f"📌 Créditos: {streamer}")
        tags_csv = ", ".join(yt_tags)
        hashtags_str = " ".join(clean_hash)
        if "Tags:" not in base_desc:
            desc_parts.append(f"🔎 Tags: {tags_csv}")
        if hashtags_str not in base_desc:
            desc_parts.append(hashtags_str)
        full_description = "\n\n".join(p for p in desc_parts if p).strip()[:4500]

        return {
            "title": title,
            "description": full_description,
            "tags": yt_tags,
            "tags_csv": tags_csv,
            "hashtags": clean_hash,
            "hashtags_str": hashtags_str,
            "related_video_url": rel_url,
            "detected_streamer": streamer,
            "detected_game": game,
        }
