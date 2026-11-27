"""
RANK DE PRIORIDADE DOS STREAMERS (arrastar e soltar no painel)
O 1º lugar tem a maior prioridade de vídeo e o último, a menor.
Com N streamers, o 1º vale peso N, o 2º N-1 ... o último 1. A esteira escolhe o streamer com
menor (vídeos já processados / peso): quem está no topo recebe proporcionalmente mais vídeos,
mas ninguém fica parado para sempre. Canais sem posição (rank 0, ex: recém-adicionados) vão para o fim.
"""

from typing import Dict, List
from sqlalchemy.orm import Session
from app.database.models import SourceChannel


def ranked_channels(db: Session) -> List[SourceChannel]:
    channels = db.query(SourceChannel).all()
    return sorted(channels, key=lambda c: ((c.priority_rank or 0) <= 0, c.priority_rank or 0, c.id))


def get_priority_weights(db: Session) -> Dict[str, int]:
    channels = ranked_channels(db)
    n = len(channels)
    return {ch.channel_id: n - idx for idx, ch in enumerate(channels)}


def priority_key(weights: Dict[str, int], channel_id: str, used_count: int) -> tuple:
    """Chave de ordenação: menor = vai primeiro. Empate -> streamer mais alto no rank."""
    weight = max(1, weights.get(channel_id, 1))
    return (used_count / weight, -weight)
