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

SEO_SYSTEM_PROMPT = """Você é um estrategista de retenção e SEO especializado em YouTube Shorts de streamers e gamers brasileiros.
Dado o nome do streamer, título do vídeo original, categoria do momento e a fala transcrita do corte, gere metadados de ALTO CTR que pareçam escritos por um editor humano brasileiro (nada robótico, sem clickbait genérico tipo "MOMENTO INSANO" ou "DESTAQUE #1", sem TUDO EM MAIÚSCULO).

Regras:
1. "title": 38 a 72 caracteres, natural em PT-BR, gera curiosidade sem entregar o final, máximo 1 emoji. Inclua o nome do streamer ou o jogo quando fizer sentido.
2. "description": 2 frases curtas em PT-BR dando contexto + uma pergunta curta no final para gerar comentários.
3. "tags": lista de 12 a 15 tags de busca curtas em minúsculas (sem #), misturando streamer, jogo, formato e termos pesquisados no Brasil.
4. "hashtags": lista de 5 hashtags (com #), sempre começando com #shorts + streamer + jogo + nicho.

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
            )

        normalized = self._normalize_seo(result, streamer, game)
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

    def _build_smart_ptbr_seo(
        self,
        streamer: str,
        video_title: str,
        game: str,
        category: str,
        transcript_excerpt: str,
        seed: int = 1,
        style_angle: Optional[str] = None,
    ) -> Dict[str, Any]:
        rng = random.Random(seed)
        in_game = f" no {game}" if (game and game != "React") else (" reagindo" if game == "React" else " na live")

        angle_templates = {
            "curiosidade": [
                f"Espera até o final pra ver o que aconteceu com o {streamer}{in_game}",
                f"Ninguém acreditou no que o {streamer} fez nesse segundo{in_game}",
                f"Olha o detalhe que quase passou batido na live do {streamer}{in_game}",
            ],
            "choque_reacao": [
                f"A reação do {streamer} ao vivo foi impagável{in_game} 😱",
                f"{streamer} travou completamente com o que viu{in_game}",
                f"O susto e a cara do {streamer} nesse momento{in_game} 😂",
            ],
            "pergunta_debate": [
                f"Isso foi sorte ou pura habilidade do {streamer}{in_game}?",
                f"Você teria reagido igual ao {streamer} nessa hora{in_game}?",
                f"Quem errou nessa jogada do {streamer}{in_game}? Comenta!",
            ],
            "direto_curto": [
                f"{streamer} quebrou tudo{in_game}",
                f"Timing absurdo do {streamer}",
                f"{streamer} não tankou isso 😂",
            ],
            "storytelling": [
                f"Tudo ia bem até o {streamer} fazer isso{in_game} 💀",
                f"O plano do {streamer} durou exatos 3 segundos{in_game}",
                f"Em 10 segundos a live do {streamer} virou caos{in_game}",
            ],
        }

        templates = {
            "scare": [
                f"{streamer} quase caiu da cadeira com esse susto{in_game} 😱",
                f"O susto que fez o {streamer} travar ao vivo{in_game}",
                f"Ninguém esperava o que apareceu pro {streamer}{in_game} 😱",
            ],
            "rage": [
                f"{streamer} perdeu a paciência ao vivo{in_game} 😂",
                f"O momento exato em que o {streamer} tiltou{in_game}",
                f"Não tinha como o {streamer} ficar calmo depois disso{in_game}",
            ],
            "clutch": [
                f"A jogada absurda do {streamer}{in_game} 🎯",
                f"Como o {streamer} saiu vivo disso{in_game}?!",
                f"{streamer} ativou o modo lenda{in_game} 🔥",
            ],
            "funny": [
                f"{streamer} não tankou o que aconteceu{in_game} 😂",
                f"O timing perfeito que quebrou o {streamer}{in_game}",
                f"A reação do {streamer} nesse momento é impagável{in_game} 😂",
            ],
            "fail": [
                f"Tudo parecia sob controle até o {streamer} vacilar{in_game} 💀",
                f"O plano do {streamer} durou 3 segundos{in_game} 😂",
            ],
            "highlight": [
                f"Olha o que aconteceu com o {streamer}{in_game}",
                f"O momento que roubou a cena na live do {streamer}{in_game}",
                f"Você precisa ver a reação do {streamer}{in_game}",
            ],
        }
        pool = angle_templates.get(style_angle or "") or templates.get(category, templates["highlight"])
        title = rng.choice(pool)
        if style_angle == "direto_curto":
            title = title[:42].strip()

        ctas = {
            "scare": "Você teria levado esse susto também? Comenta aí!",
            "rage": "Você também teria quitado nessa hora? Conta nos comentários!",
            "clutch": "De 0 a 10, que nota você dá pra essa jogada?",
            "funny": "Qual foi a parte mais engraçada pra você? Comenta!",
            "fail": "Já aconteceu algo parecido na sua gameplay? Comenta aí!",
            "highlight": "O que você achou dessa reação? Deixa nos comentários!",
        }
        angle_ctas = {
            "curiosidade": "Você esperava esse final? Comenta o segundo que te pegou!",
            "choque_reacao": "Qual nota você dá pra essa reação ao vivo?",
            "pergunta_debate": "Responde sincero nos comentários — quero ver quem concorda!",
            "direto_curto": "Curtiu o corte? Deixa o like e se inscreve!",
            "storytelling": "Já aconteceu uma virada dessas na sua gameplay? Conta aí!",
        }

        context_line = (
            f"Corte dos melhores momentos de {streamer}{in_game}."
            if game
            else f"Um dos melhores momentos recentes de {streamer}."
        )
        chosen_cta = angle_ctas.get(style_angle or "") or ctas.get(category, ctas["highlight"])
        description = f"{context_line} {chosen_cta}\n\nCréditos: {streamer}"

        streamer_low = streamer.lower()
        tags = [
            streamer_low,
            f"cortes {streamer_low}",
            f"{streamer_low} live",
            f"melhores momentos {streamer_low}",
            "cortes de lives",
            "clips gamers",
            "youtube shorts",
            "shorts brasil",
            "humor gamer",
        ]
        if game:
            g_low = game.lower()
            tags.extend([g_low, f"{g_low} brasil", f"{streamer_low} {g_low}", f"cortes {g_low}"])
        if category in ("scare", "rage", "funny", "clutch"):
            cat_tags = {
                "scare": ["susto em live", "jogos de terror"],
                "rage": ["rage gamer", "tiltou ao vivo"],
                "funny": ["momentos engraçados", "risada em live"],
                "clutch": ["jogada epica", "mitada"],
            }
            tags.extend(cat_tags[category])

        hashtags = ["#shorts", f"#{_slug_tag(streamer)}"]
        if game:
            hashtags.append(f"#{_slug_tag(game)}")
        hashtags.extend(["#cortes", "#gaming"])

        return {
            "title": title,
            "description": description,
            "tags": tags,
            "hashtags": hashtags,
        }

    def _normalize_seo(self, raw: Dict[str, Any], streamer: str, game: str) -> Dict[str, Any]:
        title = str(raw.get("title") or f"Melhor momento de {streamer}").strip()
        title = re.sub(r"\s+", " ", title)[:82]

        description = str(raw.get("description") or f"Corte de {streamer}.").strip()[:1200]

        raw_tags = raw.get("tags") or []
        if isinstance(raw_tags, str):
            raw_tags = [t.strip() for t in raw_tags.split(",")]
        seen_t = set()
        clean_tags: List[str] = []
        for t in raw_tags:
            ct = str(t).strip().lstrip("#").lower()
            if ct and ct not in seen_t:
                seen_t.add(ct)
                clean_tags.append(ct)
        if not clean_tags:
            clean_tags = [streamer.lower(), "cortes", "gaming", "youtube shorts"]

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
        if "#shorts" not in clean_hash:
            clean_hash.insert(0, "#shorts")
        clean_hash = clean_hash[:6]  # 3 a 6 hashtags é o ponto ótimo do algoritmo do YouTube Shorts

        return {
            "title": title,
            "description": description,
            "tags": clean_tags[:15],
            "tags_csv": ", ".join(clean_tags[:15]),
            "hashtags": clean_hash,
            "hashtags_str": " ".join(clean_hash),
            "detected_streamer": streamer,
            "detected_game": game,
        }
