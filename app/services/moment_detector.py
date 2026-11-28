"""
DETECTOR DE MELHORES MOMENTOS (MULTIMODAL)
Encontra os trechos mais fortes de uma live/vídeo longo combinando três sinais numa grade de 0,5 s:

1. ÁUDIO  — volume relativo (dB acima da mediana do vídeo), ataques súbitos (grito/susto),
            brilho espectral (grito agudo) e ritmo de envelope 3-7 Hz (risada "ha-ha-ha").
2. FALA   — gatilhos emocionais PT-BR na transcrição do Whisper (susto, raiva, clutch, risada,
            gancho), exclamações, repetições ("não não não") e aceleração da fala (palavras/s).
3. VISUAL — mudança de cena e intensidade de movimento via filtro `scene` do FFmpeg
            (cortes bruscos, gameplay intensa).

Os sinais são normalizados de forma robusta (mediana/MAD) por vídeo, fundidos com pesos e
os picos viram janelas de corte que crescem para onde o sinal continua alto e encaixam nas
frases da transcrição. Cada momento sai com início/fim, score 1-99, tipo dominante
(audio, speech, visual ou mixed), sub-scores e motivos legíveis.

Os momentos ficam na tabela `video_moments`; o usuário escolhe no painel quais viram clipes.
Toda conversão em clipe passa pelo DeduplicationEngine (LEI MÁXIMA: nada sobreposto).
"""

import os
import re
import json
import math
import logging
import datetime
import threading
import subprocess
import unicodedata
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from sqlalchemy.orm import Session

from app.database.models import ClipSegment, SourceVideo, SystemSetting, VideoMoment
from app.services.deduplicator import DeduplicationEngine

logger = logging.getLogger("MomentDetector")

MOMENTS_DIR = "./data/moments"
HOP = 0.5                 # segundos por bin da grade
SR = 8000                 # taxa de amostragem usada na análise de áudio
BIN_SAMPLES = int(SR * HOP)
ENV_SAMPLES = 160         # 20 ms por quadro de envelope
ENV_PER_BIN = BIN_SAMPLES // ENV_SAMPLES
SIGNAL_VERSION = 2

BASE_WEIGHTS = {"audio": 0.45, "speech": 0.35, "visual": 0.20}

KIND_LABELS = {"audio": "Áudio", "speech": "Fala", "visual": "Visual", "mixed": "Misto"}

# Gatilhos de fala (sem acento, minúsculos). Peso maior = mais forte.
SPEECH_LEXICON: Dict[str, List[Tuple[str, float]]] = {
    "scare": [
        ("meu deus", 1.0), ("mano do ceu", 1.1), ("nossa senhora", 1.0), ("socorro", 1.1),
        ("que susto", 1.3), ("ai meu deus", 1.2), ("eita", 0.7), ("caraca", 0.8), ("cacete", 0.8),
        ("me assustei", 1.2), ("tomei um susto", 1.3), ("o que foi isso", 1.0), ("que isso", 0.7),
        ("corre", 0.6), ("sai daqui", 0.8),
    ],
    "rage": [
        ("nao acredito", 1.1), ("mentira", 0.7), ("desgraca", 1.0), ("como assim", 0.8),
        ("ta de sacanagem", 1.1), ("impossivel", 0.9), ("roubado", 0.9), ("que odio", 1.1),
        ("palhacada", 0.9), ("ridiculo", 0.8), ("nao e possivel", 1.0), ("absurdo", 0.8),
        ("porra", 0.7), ("caralho", 0.7), ("filho da", 0.8), ("vai se", 0.6),
    ],
    "clutch": [
        ("mitei", 1.2), ("olha isso", 0.9), ("duvido", 0.7), ("vem tranquilo", 0.8),
        ("respeita", 0.9), ("jogou muito", 1.0), ("headshot", 1.0), ("acertei", 0.8),
        ("ganhamos", 1.0), ("vitoria", 0.9), ("consegui", 0.8), ("amassei", 1.0), ("que jogada", 1.1),
        ("lendario", 1.0), ("insano", 1.0), ("absurda", 0.8), ("clutch", 1.1), ("ace", 0.6),
    ],
    "fail": [
        ("morri", 1.0), ("deu ruim", 1.0), ("vacilei", 0.9), ("burrice", 0.9), ("perdi", 0.7),
        ("nao acredito que", 0.8), ("errei", 0.8), ("que vergonha", 1.0), ("me ferrei", 1.0),
    ],
    "funny": [
        ("trollei", 1.0), ("olha o que ele fez", 1.0), ("vacilou", 0.8), ("morrendo de rir", 1.3),
        ("to chorando", 1.0), ("que engracado", 1.0), ("rindo", 0.6), ("piada", 0.6),
    ],
    "highlight": [
        ("voces nao vao acreditar", 1.3), ("presta atencao", 0.9), ("olha so", 0.7),
        ("se liga", 0.7), ("espera ai", 0.8), ("calma ai", 0.8), ("nunca vi", 1.0),
        ("primeira vez", 0.7), ("historia", 0.5), ("chat", 0.3), ("gente", 0.3), ("mano", 0.25),
    ],
}

LAUGH_RE = re.compile(r"\b(k{3,}|(?:ha){2,}h?|(?:he){3,}|(?:rs){2,}|rs{2,}|(?:ja){3,})\b")
REPEAT_RE = re.compile(r"\b(\w{2,})(?:[\s,!.]+\1\b){2,}")

_JOBS: Dict[str, Dict[str, Any]] = {}
_JOBS_LOCK = threading.Lock()


def _strip_accents(text: str) -> str:
    norm = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in norm if not unicodedata.combining(ch)).lower()


def _robust_z(x: np.ndarray, mask: Optional[np.ndarray] = None, min_scale: float = 0.0) -> np.ndarray:
    """z-score por mediana/MAD. `mask` só vale se cobrir boa parte do vídeo (evita referência viciada)."""
    if x.size == 0:
        return x.astype(float)
    use_mask = mask is not None and mask.sum() >= max(10, 0.4 * x.size)
    ref = x[mask] if use_mask else x
    med = float(np.median(ref))
    mad = float(np.median(np.abs(ref - med))) * 1.4826
    scale = mad if mad > 1e-6 else float(np.std(ref))
    scale = max(scale, min_scale)
    if scale <= 1e-6:
        return np.zeros_like(x, dtype=float)
    return (x - med) / scale


