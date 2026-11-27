"""
LAYOUT ENGINE — Layout personalizado do Short (editor visual drag-and-drop).

O editor visual do painel salva em `editor_config.layout` um JSON assim:

    {
      "enabled": true,
      "has_cam": true,
      "background": "blur" | "black",
      "cam":  {"src": {x, y, w, h}, "dst": {x, y, w, h}},
      "game": {"src": {x, y, w, h}, "dst": {x, y, w, h}},
      "caption": {"x", "y", "w", "h"}
    }

- `src` é o recorte no vídeo original, em frações 0..1 da largura/altura da fonte.
- `dst` é a caixa no quadro final 1080×1920, em pixels.
- `caption` é a caixa da legenda no quadro final, em pixels.

Cada recorte preenche a sua caixa de destino no modo "cover" (escala até cobrir e
corta o excesso no centro), exatamente como a pré-visualização do editor desenha.
"""

from typing import Any, Dict, Optional

CANVAS_W = 1080
CANVAS_H = 1920
MIN_SRC = 0.05   # recorte mínimo: 5% da fonte
MIN_DST = 64     # caixa mínima no quadro final (px)
MIN_CAPTION_W = 200
MIN_CAPTION_H = 60

DEFAULT_LAYOUT: Dict[str, Any] = {
    "name": "Câmera em cima",
    "enabled": True,
    "has_cam": True,
    "background": "blur",
    "cam": {
        "src": {"x": 0.70, "y": 0.0, "w": 0.30, "h": 0.40},
        "dst": {"x": 0, "y": 0, "w": 1080, "h": 760},
    },
    "game": {
        "src": {"x": 0.1875, "y": 0.0, "w": 0.625, "h": 1.0},
        "dst": {"x": 0, "y": 760, "w": 1080, "h": 1160},
    },
    "caption": {"x": 60, "y": 1380, "w": 960, "h": 220},
}


def _f(value: Any, default: float) -> float:
    try:
        v = float(value)
        if v != v or v in (float("inf"), float("-inf")):  # NaN / inf
            return default
        return v
    except (TypeError, ValueError):
        return default


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _even(v: float) -> int:
    """FFmpeg/libx264 exige dimensões pares."""
    return max(2, int(round(v / 2.0)) * 2)


def normalize_src(raw: Any, default: Dict[str, float]) -> Dict[str, float]:
    r = raw if isinstance(raw, dict) else {}
    w = _clamp(_f(r.get("w"), default["w"]), MIN_SRC, 1.0)
    h = _clamp(_f(r.get("h"), default["h"]), MIN_SRC, 1.0)
    x = _clamp(_f(r.get("x"), default["x"]), 0.0, 1.0 - w)
    y = _clamp(_f(r.get("y"), default["y"]), 0.0, 1.0 - h)
    return {"x": round(x, 4), "y": round(y, 4), "w": round(w, 4), "h": round(h, 4)}


def normalize_dst(raw: Any, default: Dict[str, float], min_w: int = MIN_DST, min_h: int = MIN_DST) -> Dict[str, int]:
    r = raw if isinstance(raw, dict) else {}
    w = _even(_clamp(_f(r.get("w"), default["w"]), min_w, CANVAS_W))
    h = _even(_clamp(_f(r.get("h"), default["h"]), min_h, CANVAS_H))
    x = int(round(_clamp(_f(r.get("x"), default["x"]), 0, CANVAS_W - w)))
    y = int(round(_clamp(_f(r.get("y"), default["y"]), 0, CANVAS_H - h)))
    return {"x": x, "y": y, "w": w, "h": h}


