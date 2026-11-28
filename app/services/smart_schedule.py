"""
AGENDAMENTO INTELIGENTE POR CANAL
=================================

Aprende, para cada canal de destino, em quais horários da semana os Shorts rendem mais e
sugere o próximo horário ótimo livre para publicar.

Como funciona
-------------
1. Prior por nicho: cada nicho (gaming, humor, educação...) tem uma curva padrão de audiência
   por dia da semana × hora (horário local da grade, ex: BRT). É o que vale enquanto o canal
   ainda não tem histórico.

2. Histórico do canal: cada post já publicado (com views sincronizadas há 24h ou mais) vira uma
   observação no seu (dia da semana, hora). O desempenho é relativo ao próprio canal naquela
   época (mediana móvel dos posts vizinhos no tempo), então o canal crescer ou cair não distorce
   o mapa. Entram views, engajamento (likes + 2×comentários por view) e retenção média.

3. Mistura bayesiana: cada célula do mapa = prior do nicho puxado pelo histórico. Uma observação
   espalha peso para as horas vizinhas (gaussiana de ~1,25h) e, mais fraco, para a mesma hora em
   outros dias (o padrão "por hora" costuma se repetir na semana). Com pouco dado manda o nicho;
   com muito dado manda o histórico.

4. Sugestão: percorre os próximos 14 dias de 30 em 30 min, descarta horários em conflito
   (outro post do mesmo canal a menos de SMART_MIN_GAP_HOURS, vídeo já agendado direto no YouTube,
   limite diário do canal) e escolhe o melhor horário do primeiro dia que tem um horário "ótimo"
   (nota ≥ percentil 80 do mapa daquele canal).
"""

from __future__ import annotations

import datetime
import math
import statistics
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy.orm import Session

from app.database.models import DestinationChannel, ScheduledPost, SystemSetting

WEEKDAY_SHORT = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"]

OCCUPYING_STATUSES = ("pending", "failed", "awaiting_oauth", "scheduled_on_youtube", "published")

DEFAULT_NICHE = "gaming"
DEFAULT_MIN_GAP_HOURS = 3.0
HORIZON_DAYS = 14
STEP_MINUTES = 30
MIN_LEAD_MINUTES = 20          # nunca sugere algo para daqui a menos de 20 min
MIN_POST_AGE_HOURS = 24        # métricas de posts com menos de 24h ainda não dizem nada
PRIOR_STRENGTH = 3.0           # "quantos posts" o prior do nicho vale em cada célula
OPTIMAL_PERCENTILE = 0.80
HOUR_SIGMA = 1.25

# Picos de audiência (hora local, largura em horas, intensidade). "weekday" = seg-qui,
# "weekend" = sáb-dom; sexta mistura os dois (de dia como dia útil, à noite como fim de semana).
NICHE_PROFILES: Dict[str, Dict[str, Any]] = {
    "gaming": {
        "label": "Gaming / Lives",
        "weekday": [(12.5, 1.2, 0.45), (20.0, 2.0, 1.0), (23.0, 1.2, 0.45)],
        "weekend": [(11.0, 1.5, 0.55), (15.5, 2.0, 0.85), (20.5, 2.0, 1.0)],
    },
    "humor": {
        "label": "Humor / Entretenimento",
        "weekday": [(7.5, 1.0, 0.35), (12.5, 1.2, 0.7), (21.0, 1.8, 1.0)],
        "weekend": [(11.5, 1.5, 0.6), (16.0, 2.0, 0.75), (21.0, 2.0, 1.0)],
    },
    "educacao": {
        "label": "Educação / Curiosidades",
        "weekday": [(7.0, 1.0, 0.6), (12.5, 1.2, 0.75), (19.5, 1.8, 1.0)],
        "weekend": [(10.5, 1.5, 0.7), (17.0, 2.0, 0.7), (20.0, 1.5, 0.75)],
    },
    "tecnologia": {
        "label": "Tecnologia",
        "weekday": [(8.0, 1.0, 0.55), (12.5, 1.2, 0.8), (19.5, 1.8, 1.0)],
        "weekend": [(11.0, 1.5, 0.75), (16.0, 2.0, 0.7), (20.0, 1.8, 0.85)],
    },
    "esportes": {
        "label": "Esportes",
        "weekday": [(12.5, 1.2, 0.6), (22.0, 1.5, 1.0)],
        "weekend": [(13.0, 1.5, 0.75), (18.5, 1.5, 1.0), (22.0, 1.5, 0.8)],
    },
    "musica": {
        "label": "Música",
        "weekday": [(12.0, 1.2, 0.5), (18.5, 1.5, 0.85), (21.0, 1.5, 1.0)],
        "weekend": [(15.0, 2.0, 0.8), (21.5, 2.0, 1.0)],
    },
    "geral": {
        "label": "Geral",
        "weekday": [(12.5, 1.2, 0.7), (19.5, 2.0, 1.0)],
        "weekend": [(11.5, 1.5, 0.7), (19.0, 2.5, 1.0)],
    },
}