def _smooth(x: np.ndarray, bins: int) -> np.ndarray:
    if bins <= 1 or x.size == 0:
        return x.astype(float)
    kernel = np.ones(bins, dtype=float) / bins
    return np.convolve(x, kernel, mode="same")


def _gauss_smooth(x: np.ndarray, sigma_bins: float) -> np.ndarray:
    if sigma_bins <= 0 or x.size == 0:
        return x.astype(float)
    radius = max(1, int(sigma_bins * 3))
    t = np.arange(-radius, radius + 1, dtype=float)
    k = np.exp(-0.5 * (t / sigma_bins) ** 2)
    return np.convolve(x, k, mode="same")


def _pool_max(x: np.ndarray, points: int) -> List[float]:
    if x.size == 0:
        return []
    if x.size <= points:
        return [round(float(v), 3) for v in x]
    edges = np.linspace(0, x.size, points + 1).astype(int)
    return [round(float(x[a:max(a + 1, b)].max()), 3) for a, b in zip(edges[:-1], edges[1:])]


def _fmt(sec: float) -> str:
    s = max(0, int(sec))
    h, rem = divmod(s, 3600)
    m, r = divmod(rem, 60)
    return f"{h}:{m:02d}:{r:02d}" if h else f"{m:02d}:{r:02d}"


def get_job_state(video_id: str) -> Dict[str, Any]:
    with _JOBS_LOCK:
        return dict(_JOBS.get(video_id) or {"state": "idle"})


def _set_job(video_id: str, **kw):
    with _JOBS_LOCK:
        cur = _JOBS.setdefault(video_id, {})
        cur.update(kw)


