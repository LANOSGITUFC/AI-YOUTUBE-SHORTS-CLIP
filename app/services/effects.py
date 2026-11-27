"""
VIDEO EFFECTS ENGINE (RETENÇÃO)
Gera trechos de filtro FFmpeg para efeitos de retenção em Shorts 9:16:

- Hook zoom (punch-in): começa ampliado em `zoom_factor` e volta suavemente
  para 1.0 nos primeiros `duration_sec` segundos (curva ease-out cúbica).
- Screen shake: tremor sutil de tela (deslocamento senoidal de poucos pixels),
  opcionalmente limitado a uma janela de tempo.

Os dois efeitos mantêm a resolução de saída fixa (padrão 1080x1920), então
podem ser encadeados no fim de qualquer grafo de filtros do editor.
Requer FFmpeg >= 5.0 (variável `t` no filtro scale com eval=frame).
"""

from typing import Optional

DEFAULT_WIDTH = 1080
DEFAULT_HEIGHT = 1920

# Categorias de clipe que recebem o tremor de tela
SHAKE_CATEGORIES = {"rage", "susto", "scare"}


def _fmt(value: float) -> str:
    """Formata número sem notação científica e sem zeros sobrando."""
    text = f"{float(value):.6f}".rstrip("0").rstrip(".")
    return text if text not in ("", "-0") else "0"


class VideoEffectsEngine:
    def __init__(self, width: int = DEFAULT_WIDTH, height: int = DEFAULT_HEIGHT):
        if width <= 0 or height <= 0 or width % 2 or height % 2:
            raise ValueError("width e height devem ser positivos e pares (exigência do libx264/yuv420p)")
        self.width = width
        self.height = height

    # ------------------------------------------------------------------ zoom
    def hook_zoom_expression(self, zoom_factor: float = 1.15, duration_sec: float = 3.0) -> str:
        """Expressão de zoom em função de t: zoom_factor em t=0, 1.0 a partir de duration_sec."""
        if zoom_factor < 1.0:
            raise ValueError("zoom_factor deve ser >= 1.0")
        if duration_sec <= 0:
            raise ValueError("duration_sec deve ser > 0")
        extra = _fmt(zoom_factor - 1.0)
        d = _fmt(duration_sec)
        # p = progresso restante (1 -> 0); ease-out cúbico: extra * p^3
        return f"(1+{extra}*pow(max(0,1-t/{d}),3))"

    def build_hook_zoom_filter(
        self,
        fps: int = 30,
        zoom_factor: float = 1.15,
        duration_sec: float = 3.0,
    ) -> str:
        """
        Filtro de punch-in no gancho. Entrada e saída: width x height.
        Escala o quadro por z(t) (sempre par) e recorta o centro no tamanho original.
        """
        if fps <= 0:
            raise ValueError("fps deve ser > 0")
        z = self.hook_zoom_expression(zoom_factor, duration_sec)
        w, h = self.width, self.height
        return (
            f"scale=w='trunc({w}*{z}/2)*2':h='trunc({h}*{z}/2)*2':eval=frame,"
            f"crop={w}:{h}:(iw-{w})/2:(ih-{h})/2,"
            f"setsar=1,fps={fps}"
        )

    # ----------------------------------------------------------------- shake
    def build_shake_filter(
        self,
        intensity: int = 5,
        start_sec: Optional[float] = None,
        end_sec: Optional[float] = None,
        freq_x: float = 13.0,
        freq_y: float = 17.0,
    ) -> str:
        """
        Tremor de tela de até `intensity` px em cada eixo. Entrada e saída: width x height.
        Recorta uma margem de `intensity` px de cada lado, move a janela com senos de
        frequências diferentes (movimento menos repetitivo) e volta ao tamanho original.
        Com start_sec/end_sec, o tremor só acontece nessa janela e o resto do clipe fica intacto.
        Nesse caso o retorno é um trecho de grafo (usa rótulos internos shk_*), que funciona
        quando encadeado com vírgula depois de outro filtro, como faz build_retention_chain.
        """
        if intensity < 0:
            raise ValueError("intensity deve ser >= 0")
        w, h = self.width, self.height
        if intensity == 0:
            return f"scale={w}:{h},setsar=1"
        if 2 * intensity >= min(w, h):
            raise ValueError("intensity grande demais para a resolução")
        if start_sec is not None and end_sec is not None and end_sec <= start_sec:
            raise ValueError("end_sec deve ser maior que start_sec")

        i = int(intensity)
        fx, fy = _fmt(freq_x), _fmt(freq_y)
        x = f"{i}+{i}*sin(2*PI*{fx}*t)"
        y = f"{i}+{i}*sin(2*PI*{fy}*t+1.3)"
        shaken = f"crop={w - 2 * i}:{h - 2 * i}:'{x}':'{y}',scale={w}:{h},setsar=1"

        if start_sec is None and end_sec is None:
            # Tremor no clipe inteiro (margem de `intensity` px fica cortada o tempo todo)
            return shaken

        # Tremor só na janela: a versão tremida é sobreposta ao quadro original apenas
        # entre start e end (overlay tem suporte a timeline). Fora da janela o quadro
        # sai intacto, sem o zoom residual da margem recortada.
        s = _fmt(start_sec if start_sec is not None else 0)
        e = _fmt(end_sec) if end_sec is not None else "1e9"
        return (
            f"split=2[shk_base][shk_src];"
            f"[shk_src]{shaken}[shk_fx];"
            f"[shk_base][shk_fx]overlay=0:0:enable='between(t,{s},{e})'"
        )

    # ------------------------------------------------------------ composição
    def build_retention_chain(
        self,
        category: Optional[str] = None,
        fps: int = 30,
        zoom_factor: float = 1.15,
        hook_duration_sec: float = 3.0,
        shake_intensity: int = 5,
        shake_duration_sec: float = 1.5,
        enable_zoom: bool = True,
        enable_shake: bool = True,
    ) -> str:
        """
        Cadeia linear (sem rótulos) para aplicar no fim do grafo do editor.
        O shake só entra quando a categoria é de choque/rage, no início do gancho.
        Retorna "" se nenhum efeito se aplica.
        """
        parts = []
        if enable_zoom:
            parts.append(self.build_hook_zoom_filter(fps, zoom_factor, hook_duration_sec))
        if enable_shake and (category or "").lower() in SHAKE_CATEGORIES:
            parts.append(self.build_shake_filter(shake_intensity, 0.0, shake_duration_sec))
        return ",".join(parts)

    @staticmethod
    def append_to_graph(filter_graph: str, chain: str, out_label: str = "v") -> str:
        """
        Encadeia `chain` na saída `[out_label]` de um filter_complex já existente.
        Se o grafo não terminar com esse rótulo, devolve o grafo sem alterações.
        """
        label = f"[{out_label}]"
        if not chain or not filter_graph.endswith(label):
            return filter_graph
        base = filter_graph[: -len(label)]
        return f"{base}[pre_vfx];[pre_vfx]{chain}{label}"
