"""Biblioteca de LUTs prontas (geradas uma vez, .cube 33³). O usuário não cria LUT nenhuma."""
import colorsys
from pathlib import Path

from . import paths

SIZE = 33

LUTS = {
    "epico": {"clima": "épico", "descricao": "Teal & orange, contraste alto, sombras frias e pele quente."},
    "intimista": {"clima": "intimista", "descricao": "Quente e suave, pretos levantados, contraste baixo."},
    "noir": {"clima": "noir", "descricao": "Preto e branco de alto contraste."},
    "sonho": {"clima": "sonho", "descricao": "Pastel, brilho difuso, levemente rosado."},
    "saturado": {"clima": "saturado", "descricao": "Cores vivas e contraste médio, energia alta."},
    "dessaturado": {"clima": "dessaturado", "descricao": "Cores contidas, frio, acabamento fosco."},
}


def _clamp(x):
    return 0.0 if x < 0 else 1.0 if x > 1 else x


def _contrast(x, k, pivot=0.5):
    return _clamp((x - pivot) * k + pivot)


def _sat(r, g, b, s):
    h, l, sat = colorsys.rgb_to_hls(r, g, b)
    return colorsys.hls_to_rgb(h, l, _clamp(sat * s))


def _transform(name, r, g, b):
    luma = 0.2126 * r + 0.7152 * g + 0.0722 * b
    if name == "epico":
        r, g, b = (_contrast(c, 1.18) for c in (r, g, b))
        sh, hi = 1 - luma, luma
        r += 0.06 * hi - 0.03 * sh
        g += 0.01 * hi + 0.01 * sh
        b += -0.05 * hi + 0.06 * sh
        r, g, b = _sat(_clamp(r), _clamp(g), _clamp(b), 1.1)
    elif name == "intimista":
        r, g, b = (0.06 + c * 0.9 for c in (r, g, b))
        r, g, b = (_contrast(c, 0.92) for c in (r, g, b))
        r, g, b = r * 1.05, g * 1.0, b * 0.9
        r, g, b = _sat(_clamp(r), _clamp(g), _clamp(b), 0.92)
    elif name == "noir":
        y = _contrast(luma, 1.35)
        y = y * y * (3 - 2 * y)  # curva S
        r = g = b = y
    elif name == "sonho":
        r, g, b = (0.08 + c * 0.88 for c in (r, g, b))
        r, g, b = (_contrast(c, 0.85) for c in (r, g, b))
        r, g, b = r * 1.04, g * 0.98, b * 1.04
        r, g, b = _sat(_clamp(r), _clamp(g), _clamp(b), 0.8)
        r, g, b = (c + (1 - c) * 0.08 * luma for c in (r, g, b))
    elif name == "saturado":
        r, g, b = (_contrast(c, 1.1) for c in (r, g, b))
        r, g, b = _sat(r, g, b, 1.4)
    elif name == "dessaturado":
        r, g, b = (0.03 + c * 0.94 for c in (r, g, b))
        r, g, b = _sat(r, g, b, 0.55)
        r, g, b = r * 0.97, g * 1.0, b * 1.04
    return _clamp(r), _clamp(g), _clamp(b)


def path_for(name: str) -> Path:
    if name not in LUTS:
        name = "epico"
    p = paths.LUTS / f"cortex9_{name}.cube"
    if not p.exists():
        _write(name, p)
    return p


def _write(name, p: Path):
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = [f'TITLE "CorteX9 {name}"', f"LUT_3D_SIZE {SIZE}"]
    n = SIZE - 1
    for bi in range(SIZE):
        for gi in range(SIZE):
            for ri in range(SIZE):  # vermelho varia mais rápido (padrão .cube)
                r, g, b = _transform(name, ri / n, gi / n, bi / n)
                lines.append(f"{r:.5f} {g:.5f} {b:.5f}")
    p.write_text("\n".join(lines) + "\n")


def ensure_all():
    for n in LUTS:
        path_for(n)


def catalog():
    return [{"id": k, **v} for k, v in LUTS.items()]
