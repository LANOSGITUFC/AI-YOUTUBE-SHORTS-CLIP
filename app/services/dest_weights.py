"""
Pesos (%) dos canais de destino.

Regra: a soma dos pesos dos canais ATIVOS fecha sempre em 100%. Canal pausado fica com peso 0
e guarda o peso que tinha em `paused_weight`; ao ser reativado ele recebe esse peso de volta e
os demais encolhem proporcionalmente.

Todos os pesos são inteiros; o arredondamento usa o método do maior resto, então a soma
nunca passa nem fica abaixo do total pedido (ex.: 3 canais -> 34, 33, 33).
"""
from typing import Dict, List, Optional, Sequence

from sqlalchemy.orm import Session

from app.database.models import DestinationChannel


def largest_remainder(shares: Dict[int, float], total: int) -> Dict[int, int]:
    """Converte participações (qualquer escala >= 0) em inteiros que somam exatamente `total`."""
    if not shares or total <= 0:
        return {k: 0 for k in shares}
    s = sum(max(0.0, v) for v in shares.values())
    if s <= 0:
        shares = {k: 1.0 for k in shares}
        s = float(len(shares))
    exact = {k: max(0.0, v) * total / s for k, v in shares.items()}
    out = {k: int(v) for k, v in exact.items()}
    missing = total - sum(out.values())
    # Desempate estável: maior resto primeiro, depois o id menor
    order = sorted(exact.keys(), key=lambda k: (-(exact[k] - out[k]), k))
    for k in order[:missing]:
        out[k] += 1
    return out


def _active(db: Session, exclude_id: Optional[int] = None) -> List[DestinationChannel]:
    q = db.query(DestinationChannel).filter(DestinationChannel.is_active == True)  # noqa: E712
    if exclude_id is not None:
        q = q.filter(DestinationChannel.id != exclude_id)
    return q.order_by(DestinationChannel.id.asc()).all()


def _apply(channels: Sequence[DestinationChannel], weights: Dict[int, int]) -> None:
    for ch in channels:
        ch.weight = int(weights.get(ch.id, 0))


def fill_to_100(db: Session) -> Dict[int, int]:
    """Completa (ou corta) o que falta para 100% proporcionalmente aos pesos atuais dos ativos."""
    active = _active(db)
    weights = largest_remainder({c.id: float(c.weight or 0) for c in active}, 100)
    _apply(active, weights)
    return weights


def split_equally(db: Session) -> Dict[int, int]:
    active = _active(db)
    weights = largest_remainder({c.id: 1.0 for c in active}, 100)
    _apply(active, weights)
    return weights


def _paused_plan_total(db: Session, exclude_id: Optional[int] = None) -> int:
    """Soma dos pesos guardados (escala "todos ativos") dos canais pausados."""
    q = db.query(DestinationChannel).filter(DestinationChannel.is_active == False)  # noqa: E712
    if exclude_id is not None:
        q = q.filter(DestinationChannel.id != exclude_id)
    return sum(max(0, int(c.paused_weight or 0)) for c in q.all())


def pause_channel(db: Session, ch: DestinationChannel) -> None:
    """
    Pausa e reparte o peso do canal entre os outros ativos, na proporção dos pesos deles.

    O peso guardado fica na escala "todos os canais ativos" (o plano original), para que pausar
    e reativar vários canais em qualquer ordem volte exatamente à distribuição de antes.
    """
    others = _active(db, exclude_id=ch.id)
    scale = max(0, 100 - _paused_plan_total(db, exclude_id=ch.id)) / 100.0
    ch.paused_weight = int(round(int(ch.weight or 0) * scale))
    ch.weight = 0
    ch.is_active = False
    if others:
        _apply(others, largest_remainder({c.id: float(c.weight or 0) for c in others}, 100))


def resume_channel(db: Session, ch: DestinationChannel) -> None:
    """Reativa devolvendo o peso guardado; os outros ativos encolhem proporcionalmente."""
    others = _active(db, exclude_id=ch.id)
    if not others:
        restored = 100
    elif ch.paused_weight is None:
        restored = 100 // (len(others) + 1)  # canal antigo sem peso guardado: fatia igual
    else:
        plan = max(0, int(ch.paused_weight))
        # Fatia dos que continuam ativos no plano original (ainda sem os outros pausados)
        remaining_plan = max(0, 100 - _paused_plan_total(db, exclude_id=ch.id))
        restored = 0 if remaining_plan <= 0 else int(round(plan * 100 / remaining_plan))
        restored = max(0, min(100, restored))
    ch.is_active = True
    ch.weight = restored
    ch.paused_weight = None
    if others:
        _apply(others, largest_remainder({c.id: float(c.weight or 0) for c in others}, 100 - restored))


def release_weight_of_removed(db: Session, removed_id: int) -> None:
    """Depois de apagar um canal, os ativos restantes voltam a somar 100%."""
    others = _active(db, exclude_id=removed_id)
    if others:
        _apply(others, largest_remainder({c.id: float(c.weight or 0) for c in others}, 100))
