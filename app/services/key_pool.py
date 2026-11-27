"""
MULTI-KEY AI POOL WITH AUTOMATIC 429 / QUOTA FAILOVER
Suporta múltiplas chaves de API por provedor (separadas por vírgula, quebra de linha ou ';').
Se uma chave retornar HTTP 429 (rate limit), 402 ou 403 quota exceeded, entra em cooldown
automático por 5 minutos e o pool tenta imediatamente a próxima chave disponível.
"""

import re
import time
import threading
import logging
from typing import Callable, List, Optional, Tuple, TypeVar

import httpx

logger = logging.getLogger("AIKeyPool")
T = TypeVar("T")

_LOCK = threading.Lock()
_COOLDOWN_UNTIL: dict[str, float] = {}
_RR_INDEX: dict[str, int] = {}

DEFAULT_COOLDOWN_SECONDS = 300.0  # 5 minutos


def parse_api_keys(raw_value: Optional[str]) -> List[str]:
    if not raw_value:
        return []
    parts = re.split(r"[\n,;]+", str(raw_value))
    keys: List[str] = []
    seen = set()
    for p in parts:
        k = p.strip()
        if k and k not in seen:
            seen.add(k)
            keys.append(k)
    return keys


def _is_retryable_key_error(exc: Exception) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in (401, 402, 403, 429):
            return True
        if code >= 500:
            return True
    msg = str(exc).lower()
    if any(tok in msg for tok in ("429", "rate limit", "quota", "resource_exhausted", "too many requests")):
        return True
    return False


def get_ordered_keys(provider: str, raw_value: Optional[str]) -> List[str]:
    keys = parse_api_keys(raw_value)
    if len(keys) <= 1:
        return keys

    now = time.time()
    with _LOCK:
        start = _RR_INDEX.get(provider, 0) % len(keys)
        _RR_INDEX[provider] = (start + 1) % len(keys)
        rotated = keys[start:] + keys[:start]
        healthy = [k for k in rotated if _COOLDOWN_UNTIL.get(f"{provider}:{k}", 0.0) <= now]
        cooling = [k for k in rotated if _COOLDOWN_UNTIL.get(f"{provider}:{k}", 0.0) > now]
        return healthy + cooling


def mark_key_cooldown(provider: str, key: str, seconds: float = DEFAULT_COOLDOWN_SECONDS) -> None:
    with _LOCK:
        _COOLDOWN_UNTIL[f"{provider}:{key}"] = time.time() + seconds


def get_pool_status(provider: str, raw_value: Optional[str]) -> dict:
    keys = parse_api_keys(raw_value)
    now = time.time()
    with _LOCK:
        cooling = sum(1 for k in keys if _COOLDOWN_UNTIL.get(f"{provider}:{k}", 0.0) > now)
    return {
        "provider": provider,
        "total_keys": len(keys),
        "healthy_keys": max(0, len(keys) - cooling),
        "cooling_keys": cooling,
    }


def execute_with_key_pool(
    provider: str,
    raw_value: Optional[str],
    call_fn: Callable[[str], T],
) -> Tuple[Optional[T], Optional[Exception]]:
    """
    Executa call_fn(api_key) rotacionando automaticamente entre todas as chaves configuradas.
    Retorna (resultado, ultimo_erro).
    """
    ordered = get_ordered_keys(provider, raw_value)
    if not ordered:
        return None, ValueError(f"Nenhuma chave configurada para {provider}")

    last_exc: Optional[Exception] = None
    for idx, key in enumerate(ordered):
        try:
            return call_fn(key), None
        except Exception as exc:
            last_exc = exc
            if _is_retryable_key_error(exc):
                mark_key_cooldown(provider, key)
                logger.warning(
                    "Chave #%d de %s falhou (%s) — failover para próxima chave do pool.",
                    idx + 1,
                    provider,
                    exc,
                )
                continue
            # Erro não relacionado a cota/chave -> interrompe
            return None, exc

    return None, last_exc