def normalize_layout(raw: Any) -> Optional[Dict[str, Any]]:
    """Valida e normaliza o layout salvo. Retorna None se não houver layout ativo."""
    if not isinstance(raw, dict) or not raw.get("enabled", False):
        return None
    d = DEFAULT_LAYOUT
    cam = raw.get("cam") if isinstance(raw.get("cam"), dict) else {}
    game = raw.get("game") if isinstance(raw.get("game"), dict) else {}
    bg = str(raw.get("background") or "blur")
    name = str(raw.get("name") or "").strip()[:80] or "Layout personalizado"
    return {
        "name": name,
        "enabled": True,
        "has_cam": bool(raw.get("has_cam", True)),
        "background": bg if bg in ("blur", "black") else "blur",
        "cam": {
            "src": normalize_src(cam.get("src"), d["cam"]["src"]),
            "dst": normalize_dst(cam.get("dst"), d["cam"]["dst"]),
        },
        "game": {
            "src": normalize_src(game.get("src"), d["game"]["src"]),
            "dst": normalize_dst(game.get("dst"), d["game"]["dst"]),
        },
        "caption": normalize_dst(raw.get("caption"), d["caption"], MIN_CAPTION_W, MIN_CAPTION_H),
    }


def _crop_cover_chain(src: Dict[str, float], dst: Dict[str, int]) -> str:
    """Recorta `src` da fonte e preenche a caixa `dst` (cover + corte central)."""
    return (
        f"crop=iw*{src['w']:.4f}:ih*{src['h']:.4f}:iw*{src['x']:.4f}:ih*{src['y']:.4f},"
        f"scale={dst['w']}:{dst['h']}:force_original_aspect_ratio=increase,"
        f"crop={dst['w']}:{dst['h']},setsar=1"
    )


def build_custom_layout_filter(layout: Dict[str, Any], input_label: str = "[0:v]", output_label: str = "[v]") -> str:
    """Monta o filtergraph FFmpeg 1080×1920 para o layout personalizado."""
    lay = normalize_layout(layout) or normalize_layout(DEFAULT_LAYOUT)
    has_cam = lay["has_cam"]
    game_src, game_dst = lay["game"]["src"], lay["game"]["dst"]

    n = 3 if has_cam else 2
    labels = "[lyb][lyg][lyc]" if has_cam else "[lyb][lyg]"
    parts = [f"{input_label}split={n}{labels}"]

    # Fundo: a própria gameplay desfocada cobrindo o quadro, ou preto
    bg = (
        f"[lyb]crop=iw*{game_src['w']:.4f}:ih*{game_src['h']:.4f}:iw*{game_src['x']:.4f}:ih*{game_src['y']:.4f},"
        f"scale={CANVAS_W}:{CANVAS_H}:force_original_aspect_ratio=increase,crop={CANVAS_W}:{CANVAS_H},setsar=1"
    )
    if lay["background"] == "black":
        bg += ",drawbox=x=0:y=0:w=iw:h=ih:color=black@1:t=fill"
    else:
        bg += ",boxblur=25:5"
    parts.append(bg + "[lybg]")

    parts.append(f"[lyg]{_crop_cover_chain(game_src, game_dst)}[lygo]")
    game_out = "[lyl1]" if has_cam else output_label
    parts.append(f"[lybg][lygo]overlay={game_dst['x']}:{game_dst['y']}:shortest=1,format=yuv420p{game_out}")

    if has_cam:
        cam_src, cam_dst = lay["cam"]["src"], lay["cam"]["dst"]
        parts.append(f"[lyc]{_crop_cover_chain(cam_src, cam_dst)}[lyco]")
        parts.append(f"[lyl1][lyco]overlay={cam_dst['x']}:{cam_dst['y']}:shortest=1,format=yuv420p{output_label}")

    return ";".join(parts)


def caption_style_overrides(layout: Dict[str, Any]) -> Dict[str, Any]:
    """Converte a caixa da legenda em margens ASS (Alignment=2, ancorada embaixo no centro)."""
    lay = normalize_layout(layout)
    if not lay:
        return {}
    cap = lay["caption"]
    return {
        "margin_v": int(CANVAS_H - (cap["y"] + cap["h"])),
        "margin_l": int(cap["x"]),
        "margin_r": int(CANVAS_W - (cap["x"] + cap["w"])),
    }