class MomentDetector:
    def __init__(self, db: Session):
        self.db = db
        os.makedirs(MOMENTS_DIR, exist_ok=True)

    # ------------------------------------------------------------------
    # Configuração
    # ------------------------------------------------------------------
    def _get_setting(self, key: str, default: str = "") -> str:
        row = self.db.query(SystemSetting).filter(SystemSetting.key == key).first()
        if row and row.value is not None and str(row.value).strip():
            return str(row.value).strip()
        return default

    def auto_clip_enabled(self) -> bool:
        return self._get_setting("MOMENTS_AUTO_CLIP", "true").lower() not in ("false", "0", "no", "nao", "off")

    def _clip_bounds(self) -> Tuple[float, float]:
        try:
            min_d = float(self._get_setting("MIN_CLIP_DURATION", "20"))
        except ValueError:
            min_d = 20.0
        try:
            max_d = float(self._get_setting("MAX_CLIP_DURATION", "58"))
        except ValueError:
            max_d = 58.0
        max_d = max(8.0, min(60.0, max_d))
        min_d = max(5.0, min(min_d, max_d - 3.0))
        return min_d, max_d

    # ------------------------------------------------------------------
    # 1. ÁUDIO
    # ------------------------------------------------------------------
    def _audio_features(self, source: str) -> Optional[Dict[str, np.ndarray]]:
        cmd = [
            "ffmpeg", "-nostdin", "-v", "error", "-i", source,
            "-vn", "-sn", "-dn", "-ac", "1", "-ar", str(SR), "-f", "s16le", "-",
        ]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except FileNotFoundError:
            logger.warning("FFmpeg não encontrado: análise de áudio desativada.")
            return None

        chunk_bytes = BIN_SAMPLES * 2 * 60  # 30 s por leitura
        leftover = np.zeros(0, dtype=np.int16)
        env_parts: List[np.ndarray] = []
        bright_parts: List[np.ndarray] = []
        window = np.hanning(BIN_SAMPLES).astype(np.float32)
        freqs = np.fft.rfftfreq(BIN_SAMPLES, d=1.0 / SR)
        band_all = (freqs >= 100) & (freqs <= 3900)
        band_hi = (freqs >= 1500) & (freqs <= 3900)

        def _consume(samples: np.ndarray):
            n_bins = samples.size // BIN_SAMPLES
            if n_bins == 0:
                return
            block = samples[: n_bins * BIN_SAMPLES].astype(np.float32) / 32768.0
            env = np.sqrt(np.mean(block.reshape(-1, ENV_SAMPLES) ** 2, axis=1) + 1e-12)
            env_parts.append(env.astype(np.float32))
            spec = np.abs(np.fft.rfft(block.reshape(n_bins, BIN_SAMPLES) * window, axis=1)) ** 2
            hi = spec[:, band_hi].sum(axis=1)
            tot = spec[:, band_all].sum(axis=1) + 1e-12
            bright_parts.append((hi / tot).astype(np.float32))

        try:
            assert proc.stdout is not None
            while True:
                raw = proc.stdout.read(chunk_bytes)
                if not raw:
                    break
                if len(raw) % 2:
                    raw = raw[:-1]
                data = np.frombuffer(raw, dtype=np.int16)
                buf = np.concatenate([leftover, data]) if leftover.size else data
                usable = (buf.size // BIN_SAMPLES) * BIN_SAMPLES
                _consume(buf[:usable])
                leftover = buf[usable:].copy()
            if leftover.size >= BIN_SAMPLES // 2:
                pad = np.zeros(BIN_SAMPLES - leftover.size, dtype=np.int16)
                _consume(np.concatenate([leftover, pad]))
        finally:
            try:
                proc.stdout.close()  # type: ignore[union-attr]
            except Exception:
                pass
            proc.wait()

        if not env_parts:
            return None
        env = np.concatenate(env_parts)
        bright = np.concatenate(bright_parts)
        n_bins = bright.size
        env_bins = env[: n_bins * ENV_PER_BIN].reshape(n_bins, ENV_PER_BIN)
        db = 20.0 * np.log10(np.sqrt(np.mean(env_bins ** 2, axis=1)) + 1e-6)

        # Ritmo de risada: autocorrelação do envelope (janela 2 s) nos atrasos de 140-300 ms
        rhythm = np.zeros(n_bins, dtype=np.float32)
        win = 100
        if env.size > win + 2:
            env_db = 20.0 * np.log10(env + 1e-6)
            for b in range(n_bins):
                c = b * ENV_PER_BIN + ENV_PER_BIN // 2
                seg = env_db[max(0, c - win // 2): c + win // 2]
                if seg.size < win * 0.8:
                    continue
                # Remove a tendência lenta (degraus de volume) e fica só com a pulsação rápida
                seg = seg - np.convolve(seg, np.ones(9) / 9.0, mode="same")
                seg = seg[5:-5]
                denom = float(np.dot(seg, seg))
                if denom <= 1e-6:
                    continue
                best = 0.0
                for lag in range(7, 16):
                    v = float(np.dot(seg[:-lag], seg[lag:])) / denom
                    if v > best:
                        best = v
                rhythm[b] = best

        return {"db": db.astype(np.float32), "bright": bright, "rhythm": rhythm}

    def _audio_curve(self, feats: Dict[str, np.ndarray]) -> Dict[str, np.ndarray]:
        db = feats["db"].astype(float)
        voiced = db > (np.percentile(db, 10) + 3.0)
        loud_z = _robust_z(_smooth(db, 3), voiced, min_scale=3.0)
        prev = np.convolve(db, np.ones(12) / 12.0, mode="full")[: db.size]
        prev = np.concatenate([[db[0]], prev[:-1]])
        onset_z = _robust_z(db - prev, voiced, min_scale=3.0)
        bright_z = _robust_z(feats["bright"].astype(float), voiced, min_scale=0.03)
        rhythm = feats["rhythm"].astype(float)

        loud = np.clip(loud_z, 0, 4) / 4.0
        onset = np.clip(onset_z, 0, 4) / 4.0
        bright = np.clip(bright_z, 0, 3) / 3.0 * (loud_z > 0.5)
        laugh = np.clip((rhythm - 0.35) / 0.4, 0, 1) * (loud_z > 0.3)
        curve = np.clip(0.55 * loud + 0.25 * _smooth(onset, 3) + 0.10 * bright + 0.10 * _smooth(laugh, 3), 0, 1)
        return {
            "curve": curve,
            "loud_z": loud_z,
            "onset": onset,
            "bright": bright,
            "laugh": laugh,
            "median_db": np.array([float(np.median(db[voiced])) if voiced.sum() >= 0.4 * db.size else float(np.median(db))]),
            "db": db,
        }

    # ------------------------------------------------------------------
    # 2. FALA
    # ------------------------------------------------------------------
    def _speech_signals(self, segments: List[Dict[str, Any]], n_bins: int) -> Dict[str, Any]:
        impulses = np.zeros(n_bins, dtype=float)
        words = np.zeros(n_bins, dtype=float)
        cat_w: Dict[str, np.ndarray] = {}
        hits: List[Dict[str, Any]] = []

        def _bin(t: float) -> int:
            return int(min(n_bins - 1, max(0, t / HOP)))

        def _add(t: float, w: float, cat: str, phrase: str):
            b = _bin(t)
            impulses[b] += w
            cat_w.setdefault(cat, np.zeros(n_bins, dtype=float))[b] += w
            hits.append({"t": round(t, 2), "w": w, "cat": cat, "phrase": phrase})

        for seg in segments:
            try:
                s0 = float(seg.get("start", 0.0))
                s1 = float(seg.get("end", s0))
            except (TypeError, ValueError):
                continue
            text = str(seg.get("text") or "")
            if not text.strip():
                continue
            s1 = max(s1, s0 + 0.2)
            seg_words = seg.get("words") or []
            if seg_words:
                for w in seg_words:
                    try:
                        words[_bin(float(w.get("start", s0)))] += 1
                    except (TypeError, ValueError):
                        continue
            else:
                tokens = text.split()
                for i in range(len(tokens)):
                    words[_bin(s0 + (s1 - s0) * (i + 0.5) / max(1, len(tokens)))] += 1

            plain = _strip_accents(text)
            n_chars = max(1, len(plain))

            def _t_at(pos: int) -> float:
                return s0 + (s1 - s0) * (pos / n_chars)

            for cat, entries in SPEECH_LEXICON.items():
                for phrase, weight in entries:
                    for m in re.finditer(r"\b" + re.escape(phrase) + r"\b", plain):
                        _add(_t_at(m.start()), weight, cat, phrase)
            for m in LAUGH_RE.finditer(plain):
                _add(_t_at(m.start()), 1.2, "funny", m.group(0)[:12])
            for m in REPEAT_RE.finditer(plain):
                _add(_t_at(m.start()), 0.8, "highlight", m.group(0)[:30])
            excl = text.count("!")
            if excl:
                _add(s1, min(1.5, 0.5 * excl), "highlight", "!")
            if "?" in text:
                _add(s0, 0.25, "highlight", "?")

        kw = 1.0 - np.exp(-_gauss_smooth(impulses, 2.5) / 1.4)
        rate = _smooth(words, 6) / HOP  # palavras por segundo (média de 3 s)
        speaking = words > 0
        rate_z = _robust_z(rate, _smooth(speaking.astype(float), 6) > 0.3, min_scale=0.5) if speaking.any() else np.zeros(n_bins)
        fast = np.clip(rate_z - 0.8, 0, 2.5) / 2.5 * (rate >= 2.5)
        curve = np.clip(0.8 * kw + 0.25 * fast, 0, 1)
        return {"curve": curve, "rate": rate, "fast": fast, "hits": hits, "cat_w": cat_w, "has_text": bool(speaking.any())}

    # ------------------------------------------------------------------
    # 3. VISUAL
    # ------------------------------------------------------------------
    def _visual_mode(self, duration: float) -> str:
        mode = self._get_setting("MOMENTS_VISUAL_MODE", "auto").lower()
        if mode in ("off", "false", "0", "none"):
            return "off"
        if mode in ("full", "keyframes"):
            return mode
        return "full" if duration <= 5400 else "keyframes"

    def _visual_scores(self, video_path: str, n_bins: int, duration: float, mode: str) -> Optional[np.ndarray]:
        if mode == "off" or not video_path or not os.path.exists(video_path):
            return None
        vf = "fps=2,scale=192:-2," if mode == "full" else "scale=192:-2,"
        vf += "select='gte(scene\\,0)',metadata=print:file=-"
        cmd = ["ffmpeg", "-nostdin", "-v", "error"]
        if mode == "keyframes":
            cmd += ["-skip_frame", "nokey"]
        cmd += ["-i", video_path, "-an", "-sn", "-dn", "-vf", vf, "-f", "null", "-"]
        timeout = max(240.0, duration * (0.6 if mode == "full" else 0.2))
        scores = np.full(n_bins, np.nan)
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        except FileNotFoundError:
            return None

        killer = threading.Timer(timeout, proc.kill)
        killer.start()
        last_t: Optional[float] = None
        prev_t = 0.0
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                if "pts_time:" in line:
                    try:
                        last_t = float(line.rsplit("pts_time:", 1)[1].split()[0])
                    except (ValueError, IndexError):
                        last_t = None
                elif "scene_score=" in line and last_t is not None:
                    try:
                        val = float(line.split("scene_score=", 1)[1])
                    except ValueError:
                        continue
                    b1 = int(min(n_bins - 1, max(0, last_t / HOP)))
                    b0 = b1 if mode == "full" else int(min(b1, max(0, prev_t / HOP)))
                    seg = scores[b0: b1 + 1]
                    scores[b0: b1 + 1] = np.where(np.isnan(seg), val, np.maximum(seg, val))
                    prev_t = last_t
        finally:
            killer.cancel()
            try:
                proc.stdout.close()  # type: ignore[union-attr]
            except Exception:
                pass
            proc.wait()

        if np.isnan(scores).all():
            return None
        # Preenche buracos curtos (quadros sem amostra) com o valor anterior
        filled = scores.copy()
        last = 0.0
        for i in range(filled.size):
            if np.isnan(filled[i]):
                filled[i] = last
            else:
                last = filled[i]
        return filled.astype(np.float32)

    def _visual_curve(self, scene: np.ndarray) -> Dict[str, np.ndarray]:
        motion = _smooth(scene.astype(float), 4)
        motion_z = _robust_z(motion, min_scale=0.01)
        cuts = (scene > 0.35).astype(float)
        cut_density = np.convolve(cuts, np.ones(20), mode="same")  # cortes em 10 s
        curve = np.clip(0.6 * np.clip(motion_z, 0, 4) / 4.0 + 0.4 * np.clip(cut_density / 3.0, 0, 1), 0, 1)
        return {"curve": curve, "cut_density": cut_density, "motion_z": motion_z}

    # ------------------------------------------------------------------
    # Cache de sinais
    # ------------------------------------------------------------------
    def _cache_path(self, video_id: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", video_id)
        return os.path.join(MOMENTS_DIR, f"{safe}_signals.npz")

    def _curves_path(self, video_id: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", video_id)
        return os.path.join(MOMENTS_DIR, f"{safe}_curves.json")

    def _load_or_compute_raw(self, video: SourceVideo, duration_hint: float, force: bool) -> Dict[str, Any]:
        path = self._cache_path(video.id)
        if not force and os.path.exists(path):
            try:
                data = np.load(path, allow_pickle=False)
                if int(data["version"][0]) == SIGNAL_VERSION:
                    out = {k: data[k] for k in data.files}
                    out["visual_mode"] = str(data["visual_mode"][0]) if "visual_mode" in data.files else "off"
                    return out
            except Exception as exc:
                logger.debug("Cache de sinais inválido para %s: %s", video.id, exc)

        audio_src = None
        for cand in (video.audio_path, video.video_path):
            if cand and os.path.exists(cand):
                audio_src = cand
                break
        raw: Dict[str, Any] = {"version": np.array([SIGNAL_VERSION])}
        if audio_src:
            _set_job(video.id, message="Analisando picos de áudio...")
            feats = self._audio_features(audio_src)
            if feats:
                raw.update({"db": feats["db"], "bright": feats["bright"], "rhythm": feats["rhythm"]})

        n_bins = int(raw["db"].size) if "db" in raw else int(math.ceil(max(duration_hint, 1.0) / HOP))
        duration = max(duration_hint, n_bins * HOP) if duration_hint > 0 else n_bins * HOP
        mode = self._visual_mode(duration)
        raw["visual_mode"] = mode
        if mode != "off" and video.video_path and os.path.exists(video.video_path):
            _set_job(video.id, message="Analisando mudanças visuais...")
            scene = self._visual_scores(video.video_path, n_bins, duration, mode)
            if scene is not None:
                raw["scene"] = scene

        try:
            to_save = {k: v for k, v in raw.items() if isinstance(v, np.ndarray)}
            to_save["visual_mode"] = np.array([mode])
            np.savez_compressed(path, **to_save)
        except Exception as exc:
            logger.debug("Não foi possível salvar cache de sinais: %s", exc)
        return raw

    # ------------------------------------------------------------------
    # Detecção principal
    # ------------------------------------------------------------------
    def detect(self, video_id: str, force: bool = False, max_moments: Optional[int] = None) -> List[VideoMoment]:
        """Calcula os sinais (com cache), encontra os picos e grava os momentos sugeridos."""
        video = self.db.query(SourceVideo).filter(SourceVideo.id == video_id).first()
        if not video:
            raise ValueError(f"Vídeo {video_id} não encontrado.")

        _set_job(video_id, state="running", message="Preparando análise...",
                 started_at=datetime.datetime.now(datetime.timezone.utc).isoformat(), error=None)
        try:
            moments = self._detect(video, force=force, max_moments=max_moments)
            _set_job(video_id, state="done", message=f"{len(moments)} momentos sugeridos",
                     finished_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
            return moments
        except Exception as exc:
            _set_job(video_id, state="error", message=str(exc)[:300], error=str(exc)[:300])
            raise

    def _load_transcript(self, video: SourceVideo) -> Dict[str, Any]:
        if video.transcript_path and os.path.exists(video.transcript_path):
            try:
                with open(video.transcript_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return data if isinstance(data, dict) else {}
            except Exception:
                return {}
        return {}

    def _detect(self, video: SourceVideo, force: bool, max_moments: Optional[int]) -> List[VideoMoment]:
        transcript = self._load_transcript(video)
        segments = [s for s in (transcript.get("segments") or []) if isinstance(s, dict)]
        duration_hint = float(video.duration_seconds or transcript.get("duration") or 0.0)
        if duration_hint <= 0 and segments:
            duration_hint = float(segments[-1].get("end") or 0.0)

        raw = self._load_or_compute_raw(video, duration_hint, force)
        n_bins = int(raw["db"].size) if "db" in raw else int(math.ceil(max(duration_hint, 1.0) / HOP))
        if n_bins < 10:
            raise ValueError("Vídeo sem áudio, transcrição ou duração suficiente para detectar momentos.")
        duration = max(duration_hint, n_bins * HOP) if duration_hint > 0 else n_bins * HOP
        n_bins = int(math.ceil(duration / HOP))

        def _fit(arr: np.ndarray) -> np.ndarray:
            arr = np.asarray(arr, dtype=float)
            if arr.size >= n_bins:
                return arr[:n_bins]
            return np.concatenate([arr, np.zeros(n_bins - arr.size)])

        _set_job(video.id, message="Lendo a transcrição...")
        curves: Dict[str, np.ndarray] = {}
        audio_info: Optional[Dict[str, np.ndarray]] = None
        if "db" in raw:
            audio_info = self._audio_curve({k: _fit(raw[k]) for k in ("db", "bright", "rhythm")})
            if float(audio_info["curve"].max()) > 0.05:
                curves["audio"] = audio_info["curve"]
        speech = self._speech_signals(segments, n_bins)
        if speech["has_text"]:
            curves["speech"] = speech["curve"]
        visual_info: Optional[Dict[str, np.ndarray]] = None
        if "scene" in raw:
            visual_info = self._visual_curve(_fit(raw["scene"]))
            if float(visual_info["curve"].max()) > 0.05:
                curves["visual"] = visual_info["curve"]

        if not curves:
            raise ValueError("Nenhum sinal disponível (sem áudio, fala ou vídeo em disco).")

        wsum = sum(BASE_WEIGHTS[k] for k in curves)
        weights = {k: BASE_WEIGHTS[k] / wsum for k in curves}
        combined = np.zeros(n_bins, dtype=float)
        for k, c in curves.items():
            combined += weights[k] * c
        # Concordância entre sinais vale mais que um sinal isolado
        agree = sum((c > 0.45).astype(float) for c in curves.values())
        combined = np.clip(combined * (1.0 + 0.15 * np.clip(agree - 1, 0, 2)), 0, 1)
        combined_s = _smooth(combined, 4)

        self._save_curves(video.id, duration, curves, combined_s, raw.get("visual_mode", "off"))

        min_d, max_d = self._clip_bounds()
        if max_moments is None:
            max_moments = int(min(40, max(5, duration // 90)))

        peaks = self._pick_peaks(combined_s, curves, min_sep_bins=int(min_d / HOP))
        seg_bounds = [
            (float(s.get("start", 0.0)), float(s.get("end", 0.0)), str(s.get("text") or "").strip())
            for s in segments
        ]

        # Momentos que o usuário já aceitou ou descartou nunca voltam como sugestão nova
        kept = (
            self.db.query(VideoMoment)
            .filter(VideoMoment.video_id == video.id)
            .filter(VideoMoment.status.in_(["accepted", "dismissed"]))
            .all()
        )
        self.db.query(VideoMoment).filter(VideoMoment.video_id == video.id).filter(
            VideoMoment.status == "suggested"
        ).delete(synchronize_session=False)
        self.db.flush()
        blocked = [(m.start_time, m.end_time) for m in kept]
        used_clips = (
            self.db.query(ClipSegment)
            .filter(ClipSegment.video_id == video.id)
            .filter(ClipSegment.status.in_(["analyzed", "rendered", "scheduled", "published"]))
            .all()
        )
        blocked += [(c.start_time, c.end_time) for c in used_clips]

        def _overlap_ratio(a0: float, a1: float, b0: float, b1: float) -> float:
            inter = max(0.0, min(a1, b1) - max(a0, b0))
            return inter / max(1e-6, min(a1 - a0, b1 - b0))

        chosen: List[VideoMoment] = []
        taken: List[Tuple[float, float]] = []
        for p in peaks:
            if len(chosen) >= max_moments:
                break
            s, e = self._build_window(p, combined_s, n_bins, duration, min_d, max_d, seg_bounds)
            if e - s < min_d - 0.5:
                continue
            if any(_overlap_ratio(s, e, b0, b1) > 0.35 for b0, b1 in taken):
                continue
            if any(_overlap_ratio(s, e, b0, b1) > 0.5 for b0, b1 in blocked):
                continue
            moment = self._describe(video, p, s, e, curves, weights, combined_s, audio_info, speech, visual_info, seg_bounds)
            self.db.add(moment)
            chosen.append(moment)
            taken.append((s, e))

        self.db.commit()
        for m in chosen:
            self.db.refresh(m)
        logger.info("MomentDetector: %d momentos sugeridos para %s (%s)", len(chosen), video.id, ",".join(curves))
        return chosen

    def _pick_peaks(self, combined: np.ndarray, curves: Dict[str, np.ndarray], min_sep_bins: int) -> List[int]:
        n = combined.size
        if n < 3:
            return []
        floor = max(0.12, float(np.percentile(combined, 70)))
        cand: List[Tuple[float, int]] = []
        local = (combined[1:-1] >= combined[:-2]) & (combined[1:-1] >= combined[2:])
        for i in np.nonzero(local)[0] + 1:
            if combined[i] >= floor:
                cand.append((float(combined[i]), int(i)))
        # Garante que picos fortes de um único sinal (ex.: só visual) também concorram
        for c in curves.values():
            cs = _smooth(c, 4)
            thr = max(0.35, float(np.percentile(cs, 97)))
            loc = (cs[1:-1] >= cs[:-2]) & (cs[1:-1] >= cs[2:]) & (cs[1:-1] >= thr)
            for i in np.nonzero(loc)[0] + 1:
                cand.append((float(combined[i]) * 0.9 + 0.1 * float(cs[i]), int(i)))
        cand.sort(reverse=True)
        picked: List[int] = []
        sep = max(8, min_sep_bins)
        for _, i in cand:
            if all(abs(i - j) >= sep for j in picked):
                picked.append(i)
        return picked

    def _build_window(
        self,
        peak: int,
        combined: np.ndarray,
        n_bins: int,
        duration: float,
        min_d: float,
        max_d: float,
        seg_bounds: List[Tuple[float, float, str]],
    ) -> Tuple[float, float]:
        """Cresce a janela a partir do pico para o lado onde o interesse continua alto."""
        pre = int(6 / HOP)
        post = int(4 / HOP)
        lo = max(0, peak - pre)
        hi = min(n_bins, peak + post)
        min_b = int(min_d / HOP)
        max_b = int(max_d / HOP)
        step = int(1 / HOP)
        pk = float(combined[peak]) or 1e-6
        while hi - lo < max_b:
            can_l = lo > 0
            can_r = hi < n_bins
            if not (can_l or can_r):
                break
            left = float(combined[max(0, lo - step): lo].mean()) if can_l else -1.0
            right = float(combined[hi: min(n_bins, hi + step)].mean()) if can_r else -1.0
            # Pequeno viés para o depois (reação/punchline) depois de ter 8 s de contexto antes
            if peak - lo >= int(8 / HOP):
                right *= 1.15
            if hi - lo >= min_b:
                mean_now = float(combined[lo:hi].mean())
                if max(left, right) < max(0.5 * mean_now, 0.3 * pk):
                    break
            if right >= left:
                hi = min(n_bins, hi + step)
            else:
                lo = max(0, lo - step)

        s = lo * HOP
        e = min(duration, hi * HOP)
        peak_t = peak * HOP

        # Encaixa nas frases: começa no início de uma fala e termina no fim de uma fala
        if seg_bounds:
            starts = [b[0] for b in seg_bounds if s - 5.0 <= b[0] <= min(s + 2.0, peak_t - 2.0)]
            if starts:
                s = min(starts, key=lambda t: abs(t - s) + (0.5 if t > s else 0.0))
            ends = [b[1] for b in seg_bounds if max(e - 2.0, peak_t + 3.0) <= b[1] <= e + 5.0]
            if ends:
                e = min(ends, key=lambda t: abs(t - e))
        s = max(0.0, s)
        e = min(duration, max(e, peak_t + 3.0))
        if e - s > max_d:
            # Corta do lado mais longe do pico
            if peak_t - s > e - peak_t:
                s = e - max_d
            else:
                e = s + max_d
        if e - s < min_d:
            need = min_d - (e - s)
            s = max(0.0, s - need * 0.4)
            e = min(duration, s + min_d)
            s = max(0.0, e - min_d)
        return round(s, 1), round(e, 1)

    def _describe(
        self,
        video: SourceVideo,
        peak: int,
        s: float,
        e: float,
        curves: Dict[str, np.ndarray],
        weights: Dict[str, float],
        combined: np.ndarray,
        audio_info: Optional[Dict[str, np.ndarray]],
        speech: Dict[str, Any],
        visual_info: Optional[Dict[str, np.ndarray]],
        seg_bounds: List[Tuple[float, float, str]],
    ) -> VideoMoment:
        b0, b1 = int(s / HOP), max(int(s / HOP) + 1, int(e / HOP))
        n = combined.size
        b1 = min(n, b1)
        near0, near1 = max(0, peak - 6), min(n, peak + 7)
        sub = {k: float(c[near0:near1].max()) for k, c in curves.items()}
        peak_val = float(combined[peak])
        mean_val = float(combined[b0:b1].mean()) if b1 > b0 else peak_val
        # Score: sinal mais forte + concordância de um segundo sinal + interesse sustentado na janela
        ranked_sub = sorted(sub.values(), reverse=True) + [0.0, 0.0]
        raw = 0.5 * ranked_sub[0] + 0.3 * ranked_sub[1] + 0.2 * min(1.0, 2.0 * mean_val)
        raw = max(raw, peak_val)
        score = int(round(min(99.0, max(1.0, 100.0 * (min(1.0, raw) ** 0.8)))))

        contrib = sorted(((weights[k] * v, k) for k, v in sub.items()), reverse=True)
        kind = contrib[0][1]
        if len(contrib) > 1 and contrib[1][0] >= 0.7 * contrib[0][0] and contrib[1][0] > 0.12:
            kind = "mixed"

        reasons: List[str] = []
        category_votes: Dict[str, float] = {}
        peak_t = peak * HOP

        if audio_info is not None and "audio" in curves:
            db = audio_info["db"]
            med = float(audio_info["median_db"][0])
            pk_db = float(db[near0:near1].max())
            ratio = 10 ** ((pk_db - med) / 20.0)
            if sub["audio"] > 0.3 and ratio > 1.3:
                reasons.append(f"Pico de áudio {ratio:.1f}x mais alto que a média em {_fmt(peak_t)}")
            if float(audio_info["laugh"][near0:near1].max()) > 0.5:
                reasons.append("Ritmo de risada no áudio")
                category_votes["funny"] = category_votes.get("funny", 0) + 0.8
            if float(audio_info["bright"][near0:near1].max()) > 0.5 and float(audio_info["onset"][near0:near1].max()) > 0.5:
                reasons.append("Grito ou reação súbita")
                category_votes["scare"] = category_votes.get("scare", 0) + 0.6
            elif float(audio_info["onset"][near0:near1].max()) > 0.6:
                reasons.append("Explosão de volume repentina")

        win_hits = [h for h in speech["hits"] if s - 1.0 <= h["t"] <= e + 1.0]
        phrases: List[str] = []
        for h in sorted(win_hits, key=lambda x: (-x["w"], abs(x["t"] - peak_t))):
            category_votes[h["cat"]] = category_votes.get(h["cat"], 0) + h["w"] / (1.0 + abs(h["t"] - peak_t) / 10.0)
            ph = h["phrase"]
            if ph in ("!", "?") or ph in phrases:
                continue
            if h["cat"] != "highlight" or h["w"] >= 0.7:
                phrases.append(ph)
        if phrases:
            reasons.append("Fala: " + ", ".join(f'"{p}"' for p in phrases[:4]))
        excl = sum(1 for h in win_hits if h["phrase"] == "!")
        if excl >= 2:
            reasons.append(f"{excl} frases exclamativas")
        if float(speech["fast"][b0:b1].max() if b1 > b0 else 0) > 0.5:
            pps = float(speech["rate"][b0:b1].max())
            reasons.append(f"Fala acelerada (~{pps:.1f} palavras/s)")

        if visual_info is not None and "visual" in curves:
            cuts = int(round(float(visual_info["cut_density"][near0:near1].max())))
            if cuts >= 2:
                reasons.append(f"{cuts} mudanças bruscas de cena em 10 s")
            elif cuts == 1:
                reasons.append("Mudança brusca de cena")
            elif sub["visual"] > 0.4:
                reasons.append("Movimento intenso na tela")
            if sub["visual"] > 0.5:
                category_votes["clutch"] = category_votes.get("clutch", 0) + 0.3

        category = "highlight"
        if category_votes:
            best_cat = max(category_votes.items(), key=lambda kv: kv[1])
            if best_cat[1] >= 0.5:
                category = best_cat[0]

        # Título e gancho a partir da fala mais forte perto do pico
        in_win = [b for b in seg_bounds if b[1] >= s and b[0] <= e and b[2]]
        label = ""
        if in_win:
            def _seg_rank(b: Tuple[float, float, str]) -> float:
                txt = _strip_accents(b[2])
                bonus = sum(h["w"] for h in win_hits if b[0] - 0.5 <= h["t"] <= b[1] + 0.5)
                bonus += 0.4 * b[2].count("!")
                return bonus - abs((b[0] + b[1]) / 2 - peak_t) / 20.0 + (0.2 if len(txt) > 15 else 0.0)
            best = max(in_win, key=_seg_rank)
            label = best[2]
        if len(label) > 70:
            label = label[:70].rsplit(" ", 1)[0].rstrip(" ,.;:-") + "..."
        if not label:
            label = f"Momento {KIND_LABELS.get(kind, 'forte').lower()} em {_fmt(peak_t)}"
        hook = in_win[0][2] if in_win else ""
        if len(hook) > 120:
            hook = hook[:120].rsplit(" ", 1)[0] + "..."
        if not reasons:
            reasons.append("Trecho acima da média do vídeo em energia combinada")

        return VideoMoment(
            video_id=video.id,
            start_time=s,
            end_time=e,
            peak_time=round(peak_t, 1),
            score=score,
            kind=kind,
            audio_score=int(round(100 * sub.get("audio", 0.0))),
            speech_score=int(round(100 * sub.get("speech", 0.0))),
            visual_score=int(round(100 * sub.get("visual", 0.0))),
            category=category,
            label=label[:240],
            hook_text=hook[:280],
            reasons_json=json.dumps(reasons, ensure_ascii=False),
            status="suggested",
        )

    def _save_curves(self, video_id: str, duration: float, curves: Dict[str, np.ndarray], combined: np.ndarray, visual_mode: str):
        points = 360
        payload = {
            "video_id": video_id,
            "duration": round(duration, 1),
            "points": min(points, combined.size),
            "visual_mode": visual_mode,
            "signals": sorted(curves.keys()),
            "computed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "combined": _pool_max(combined, points),
        }
        for k in ("audio", "speech", "visual"):
            payload[k] = _pool_max(curves[k], points) if k in curves else []
        try:
            with open(self._curves_path(video_id), "w", encoding="utf-8") as f:
                json.dump(payload, f)
        except Exception as exc:
            logger.debug("Não foi possível salvar curvas: %s", exc)

    def load_curves(self, video_id: str) -> Optional[Dict[str, Any]]:
        path = self._curves_path(video_id)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Consulta, aceite e descarte
    # ------------------------------------------------------------------
    def _clip_alive(self, clip_id: Optional[int]) -> bool:
        if not clip_id:
            return False
        c = self.db.query(ClipSegment).filter(ClipSegment.id == clip_id).first()
        return bool(c and c.status != "discarded")

    def serialize(self, m: VideoMoment, used: Optional[List[Tuple[float, float]]] = None) -> Dict[str, Any]:
        try:
            reasons = json.loads(m.reasons_json or "[]")
        except Exception:
            reasons = []
        status = m.status
        if status == "accepted" and not self._clip_alive(m.clip_id):
            status = "suggested"
        covered = False
        if status == "suggested" and used:
            for u0, u1 in used:
                inter = max(0.0, min(m.end_time, u1) - max(m.start_time, u0))
                if inter > 0.05 * (m.end_time - m.start_time):
                    covered = True
                    break
        return {
            "id": m.id,
            "video_id": m.video_id,
            "video_title": m.video.title if m.video else "",
            "start": round(float(m.start_time), 1),
            "end": round(float(m.end_time), 1),
            "duration": round(float(m.end_time - m.start_time), 1),
            "peak": round(float(m.peak_time), 1),
            "score": m.score,
            "kind": m.kind,
            "kind_label": KIND_LABELS.get(m.kind, m.kind),
            "audio_score": m.audio_score,
            "speech_score": m.speech_score,
            "visual_score": m.visual_score,
            "category": m.category,
            "label": m.label or "",
            "hook_text": m.hook_text or "",
            "reasons": reasons,
            "status": "covered" if covered else status,
            "clip_id": m.clip_id if status == "accepted" else None,
        }

    def used_ranges(self, video_id: str) -> List[Tuple[float, float]]:
        rows = (
            self.db.query(ClipSegment)
            .filter(ClipSegment.video_id == video_id)
            .filter(ClipSegment.status.in_(["analyzed", "rendered", "scheduled", "published"]))
            .all()
        )
        return [(c.start_time, c.end_time) for c in rows]

    def accept(self, moment_id: int, overrides: Optional[Dict[str, Any]] = None, agent: str = "moments") -> ClipSegment:
        """Transforma um momento em ClipSegment (sempre via DeduplicationEngine)."""
        overrides = overrides or {}
        m = self.db.query(VideoMoment).filter(VideoMoment.id == moment_id).first()
        if not m:
            raise LookupError("Momento não encontrado.")
        if m.status == "accepted" and self._clip_alive(m.clip_id):
            raise ValueError(f"Esse momento já virou o clipe #{m.clip_id}.")

        start = float(overrides.get("start_time", m.start_time))
        end = float(overrides.get("end_time", m.end_time))
        min_d, max_d = self._clip_bounds()
        if start < 0 or end <= start:
            raise ValueError("Intervalo inválido.")
        if end - start > 60.0:
            raise ValueError(f"Duração de {end - start:.0f}s passa do limite de 60s do Shorts.")
        if end - start < 5.0:
            raise ValueError("Duração muito curta (mínimo 5s).")

        title = str(overrides.get("title") or m.label or "Melhor momento").strip()[:240]
        dedup = DeduplicationEngine(self.db)
        is_over, reason = dedup.is_overlapping(m.video_id, start, end)
        if is_over:
            raise ValueError(f"Conflito de duplicidade: {reason}")

        try:
            reasons = json.loads(m.reasons_json or "[]")
        except Exception:
            reasons = []
        score = int(m.score or 80)
        seg = dedup.register_segment(
            video_id=m.video_id,
            start_time=start,
            end_time=end,
            title=title,
            hook_text=str(overrides.get("hook_text") or m.hook_text or "")[:280],
            summary=("; ".join(reasons))[:1000],
            virality_score=max(1, min(100, score)),
            category=(m.category or "highlight")[:45],
            suggested_by=f"{agent}:{m.kind}"[:45],
        )
        seg.hook_score = max(1, min(100, int(max(m.audio_score or 0, m.speech_score or 0, score))))
        seg.pacing_score = max(1, min(100, int(max(m.visual_score or 0, score - 4))))
        seg.story_score = max(1, min(100, int(max(m.speech_score or 0, score - 2))))
        crop = self._get_setting("CROP_MODE", "split_screen")
        if overrides.get("crop_mode"):
            crop = str(overrides["crop_mode"])
        seg.crop_mode = crop
        try:
            from app.services.seo_engine import ViralSEOEngine
            seo = ViralSEOEngine(self.db).generate_for_clip(seg, prefer_llm=False)
            seg.editor_config_json = json.dumps(
                {"seo_description": seo["description"], "seo_tags": seo["tags_csv"], "seo_hashtags": seo["hashtags_str"]},
                ensure_ascii=False,
            )
        except Exception:
            pass
        m.status = "accepted"
        m.clip_id = seg.id
        if "start_time" in overrides or "end_time" in overrides:
            m.start_time, m.end_time = round(start, 1), round(end, 1)
        self.db.commit()
        self.db.refresh(seg)
        return seg

    def mark_clips_from_candidates(self, video_id: str, clips: List[ClipSegment]):
        """Marca como aceitos os momentos cobertos por clipes criados pela esteira automática."""
        if not clips:
            return
        moments = (
            self.db.query(VideoMoment)
            .filter(VideoMoment.video_id == video_id)
            .filter(VideoMoment.status == "suggested")
            .all()
        )
        for m in moments:
            for c in clips:
                inter = max(0.0, min(m.end_time, c.end_time) - max(m.start_time, c.start_time))
                if inter >= 0.5 * (m.end_time - m.start_time):
                    m.status = "accepted"
                    m.clip_id = c.id
                    break
        self.db.commit()

    def candidates_for_analyzer(
        self,
        video_id: str,
        available_windows: List[Dict[str, float]],
        min_dur: float,
        max_dur: float,
    ) -> List[Dict[str, Any]]:
        """Momentos sugeridos no formato de candidato do HighlightAnalyzer, dentro das janelas livres."""
        rows = (
            self.db.query(VideoMoment)
            .filter(VideoMoment.video_id == video_id)
            .filter(VideoMoment.status == "suggested")
            .order_by(VideoMoment.score.desc())
            .all()
        )
        out: List[Dict[str, Any]] = []
        for m in rows:
            s, e = float(m.start_time), float(m.end_time)
            win = next((w for w in available_windows if w["start"] <= s and e <= w["end"]), None)
            if win is None:
                continue
            if e - s < min_dur or e - s > max_dur + 2.0:
                continue
            try:
                reasons = json.loads(m.reasons_json or "[]")
            except Exception:
                reasons = []
            out.append({
                "start_time": s,
                "end_time": e,
                "title": m.label or "",
                "hook_text": m.hook_text or "",
                "summary": "; ".join(reasons),
                "virality_score": int(m.score or 80),
                "hook_score": int(max(m.audio_score or 0, m.speech_score or 0, m.score or 0)),
                "pacing_score": int(max(m.visual_score or 0, (m.score or 80) - 4)),
                "story_score": int(max(m.speech_score or 0, (m.score or 80) - 2)),
                "category": m.category or "highlight",
                "moment_id": m.id,
            })
        return out

    def prompt_hints(self, video_id: str, limit: int = 15) -> str:
        """Resumo dos momentos para enriquecer o prompt da LLM."""
        rows = (
            self.db.query(VideoMoment)
            .filter(VideoMoment.video_id == video_id)
            .filter(VideoMoment.status == "suggested")
            .order_by(VideoMoment.score.desc())
            .limit(limit)
            .all()
        )
        lines = []
        for m in rows:
            try:
                reasons = json.loads(m.reasons_json or "[]")
            except Exception:
                reasons = []
            lines.append(
                f"- [{m.start_time:.0f}s - {m.end_time:.0f}s] pico {m.peak_time:.0f}s, score {m.score}, "
                f"tipo {KIND_LABELS.get(m.kind, m.kind)}: {'; '.join(reasons)[:160]}"
            )
        return "\n".join(lines)
