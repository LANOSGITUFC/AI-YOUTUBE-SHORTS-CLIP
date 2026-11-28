"""
Render seguro por clipe.

1. Trava por clipe: só um FFmpeg por vez grava o MP4 de um clipe, mesmo se o worker 24/7,
   o botão "Render" e o "Salvar e renderizar" pedirem ao mesmo tempo. A trava vale entre
   threads (threading.Lock) e entre processos (lockfile com fcntl, quando o SO tem).
2. Validação real do MP4: ffprobe lê o cabeçalho e o FFmpeg decodifica o vídeo inteiro.
   Um arquivo com "Invalid NAL unit size" passa no ffprobe de cabeçalho, mas falha
   na decodificação, então é reprovado aqui.
"""

import os
import json
import time
import logging
import threading
import subprocess
from contextlib import contextmanager
from typing import Dict, Iterator, Optional, Tuple

try:
    import fcntl  # Linux/macOS
except ImportError:  # Windows: só a trava entre threads do mesmo processo
    fcntl = None

logger = logging.getLogger("RenderGuard")

LOCKS_DIR = "./data/clips/.locks"
MIN_MP4_BYTES = 4096

_GUARD = threading.Lock()
_THREAD_LOCKS: Dict[int, threading.Lock] = {}
_BUSY: Dict[int, float] = {}

# Cache da validação: (caminho, tamanho, mtime) -> válido? Evita decodificar o mesmo MP4 toda volta do worker.
_PROBE_CACHE: Dict[Tuple[str, int, int], bool] = {}
_PROBE_CACHE_MAX = 2000


class RenderBusyError(RuntimeError):
    """O clipe já está sendo renderizado por outro pedido."""

    def __init__(self, clip_id: int):
        super().__init__(f"Clipe #{clip_id} já está renderizando. Aguarde terminar.")
        self.clip_id = clip_id


def _thread_lock(clip_id: int) -> threading.Lock:
    with _GUARD:
        lock = _THREAD_LOCKS.get(clip_id)
        if lock is None:
            lock = threading.Lock()
            _THREAD_LOCKS[clip_id] = lock
        return lock


def is_rendering(clip_id: int) -> bool:
    """True se este processo está renderizando o clipe agora."""
    with _GUARD:
        return clip_id in _BUSY


@contextmanager
def clip_render_lock(clip_id: int, wait: bool = True, timeout: float = 900.0) -> Iterator[None]:
    """
    Trava exclusiva de render do clipe.
    wait=True: espera o render em andamento terminar (até `timeout` segundos) e depois roda.
    wait=False: se já está renderizando, levanta RenderBusyError na hora.
    """
    tlock = _thread_lock(clip_id)
    acquired = tlock.acquire(timeout=timeout) if wait else tlock.acquire(blocking=False)
    if not acquired:
        raise RenderBusyError(clip_id)

    fh = None
    try:
        if fcntl is not None:
            os.makedirs(LOCKS_DIR, exist_ok=True)
            fh = open(os.path.join(LOCKS_DIR, f"clip_{clip_id}.lock"), "a+")
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except (BlockingIOError, PermissionError):
                    if not wait or time.monotonic() >= deadline:
                        raise RenderBusyError(clip_id)
                    time.sleep(0.5)
        with _GUARD:
            _BUSY[clip_id] = time.time()
        try:
            yield
        finally:
            with _GUARD:
                _BUSY.pop(clip_id, None)
    finally:
        if fh is not None:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            fh.close()
        tlock.release()


def _probe_header(path: str) -> bool:
    probe = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=codec_name,width,height:format=duration",
            "-of", "json",
            path,
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    if probe.returncode != 0 or (probe.stderr or "").strip():
        logger.warning("ffprobe reprovou %s: %s", path, (probe.stderr or "").strip()[:300])
        return False
    data = json.loads(probe.stdout or "{}")
    streams = data.get("streams") or []
    if not streams:
        return False
    width = int(streams[0].get("width") or 0)
    height = int(streams[0].get("height") or 0)
    duration = float((data.get("format") or {}).get("duration") or 0.0)
    return width > 0 and height > 0 and duration > 0.5


def _decode_ok(path: str) -> bool:
    """Decodifica o arquivo inteiro. Qualquer erro do decodificador (ex: Invalid NAL unit size) reprova."""
    res = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-i", path, "-map", "0:v:0", "-map", "0:a?", "-f", "null", "-"],
        capture_output=True,
        text=True,
        timeout=300,
    )
    err = (res.stderr or "").strip()
    if res.returncode != 0 or err:
        logger.warning("Decodificação reprovou %s: %s", path, err[:300])
        return False
    return True


def validate_mp4(path: Optional[str], deep: bool = True) -> bool:
    """
    MP4 pronto para agendar/postar?
    - Tamanho mínimo, cabeçalho válido no ffprobe (com vídeo, largura/altura e duração).
    - deep=True: também decodifica o vídeo inteiro para pegar quadros corrompidos.
    Resultado fica em cache por (caminho, tamanho, mtime).
    """
    if not path or not os.path.isfile(path):
        return False
    try:
        st = os.stat(path)
    except OSError:
        return False
    if st.st_size <= MIN_MP4_BYTES:
        return False

    key = (os.path.abspath(path) + ("#deep" if deep else ""), st.st_size, st.st_mtime_ns)
    cached = _PROBE_CACHE.get(key)
    if cached is not None:
        return cached

    try:
        ok = _probe_header(path) and (not deep or _decode_ok(path))
    except FileNotFoundError:
        # Sem ffprobe/ffmpeg instalado: só dá para checar o tamanho
        logger.warning("ffprobe/ffmpeg não encontrado; validando %s só pelo tamanho.", path)
        return True
    except Exception as exc:
        logger.warning("Falha ao validar MP4 %s: %s", path, exc)
        return False

    if len(_PROBE_CACHE) >= _PROBE_CACHE_MAX:
        _PROBE_CACHE.clear()
    _PROBE_CACHE[key] = ok
    return ok


def remember_valid(path: str, deep: bool = True) -> None:
    """Registra no cache que o arquivo (já validado antes de ser movido) está bom."""
    try:
        st = os.stat(path)
    except OSError:
        return
    _PROBE_CACHE[(os.path.abspath(path) + ("#deep" if deep else ""), st.st_size, st.st_mtime_ns)] = True