# ---------------------------------------------------------------------------
# utilidades
# ---------------------------------------------------------------------------

def _circ_hour_dist(a: float, b: float) -> float:
    d = abs(a - b) % 24.0
    return min(d, 24.0 - d)


def _gauss(dist: float, sigma: float) -> float:
    return math.exp(-(dist * dist) / (2.0 * sigma * sigma))


def _percentile(values: Sequence[float], q: float) -> float:
    vals = sorted(values)
    if not vals:
        return 0.0
    pos = (len(vals) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    if lo == hi:
        return vals[lo]
    return vals[lo] + (vals[hi] - vals[lo]) * (pos - lo)


def normalize_niche(value: Optional[str]) -> str:
    v = (value or "").strip().lower()
    v = v.replace("ç", "c").replace("ã", "a").replace("ú", "u").replace("é", "e")
    return v if v in NICHE_PROFILES else DEFAULT_NICHE


def list_niches() -> List[Dict[str, str]]:
    return [{"id": k, "label": v["label"]} for k, v in NICHE_PROFILES.items()]


def _get_setting(db: Session, key: str, default: str) -> str:
    row = db.query(SystemSetting).filter(SystemSetting.key == key).first()
    return (row.value or "").strip() if (row and row.value) else default


def get_min_gap_hours(db: Session) -> float:
    try:
        return max(0.5, min(24.0, float(_get_setting(db, "SMART_MIN_GAP_HOURS", str(DEFAULT_MIN_GAP_HOURS)))))
    except ValueError:
        return DEFAULT_MIN_GAP_HOURS


def is_smart_auto_enabled(db: Session) -> bool:
    return _get_setting(db, "SMART_SCHEDULE_AUTO", "false").lower() in ("true", "1", "yes", "sim")


# ---------------------------------------------------------------------------
# prior por nicho
# ---------------------------------------------------------------------------

def niche_prior(niche: str) -> List[List[float]]:
    """Matriz 7×24 (dia da semana × hora) com valores 0..1 para o nicho."""
    profile = NICHE_PROFILES[normalize_niche(niche)]

    def curve(peaks: Iterable[Tuple[float, float, float]], hour: float) -> float:
        val = 0.06
        for center, width, amp in peaks:
            val += amp * _gauss(_circ_hour_dist(hour, center), width)
        # madrugada (2h-6h) quase ninguém está rolando o feed
        if 2 <= hour <= 6:
            val *= 0.35
        return val

    matrix: List[List[float]] = []
    for wd in range(7):
        row = []
        for h in range(24):
            if wd <= 3:
                v = curve(profile["weekday"], h)
            elif wd == 4:  # sexta: dia útil até 17h, fim de semana à noite
                mix = 0.0 if h < 17 else 1.0
                v = (1 - mix) * curve(profile["weekday"], h) + mix * curve(profile["weekend"], h)
            else:
                v = curve(profile["weekend"], h)
            row.append(v)
        matrix.append(row)

    flat = [v for row in matrix for v in row]
    lo, hi = min(flat), max(flat)
    span = (hi - lo) or 1.0
    return [[(v - lo) / span for v in row] for row in matrix]


# ---------------------------------------------------------------------------
# histórico do canal
# ---------------------------------------------------------------------------

def _post_publish_dt(post: ScheduledPost) -> Optional[datetime.datetime]:
    dt = post.published_at or post.scheduled_for
    if dt is not None and dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


def _is_real_youtube_post(post: ScheduledPost) -> bool:
    vid = post.youtube_video_id or ""
    return bool(vid) and not vid.startswith("yt_sim_")


def collect_history(db: Session, channel_id: int) -> List[Dict[str, Any]]:
    """
    Posts do canal que já foram ao ar com métricas utilizáveis, cada um com uma nota relativa
    (z) ao desempenho típico do canal naquela época.
    """
    now = datetime.datetime.now()
    posts = (
        db.query(ScheduledPost)
        .filter(ScheduledPost.destination_channel_id == channel_id)
        .filter(ScheduledPost.status.in_(("published", "scheduled_on_youtube")))
        .all()
    )

    rows = []
    for p in posts:
        if not _is_real_youtube_post(p):
            continue
        pub = _post_publish_dt(p)
        if pub is None or pub > now:
            continue
        measured_at = p.metrics_updated_at
        if measured_at is not None and measured_at.tzinfo is not None:
            measured_at = measured_at.astimezone().replace(tzinfo=None)
        if measured_at is None or (measured_at - pub).total_seconds() < MIN_POST_AGE_HOURS * 3600:
            continue
        views = max(0, int(p.views_count or 0))
        likes = max(0, int(p.likes_count or 0))
        comments = max(0, int(p.comments_count or 0))
        rows.append({
            "post_id": p.id,
            "dt": pub,
            "views": views,
            "eng": (likes + 2 * comments) / max(views, 1),
            "ret": float(p.avg_retention_pct or 0.0),
        })

    if not rows:
        return []

    rows.sort(key=lambda r: r["dt"])
    window = 6  # vizinhos de cada lado para a mediana móvel (tira o efeito de crescimento do canal)
    rets = [r["ret"] for r in rows if r["ret"] > 0]
    ret_median = statistics.median(rets) if rets else 0.0

    for i, r in enumerate(rows):
        neigh = rows[max(0, i - window): i + window + 1]
        base_views = statistics.median([n["views"] for n in neigh])
        base_eng = statistics.median([n["eng"] for n in neigh])
        z = math.log((r["views"] + 10.0) / (base_views + 10.0))
        z += 0.5 * math.log((r["eng"] + 0.005) / (base_eng + 0.005))
        if r["ret"] > 0 and ret_median > 0:
            z += 0.3 * (r["ret"] - ret_median) / 25.0
        r["z"] = max(-3.0, min(3.0, z))
    return rows


# ---------------------------------------------------------------------------
# mapa de calor
# ---------------------------------------------------------------------------

def build_heatmap(db: Session, channel: DestinationChannel) -> Dict[str, Any]:
    niche = normalize_niche(getattr(channel, "niche", None))
    prior = niche_prior(niche)
    history = collect_history(db, channel.id)

    # Prior em escala "z" (mesma ordem de grandeza da log-razão de views entre posts)
    flat_prior = [v for row in prior for v in row]
    p_mean = statistics.fmean(flat_prior)
    p_std = statistics.pstdev(flat_prior) or 1.0
    prior_z = [[0.45 * (v - p_mean) / p_std for v in row] for row in prior]

    sum_w = [[0.0] * 24 for _ in range(7)]
    sum_wz = [[0.0] * 24 for _ in range(7)]
    samples = [[0] * 24 for _ in range(7)]
    for obs in history:
        wd, hr = obs["dt"].weekday(), obs["dt"].hour + obs["dt"].minute / 60.0
        samples[wd][obs["dt"].hour] += 1
        obs_weekend = wd >= 5
        for d in range(7):
            if d == wd:
                day_w = 1.0
            elif (d >= 5) == obs_weekend:
                day_w = 0.45
            else:
                day_w = 0.25
            for h in range(24):
                w = day_w * _gauss(_circ_hour_dist(h + 0.5, hr), HOUR_SIGMA)
                if w < 0.02:
                    continue
                sum_w[d][h] += w
                sum_wz[d][h] += w * obs["z"]

    raw = [[0.0] * 24 for _ in range(7)]
    for d in range(7):
        for h in range(24):
            n = sum_w[d][h]
            mean_obs = (sum_wz[d][h] / n) if n > 0 else 0.0
            raw[d][h] = (prior_z[d][h] * PRIOR_STRENGTH + mean_obs * n) / (PRIOR_STRENGTH + n)

    flat = [v for row in raw for v in row]
    lo, hi = min(flat), max(flat)
    span = (hi - lo) or 1.0
    scores = [[round(100.0 * (v - lo) / span) for v in row] for row in raw]

    n_posts = len(history)
    if n_posts == 0:
        source = "nicho"
    elif n_posts < 12:
        source = "misto"
    else:
        source = "historico"

    flat_scores = [v for row in scores for v in row]
    threshold = _percentile(flat_scores, OPTIMAL_PERCENTILE)

    best_cells = sorted(
        ((scores[d][h], d, h) for d in range(7) for h in range(24)),
        reverse=True,
    )
    top = []
    for sc, d, h in best_cells:
        # um destaque por dia/faixa para não listar 19h, 20h, 21h do mesmo sábado
        if any(t["weekday"] == d and abs(t["hour"] - h) < 3 for t in top):
            continue
        top.append({"weekday": d, "day": WEEKDAY_SHORT[d], "hour": h, "label": f"{WEEKDAY_SHORT[d]} {h:02d}h", "score": sc})
        if len(top) >= 5:
            break

    return {
        "channel_id": channel.id,
        "channel_name": channel.name,
        "niche": niche,
        "niche_label": NICHE_PROFILES[niche]["label"],
        "source": source,
        "posts_used": n_posts,
        "confidence": round(n_posts / (n_posts + 12.0), 2),
        "weekdays": WEEKDAY_SHORT,
        "scores": scores,
        "samples": samples,
        "optimal_threshold": round(threshold),
        "top_slots": top,
    }


def _score_at(scores: List[List[float]], dt: datetime.datetime) -> float:
    """Nota de um horário específico, interpolando entre a hora cheia e a próxima."""
    h, m = dt.hour, dt.minute
    a = scores[dt.weekday()][h]
    nxt = dt + datetime.timedelta(hours=1)
    b = scores[nxt.weekday()][nxt.hour]
    return a + (b - a) * (m / 60.0)


# ---------------------------------------------------------------------------
# conflitos
# ---------------------------------------------------------------------------

def _busy_times(db: Session, channel: DestinationChannel, exclude_post_id: Optional[int]) -> List[datetime.datetime]:
    q = (
        db.query(ScheduledPost)
        .filter(ScheduledPost.destination_channel_id == channel.id)
        .filter(ScheduledPost.status.in_(OCCUPYING_STATUSES))
    )
    if exclude_post_id is not None:
        q = q.filter(ScheduledPost.id != exclude_post_id)
    busy = [p.scheduled_for for p in q.all() if p.scheduled_for]

    try:
        from app.services.uploader import fetch_channel_youtube_timeline
        local_ids = {
            p.youtube_video_id
            for p in db.query(ScheduledPost).filter(ScheduledPost.destination_channel_id == channel.id)
            .filter(ScheduledPost.youtube_video_id.isnot(None)).all()
        }
        for item in fetch_channel_youtube_timeline(db, channel):
            if item.get("datetime") and item.get("youtube_video_id") not in local_ids:
                busy.append(item["datetime"])
    except Exception:
        pass
    return busy


def find_conflict(
    db: Session,
    channel_id: int,
    when: datetime.datetime,
    exclude_post_id: Optional[int] = None,
    window_minutes: int = 30,
) -> Optional[ScheduledPost]:
    """Outro post do mesmo canal a até `window_minutes` desse horário (mesmo horário/canal = conflito)."""
    delta = datetime.timedelta(minutes=window_minutes)
    q = (
        db.query(ScheduledPost)
        .filter(ScheduledPost.destination_channel_id == channel_id)
        .filter(ScheduledPost.status.in_(OCCUPYING_STATUSES))
        .filter(ScheduledPost.scheduled_for >= when - delta)
        .filter(ScheduledPost.scheduled_for <= when + delta)
    )
    if exclude_post_id is not None:
        q = q.filter(ScheduledPost.id != exclude_post_id)
    return q.first()


# ---------------------------------------------------------------------------
# sugestões
# ---------------------------------------------------------------------------

def _candidates(
    db: Session,
    channel: DestinationChannel,
    heatmap: Dict[str, Any],
    exclude_post_id: Optional[int] = None,
    now: Optional[datetime.datetime] = None,
) -> List[Tuple[datetime.datetime, float]]:
    now = now or datetime.datetime.now()
    gap = datetime.timedelta(hours=get_min_gap_hours(db))
    daily_limit = max(1, int(channel.daily_post_limit or 4))
    busy = _busy_times(db, channel, exclude_post_id)

    per_day: Dict[datetime.date, int] = {}
    for b in busy:
        per_day[b.date()] = per_day.get(b.date(), 0) + 1

    start = now + datetime.timedelta(minutes=MIN_LEAD_MINUTES)
    start = start.replace(second=0, microsecond=0)
    rem = start.minute % STEP_MINUTES
    if rem:
        start += datetime.timedelta(minutes=STEP_MINUTES - rem)
    end = now + datetime.timedelta(days=HORIZON_DAYS)

    out: List[Tuple[datetime.datetime, float]] = []
    t = start
    scores = heatmap["scores"]
    while t <= end:
        if per_day.get(t.date(), 0) < daily_limit and not any(abs(t - b) < gap for b in busy):
            out.append((t, _score_at(scores, t)))
        t += datetime.timedelta(minutes=STEP_MINUTES)
    return out


def next_optimal_slot(
    db: Session,
    channel: DestinationChannel,
    exclude_post_id: Optional[int] = None,
    heatmap: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Próximo horário ótimo livre: o melhor horário do primeiro dia que tem um horário com nota
    acima do limiar "ótimo" do canal. Se nenhum dia dos próximos 14 tiver, devolve o melhor livre.
    """
    heatmap = heatmap or build_heatmap(db, channel)
    cands = _candidates(db, channel, heatmap, exclude_post_id)
    if not cands:
        return None
    threshold = heatmap["optimal_threshold"]

    by_day: Dict[datetime.date, List[Tuple[datetime.datetime, float]]] = {}
    for dt, sc in cands:
        by_day.setdefault(dt.date(), []).append((dt, sc))

    chosen = None
    for day in sorted(by_day):
        good = [c for c in by_day[day] if c[1] >= threshold]
        if good:
            chosen = max(good, key=lambda c: (c[1], -c[0].timestamp()))
            break
    if chosen is None:
        chosen = max(cands, key=lambda c: (c[1], -c[0].timestamp()))
    return _slot_payload(chosen[0], chosen[1], threshold)


def suggest_slots(
    db: Session,
    channel: DestinationChannel,
    count: int = 5,
    exclude_post_id: Optional[int] = None,
    heatmap: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Os `count` melhores horários livres, preferindo os mais próximos (perde 2 pontos por dia)."""
    heatmap = heatmap or build_heatmap(db, channel)
    cands = _candidates(db, channel, heatmap, exclude_post_id)
    now = datetime.datetime.now()
    gap = datetime.timedelta(hours=get_min_gap_hours(db))
    threshold = heatmap["optimal_threshold"]

    ranked = sorted(
        cands,
        key=lambda c: c[1] - 2.0 * ((c[0] - now).total_seconds() / 86400.0),
        reverse=True,
    )
    picked: List[Tuple[datetime.datetime, float]] = []
    for dt, sc in ranked:
        if any(abs(dt - p[0]) < gap for p in picked):
            continue
        picked.append((dt, sc))
        if len(picked) >= max(1, count):
            break
    picked.sort(key=lambda c: c[0])
    return [_slot_payload(dt, sc, threshold) for dt, sc in picked]


def _slot_payload(dt: datetime.datetime, score: float, threshold: float) -> Dict[str, Any]:
    today = datetime.date.today()
    if dt.date() == today:
        day_label = "Hoje"
    elif dt.date() == today + datetime.timedelta(days=1):
        day_label = "Amanhã"
    else:
        day_label = f"{WEEKDAY_SHORT[dt.weekday()]} {dt.strftime('%d/%m')}"
    return {
        "datetime": dt.isoformat(timespec="minutes"),
        "label": f"{day_label} {dt.strftime('%H:%M')}",
        "score": round(score),
        "is_optimal": score >= threshold,
    }


def pick_channel_for_clip(db: Session) -> Optional[DestinationChannel]:
    """Canal ativo mais "atrasado" em relação ao seu peso (%) — mesma regra do auto-preencher."""
    channels = [
        ch for ch in db.query(DestinationChannel).filter(DestinationChannel.is_active == True).all()  # noqa: E712
        if int(ch.weight or 0) > 0
    ]
    if not channels:
        return None
    total = sum(int(ch.weight or 0) for ch in channels)
    best, best_ratio = None, None
    for ch in channels:
        share = int(ch.weight or 0) / total
        active = (
            db.query(ScheduledPost)
            .filter(ScheduledPost.destination_channel_id == ch.id)
            .filter(ScheduledPost.status.in_(("pending", "awaiting_oauth", "scheduled_on_youtube")))
            .count()
        )
        ratio = active / share
        if best_ratio is None or ratio < best_ratio:
            best, best_ratio = ch, ratio
    return best
