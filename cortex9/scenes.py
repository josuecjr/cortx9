"""Motor de cenas do vídeo de fotos.

Cada cena é desenhada quadro a quadro em Python (Pillow) com posições fracionárias (subpixel) e
enviada ao FFmpeg como vídeo bruto: zoom e pan contínuos, sem os "trancos" do zoompan.

Acabamento (2.5):
  * recorte inteligente: rostos (OpenCV) ou, sem rostos, o ponto de maior interesse; rostos ficam
    no terço superior e nunca são cortados pelo enquadramento;
  * layouts editoriais: fendas finas, cantos retos, entrada por revelação (máscara que abre com
    leve paralaxe da foto), sem "pop" de escala;
  * colagem com composição fixa (foto principal no centro, as outras atrás), rotações pequenas,
    entrada deslizando e assentando, fundo desfocado com vinheta.

Layouts:
  unica    1 foto em tela cheia, câmera suave e pulso na batida (opcional)
  dupla    2 fotos lado a lado (ou empilhadas no vertical)
  trio     1 foto grande + 2 menores
  grade    4 fotos em grade 2×2
  colagem  3 a 5 fotos impressas sobre a mesa
  rajada   3 a 6 fotos em cortes rápidos na batida (picos da música)
"""
import hashlib
import json
import math
import random
import subprocess
from pathlib import Path

LAYOUTS = {"unica": (1, 1), "dupla": (2, 2), "trio": (3, 3), "grade": (4, 4), "colagem": (3, 5), "rajada": (3, 6)}
MIN_BEATS = {"unica": 2, "dupla": 4, "trio": 5, "grade": 6, "colagem": 6, "rajada": 3}
BG = (11, 11, 13)          # fundo dos layouts editoriais
ENGINE_VERSION = 6          # muda a chave de cache das cenas quando o desenho muda


def layout_for_count(n):
    return {1: "unica", 2: "dupla", 3: "trio", 4: "grade"}.get(n, "colagem" if n <= 5 else "rajada")


# ------------------------------------------------------------------ curvas
def clamp01(p):
    return min(1.0, max(0.0, p))


def ease_sine(p):
    return 0.5 - 0.5 * math.cos(math.pi * clamp01(p))


def ease_motion(p):
    """Câmera: quase velocidade constante, sem parar nas pontas (as transições cobrem as bordas)."""
    p = clamp01(p)
    return 0.75 * p + 0.25 * ease_sine(p)


def ease_out_quint(p):
    return 1 - (1 - clamp01(p)) ** 5


def ease_out_cubic(p):
    return 1 - (1 - clamp01(p)) ** 3


def beat_pulse(t, beats, amp):
    v = 0.0
    for b in beats:
        d = t - b
        if 0 <= d < 0.25:
            v = max(v, (1 - d / 0.25) ** 2)
    return 1 + amp * v


# ------------------------------------------------------------------ ponto de interesse (rostos / assunto)
_CASCADES = None


def _cascades():
    global _CASCADES
    if _CASCADES is None:
        try:
            import cv2
            d = cv2.data.haarcascades
            _CASCADES = (cv2, cv2.CascadeClassifier(d + "haarcascade_frontalface_alt2.xml"),
                         cv2.CascadeClassifier(d + "haarcascade_profileface.xml"))
        except Exception:  # noqa: BLE001  (sem OpenCV: usa só o mapa de interesse)
            _CASCADES = False
    return _CASCADES


def _faces(im):
    """Caixas de rosto normalizadas (x0, y0, x1, y1)."""
    c = _cascades()
    if not c:
        return []
    cv2, front, prof = c
    import numpy as np
    s = 900 / max(im.size)
    sm = im.resize((max(1, int(im.width * s)), max(1, int(im.height * s))))
    g = cv2.equalizeHist(cv2.cvtColor(np.asarray(sm), cv2.COLOR_RGB2GRAY))
    mn = max(14, int(min(g.shape) * 0.03))
    boxes = list(front.detectMultiScale(g, 1.08, 4, minSize=(mn, mn))) + list(prof.detectMultiScale(g, 1.08, 6, minSize=(mn, mn)))
    out = []
    for (x, y, w, h) in boxes:
        b = (x / sm.width, y / sm.height, (x + w) / sm.width, (y + h) / sm.height)
        if not any(abs(b[0] - o[0]) < 0.03 and abs(b[1] - o[1]) < 0.03 for o in out):
            out.append(b)
    # descarta "rostos" muito pequenos perto de rostos grandes (falsos positivos em estampas)
    if out:
        big = max(o[2] - o[0] for o in out)
        out = [o for o in out if (o[2] - o[0]) > big * 0.25]
    return out


def _saliency_box(im):
    """Sem rostos: região com mais detalhe e cor (assunto), como caixa normalizada."""
    import numpy as np
    from PIL import ImageFilter
    sm = im.resize((96, max(8, int(96 * im.height / im.width)))) if im.width >= im.height else im.resize((max(8, int(96 * im.width / im.height)), 96))
    a = np.asarray(sm.convert("HSV"), np.float32)
    edges = np.asarray(sm.convert("L").filter(ImageFilter.FIND_EDGES), np.float32)
    m = edges / (edges.max() + 1e-6) + 0.8 * a[..., 1] / 255.0
    yy, xx = np.mgrid[0:m.shape[0], 0:m.shape[1]]
    cy, cx = (m.shape[0] - 1) / 2, (m.shape[1] - 1) / 2
    m *= 1 - 0.35 * (((yy - cy) / m.shape[0]) ** 2 + ((xx - cx) / m.shape[1]) ** 2) * 4  # leve preferência pelo centro
    thr = np.quantile(m, 0.80)
    ys, xs = np.where(m >= thr)
    if len(xs) == 0:
        return (0.25, 0.25, 0.75, 0.75)
    w = m[ys, xs]
    mx, my = float((xs * w).sum() / w.sum()), float((ys * w).sum() / w.sum())
    sx, sy = float(np.sqrt(((xs - mx) ** 2 * w).sum() / w.sum())), float(np.sqrt(((ys - my) ** 2 * w).sum() / w.sum()))
    H, W = m.shape
    return (max(0, (mx - 1.3 * sx) / W), max(0, (my - 1.3 * sy) / H), min(1, (mx + 1.3 * sx) / W), min(1, (my + 1.3 * sy) / H))


def interest(info, im=None):
    """{'box': caixa de interesse, 'faces': n, 'fx','fy'}. Guardado em info['interesse']."""
    if info.get("interesse"):
        return info["interesse"]
    if im is None:
        from .photos import _open
        im = _open(info.get("arquivo_hd") or info["arquivo"])
    faces = _faces(im)
    if faces:
        x0 = min(f[0] for f in faces)
        x1 = max(f[2] for f in faces)
        y0 = min(f[1] for f in faces)
        y1 = max(f[3] for f in faces)
        fh = max(f[3] - f[1] for f in faces)
        box = (max(0, x0 - fh * 0.6), max(0, y0 - fh * 0.5), min(1, x1 + fh * 0.6), min(1, y1 + fh * 1.2))  # cabeça + ombros
        res = {"box": box, "faces": len(faces), "fx": (x0 + x1) / 2, "fy": (y0 + y1) / 2}
    else:
        box = _saliency_box(im)
        res = {"box": box, "faces": 0, "fx": (box[0] + box[2]) / 2, "fy": (box[1] + box[3]) / 2}
    info["interesse"] = res
    return res


# ------------------------------------------------------------------ imagens
def _prep(info, min_w, min_h, cache):
    """Foto com ajuste automático, redimensionada para cobrir min_w×min_h (com folga para o zoom)."""
    from PIL import Image
    from .photos import _open, enhance
    key = (info["i"], int(min_w), int(min_h))
    if key in cache:
        return cache[key]
    raw = cache.get(("raw", info["i"]))
    if raw is None:
        raw = _open(info.get("arquivo_hd") or info["arquivo"])
        cache[("raw", info["i"])] = raw
        interest(info, raw)
    s = max(min_w / raw.width, min_h / raw.height)
    im = enhance(raw, info.get("qualidade"), max(1.0, s))
    nw, nh = max(1, math.ceil(im.width * s)), max(1, math.ceil(im.height * s))
    if (nw, nh) != im.size:
        im = im.resize((nw, nh), Image.LANCZOS)
    cache[key] = im
    return im


def fit_zoom_limit(info, bw, bh, sw, sh):
    """Maior zoom (sobre o 'cover') que ainda mostra a caixa de interesse inteira."""
    b = interest(info)["box"]
    s0 = max(bw / sw, bh / sh)
    ww, wh = bw / s0, bh / s0                      # janela em z=1, em pixels da fonte
    need_w, need_h = (b[2] - b[0]) * sw * 1.08, (b[3] - b[1]) * sh * 1.08
    return max(1.0, min(ww / max(1, need_w), wh / max(1, need_h)))


def framing(info, bw, bh, sw, sh):
    """Centro (cx, cy) e âncora vertical para o recorte: rostos no terço superior, assunto no centro."""
    it = interest(info)
    b = it["box"]
    cx = (b[0] + b[2]) / 2
    if it["faces"]:
        return cx, it["fy"], 0.36          # rostos a 36% do topo da janela
    return cx, (b[1] + b[3]) / 2, 0.5


def camera(src, bw, bh, z, cx, cy, anchor=0.5):
    """Recorta src (zoom z sobre o 'cover', ponto cx,cy posicionado em 'anchor' da altura) para bw×bh, subpixel."""
    from PIL import Image
    sw, sh = src.size
    s0 = max(bw / sw, bh / sh)
    k = 1.0 / (s0 * z)
    ww, wh = bw * k, bh * k
    x0 = min(max(0.0, cx * sw - ww / 2), max(0.0, sw - ww))
    y0 = min(max(0.0, cy * sh - wh * anchor), max(0.0, sh - wh))
    return src.resize((max(1, int(bw)), max(1, int(bh))), Image.BICUBIC, box=(x0, y0, x0 + ww, y0 + wh))


def motion(mov, p, inten, zmax=1.2):
    """(zoom, dx, dy): zoom e deslocamento relativo (fração da folga) no instante p (0..1)."""
    e = ease_motion(p)
    a = min(0.12 * inten, zmax - 1.0)
    if mov == "zoom_in":
        return 1 + a * e, 0.0, 0.0
    if mov == "zoom_out":
        return 1 + a * (1 - e), 0.0, 0.0
    z = min(1.10 + 0.04 * inten, max(1.04, zmax))
    span = 0.9
    if mov == "pan_esquerda":
        return z, span / 2 - span * e, 0.0
    if mov == "pan_direita":
        return z, -span / 2 + span * e, 0.0
    if mov == "pan_cima":
        return z, 0.0, span / 2 - span * e
    if mov == "pan_baixo":
        return z, 0.0, -span / 2 + span * e
    return min(1.04 + 0.05 * inten * e, max(1.02, zmax)), -0.4 + 0.8 * e, 0.2 - 0.4 * e   # deriva


def shot(src, info, bw, bh, mov, p, inten, pulse=1.0):
    """Enquadramento com câmera: respeita rostos/assunto e o zoom máximo que não corta o interesse."""
    sw, sh = src.size
    zmax = fit_zoom_limit(info, bw, bh, sw, sh)
    z, dx, dy = motion(mov, p, inten, zmax)
    z = max(1.0, min(z * pulse, max(zmax, 1.0) * 1.03))
    cx, cy, anchor = framing(info, bw, bh, sw, sh)
    s0 = max(bw / sw, bh / sh)
    slack_x = max(0.0, (sw - bw / (s0 * z)) / sw) / 2   # folga em fração da largura
    slack_y = max(0.0, (sh - bh / (s0 * z)) / sh) / 2
    return camera(src, bw, bh, z, cx + dx * slack_x, cy + dy * slack_y, anchor)


def _backdrop(info, W, H, cache):
    """Fundo de estúdio: grafite com um leve tom da foto principal, luz suave no centro, vinheta e textura fina."""
    import numpy as np
    from PIL import Image, ImageStat
    key = ("backdrop", info["i"], W, H)
    if key in cache:
        return cache[key]
    src = _prep(info, 64, 64, cache)
    r, g, b = ImageStat.Stat(src.resize((32, 32))).mean
    m = max(r, g, b, 1)
    tint = np.array([r / m, g / m, b / m], np.float32)
    w4, h4 = max(8, W // 4), max(8, H // 4)
    yy, xx = np.mgrid[0:h4, 0:w4].astype(np.float32)
    d = np.sqrt(((xx - w4 / 2) / (w4 * 0.62)) ** 2 + ((yy - h4 * 0.46) / (h4 * 0.62)) ** 2)
    light = np.clip(1 - d, 0, 1) ** 1.6
    base = np.array(BG, np.float32)
    col = base[None, None, :] + light[..., None] * (14 + 26 * tint[None, None, :])
    im = Image.fromarray(np.clip(col, 0, 255).astype(np.uint8)).resize((W, H), Image.BICUBIC)
    noise = (np.random.default_rng(info["i"]).normal(0, 2.2, (H, W, 1))).astype(np.float32)
    im = Image.fromarray(np.clip(np.asarray(im, np.float32) + noise, 0, 255).astype(np.uint8))
    cache[key] = im
    return im


def _bg(info, W, H, cache, dark=0.38):
    """Fundo: a foto bem desfocada e escurecida, com vinheta."""
    from PIL import Image, ImageDraw, ImageFilter
    key = ("bg", info["i"], W, H)
    if key in cache:
        return cache[key]
    src = _prep(info, W / 8, H / 8, cache)
    small = camera(src, max(2, W // 8), max(2, H // 8), 1.0, 0.5, 0.5)
    im = small.filter(ImageFilter.GaussianBlur(4)).resize((W, H), Image.BICUBIC)
    im = Image.eval(im, lambda v: int(v * dark))
    vig = Image.new("L", (W // 4, H // 4), 0)
    ImageDraw.Draw(vig).ellipse((-W // 16, -H // 16, W // 4 + W // 16, H // 4 + H // 16), fill=255)
    vig = vig.filter(ImageFilter.GaussianBlur(min(W, H) // 24)).resize((W, H), Image.BICUBIC)
    im = Image.composite(im, Image.new("RGB", (W, H), BG), vig)
    cache[key] = im
    return im


# ------------------------------------------------------------------ painéis (dupla, trio, grade)
def _panels(layout, W, H):
    g = max(4, round(min(W, H) * 0.012))     # fenda fina, estilo editorial
    tall = H >= W * 1.15
    if layout == "dupla":
        if tall:
            ph = (H - g) / 2
            return [(0, 0, W, ph), (0, ph + g, W, H - ph - g)]
        pw = (W - g) / 2
        return [(0, 0, pw, H), (pw + g, 0, W - pw - g, H)]
    if layout == "trio":
        if tall:
            bh = (H - g) * 0.58
            sw = (W - g) / 2
            return [(0, 0, W, bh), (0, bh + g, sw, H - bh - g), (sw + g, bh + g, W - sw - g, H - bh - g)]
        bw = (W - g) * 0.6
        sh = (H - g) / 2
        return [(0, 0, bw, H), (bw + g, 0, W - bw - g, sh), (bw + g, sh + g, W - bw - g, H - sh - g)]
    cw, ch = (W - g) / 2, (H - g) / 2
    return [(0, 0, cw, ch), (cw + g, 0, W - cw - g, ch), (0, ch + g, cw, H - ch - g), (cw + g, ch + g, W - cw - g, H - ch - g)]


def _assign(infos, rects):
    """Foto de proporção mais parecida em cada painel; o painel maior recebe a primeira foto da cena (a principal)."""
    left = list(infos)
    order = sorted(range(len(rects)), key=lambda k: -rects[k][2] * rects[k][3])
    out = [None] * len(rects)
    first = True
    for k in order:
        x, y, w, h = rects[k]
        ar = w / h
        if first and len({round(r[2] * r[3]) for r in rects}) > 1:
            best = left[0]
        else:
            best = min(left, key=lambda f: abs(math.log((f["w"] / f["h"]) / ar)))
        first = False
        left.remove(best)
        out[k] = (rects[k], best)
    return out


def _entrance_times(n, beats, dur):
    bs = [b for b in beats if 0.05 < b < dur * 0.6]
    times = [0.0]
    for b in bs:
        if len(times) >= n:
            break
        if b - times[-1] >= 0.28:
            times.append(b)
    while len(times) < n:
        times.append(times[-1] + min(0.4, dur * 0.1))
    return times[:n]


class Scene:
    def __init__(self, spec, infos, W, H, dur, beats, inten, pulse, seed, letterbox=False):
        self.spec, self.infos, self.W, self.H, self.dur = spec, infos, W, H, dur
        self.beats, self.inten, self.pulse, self.letterbox = beats, inten, pulse, letterbox
        self.rnd = random.Random(seed)
        self.cache = {}
        self.layout = spec["layout"]
        getattr(self, f"_setup_{self.layout}")()

    def _bars(self, frame):
        if self.letterbox and self.W > self.H:
            from PIL import ImageDraw
            bh = int(self.H * 0.09)
            d = ImageDraw.Draw(frame)
            d.rectangle((0, 0, self.W, bh), fill=(0, 0, 0))
            d.rectangle((0, self.H - bh, self.W, self.H), fill=(0, 0, 0))
        return frame

    # ---------------- única
    def _setup_unica(self):
        info = self.infos[0]
        ar_img, ar_fr = info["w"] / info["h"], self.W / self.H
        self.moldura = abs(math.log(ar_img / ar_fr)) >= 0.35
        if not self.moldura:
            self.src = _prep(info, self.W * 1.25, self.H * 1.25, self.cache)
        else:  # foto vertical em vídeo horizontal (ou o contrário): foto inteira sobre o fundo dela mesma
            self.bgim = _bg(info, self.W, self.H, self.cache, dark=0.30)
            fit = min(self.W * 0.9 / info["w"], self.H * 0.9 / info["h"])
            self.fw, self.fh = int(info["w"] * fit), int(info["h"] * fit)
            self.src = _prep(info, self.fw * 1.12, self.fh * 1.12, self.cache)
            from PIL import ImageFilter, Image
            pad = int(min(self.W, self.H) * 0.04)
            m = Image.new("L", (self.fw + 2 * pad, self.fh + 2 * pad), 0)
            m.paste(170, (pad, pad + pad // 3, pad + self.fw, pad + self.fh + pad // 3))
            self.sh = m.filter(ImageFilter.GaussianBlur(pad // 2))
            self.pad = pad

    def _draw_unica(self, t):
        p = t / self.dur
        info = self.infos[0]
        pul = beat_pulse(t, self.beats, 0.03) if self.pulse else 1.0
        if not self.moldura:
            return self._bars(shot(self.src, info, self.W, self.H, self.spec.get("movimento", "zoom_in"), p, self.inten, pul))
        frame = self.bgim.copy()
        sc = (1.0 + 0.04 * self.inten * ease_motion(p)) * pul
        fw, fh = int(self.fw * sc), int(self.fh * sc)
        img = camera(self.src, fw, fh, 1.0, 0.5, 0.5)
        x, y = (self.W - fw) / 2, (self.H - fh) / 2
        sh = self.sh if sc == 1.0 else self.sh.resize((self.sh.width * fw // self.fw, self.sh.height * fh // self.fh))
        sx, sy = int(x - self.pad * fw / self.fw), int(y - self.pad * fh / self.fh)
        frame.paste((0, 0, 0), (sx, sy, sx + sh.width, sy + sh.height), sh)
        frame.paste(img, (int(round(x)), int(round(y))))
        return self._bars(frame)

    # ---------------- painéis editoriais
    def _setup_panels(self):
        from PIL import Image
        rects = _panels(self.layout, self.W, self.H)
        self.items = []
        for (x, y, w, h), info in _assign(self.infos, rects):
            src = _prep(info, w * 1.18, h * 1.18, self.cache)
            self.items.append({"rect": (x, y, w, h), "src": src, "info": info,
                               "mov": self.rnd.choice(["zoom_in", "zoom_out", "deriva", "zoom_in"])})
        # ordem de entrada: o maior primeiro, depois leitura (esquerda→direita, cima→baixo)
        self.items.sort(key=lambda it: (-(it["rect"][2] * it["rect"][3]) if self.layout == "trio" else 0, round(it["rect"][1]), it["rect"][0]))
        times = _entrance_times(len(self.items), self.beats, self.dur)
        for k, it in enumerate(self.items):
            it["t0"] = times[k]
            x, y, w, h = it["rect"]
            # revela a partir do lado de fora do quadro (a foto "entra" pela borda mais próxima)
            if self.layout == "dupla":
                it["dir"] = ("U" if k == 0 else "D") if self.H >= self.W * 1.15 else ("L" if k == 0 else "R")
            elif w >= h * 1.3:
                it["dir"] = "L" if x < self.W / 2 - 1 else "R"
            else:
                it["dir"] = "U" if y < self.H / 2 - 1 else "D"
        self.base = _backdrop(self.infos[0], self.W, self.H, self.cache)

    _setup_dupla = _setup_trio = _setup_grade = _setup_panels

    def _draw_panels(self, t):
        frame = self.base.copy()
        REV = 0.65
        for it in self.items:
            dt = t - it["t0"]
            if dt <= 0:
                continue
            x, y, w, h = it["rect"]
            q = ease_out_quint(dt / REV)
            p = clamp01(dt / max(0.6, self.dur - it["t0"]))
            pul = beat_pulse(t, self.beats, 0.015) if self.pulse else 1.0
            content = shot(it["src"], it["info"], w, h, it["mov"], p, 0.6 * self.inten, pul * (1.0 + 0.06 * (1 - q)))
            if q >= 0.999:
                frame.paste(content, (int(round(x)), int(round(y))))
                continue
            # revelação: a janela abre a partir de uma borda; a foto desliza um pouco no sentido contrário (paralaxe)
            d = it["dir"]
            iw, ih = int(round(w)), int(round(h))
            if d in ("L", "R"):
                vis = max(1, int(round(iw * q)))
                par = int(round((1 - q) * iw * 0.18))
                if d == "L":
                    crop = content.crop((iw - vis - par, 0, iw - par, ih)) if iw - vis - par >= 0 else content.crop((0, 0, vis, ih))
                    frame.paste(crop, (int(round(x)), int(round(y))))
                else:
                    crop = content.crop((par, 0, par + vis, ih)) if par + vis <= iw else content.crop((iw - vis, 0, iw, ih))
                    frame.paste(crop, (int(round(x)) + iw - vis, int(round(y))))
            else:
                vis = max(1, int(round(ih * q)))
                par = int(round((1 - q) * ih * 0.18))
                if d == "U":
                    crop = content.crop((0, ih - vis - par, iw, ih - par)) if ih - vis - par >= 0 else content.crop((0, 0, iw, vis))
                    frame.paste(crop, (int(round(x)), int(round(y))))
                else:
                    crop = content.crop((0, par, iw, par + vis)) if par + vis <= ih else content.crop((0, ih - vis, iw, ih))
                    frame.paste(crop, (int(round(x)), int(round(y)) + ih - vis))
        return frame

    _draw_dupla = _draw_trio = _draw_grade = _draw_panels

    # ---------------- colagem (fotos impressas sobre a mesa)
    def _layout_colagem(self, n):
        """Composições fixas: (cx, cy, altura relativa, rotação). A primeira é a principal (fica por cima)."""
        wide = self.W >= self.H
        if wide:
            T = {3: [(0.50, 0.50, 0.70, -1.5), (0.22, 0.47, 0.52, -5), (0.78, 0.53, 0.52, 4)],
                 4: [(0.50, 0.50, 0.66, 1.5), (0.20, 0.36, 0.44, -6), (0.80, 0.36, 0.44, 5), (0.78, 0.74, 0.38, -3)],
                 5: [(0.50, 0.50, 0.62, -1), (0.19, 0.33, 0.42, -6), (0.81, 0.33, 0.42, 5), (0.22, 0.72, 0.40, 4), (0.78, 0.72, 0.40, -4)]}
        else:
            T = {3: [(0.50, 0.50, 0.46, -1.5), (0.42, 0.22, 0.32, -5), (0.58, 0.79, 0.32, 4)],
                 4: [(0.50, 0.50, 0.42, 1.5), (0.33, 0.20, 0.30, -5), (0.67, 0.22, 0.30, 5), (0.50, 0.81, 0.30, 3)],
                 5: [(0.50, 0.50, 0.40, -1), (0.30, 0.18, 0.28, -6), (0.70, 0.20, 0.28, 5), (0.30, 0.82, 0.28, 4), (0.70, 0.80, 0.28, -4)]}
        return T.get(n, T[5])[:n]

    def _setup_colagem(self):
        from PIL import Image, ImageFilter, ImageOps
        W, H = self.W, self.H
        n = len(self.infos)
        comp = self._layout_colagem(n)
        self.prints = []
        for k, (info, (cx, cy, hrel, ang)) in enumerate(zip(self.infos, comp)):
            ar = max(0.62, min(1.6, info["w"] / info["h"]))
            ph = hrel * H
            pw = ph * ar
            if pw > W * 0.46:
                pw = W * 0.46
                ph = pw / ar
            src = _prep(info, pw * 1.05, ph * 1.05, self.cache)
            sw, sh = src.size
            cxf, cyf, anchor = framing(info, pw, ph, sw, sh)
            img = camera(src, int(pw), int(ph), 1.0, cxf, cyf, anchor)
            border = max(4, int(min(pw, ph) * 0.028))
            card = ImageOps.expand(img, border=border, fill=(248, 246, 241))
            pad = int(max(card.size) * 0.08)
            m = Image.new("L", (card.width + 2 * pad, card.height + 2 * pad), 0)
            m.paste(150, (pad, pad + pad // 3, pad + card.width, pad + card.height + pad // 3))
            shadow = m.filter(ImageFilter.GaussianBlur(pad // 2))
            self.prints.append({"card": card.convert("RGBA"), "shadow": shadow, "pad": pad, "cx": cx * W, "cy": cy * H, "ang": ang,
                                "rest": card.convert("RGBA").rotate(ang, resample=Image.BICUBIC, expand=True),
                                "rest_sh": shadow.rotate(ang, resample=Image.BICUBIC, expand=True)})
        # a principal entra por último (fica por cima); as de trás entram antes
        order = list(range(1, n)) + [0]
        times = _entrance_times(n, self.beats, self.dur)
        for slot, k in enumerate(order):
            self.prints[k]["t0"] = times[slot]
            self.prints[k]["from"] = (-1 if self.prints[k]["cx"] < W / 2 else 1, 1)
        self.draw_order = order
        self.bg = _backdrop(self.infos[0], W, H, self.cache)

    def _draw_colagem(self, t):
        from PIL import Image
        p = t / self.dur
        frame = self.bg.copy()
        ENT = 0.75
        for k in self.draw_order:
            pr = self.prints[k]
            dt = t - pr["t0"]
            if dt <= 0:
                continue
            q = ease_out_quint(dt / ENT)
            if q >= 0.999:
                img, sh = pr["rest"], pr["rest_sh"]
                lift = 0.0
            else:
                ang = pr["ang"] + (1 - q) * 9 * pr["from"][0]
                img = pr["card"].rotate(ang, resample=Image.BILINEAR, expand=True)
                sh = pr["shadow"].rotate(ang, resample=Image.BILINEAR, expand=True)
                lift = 1 - q
            ox = pr["from"][0] * lift * self.W * 0.10
            oy = lift * self.H * 0.22
            a = clamp01(dt / 0.25)
            x, y = pr["cx"] - img.width / 2 + ox, pr["cy"] - img.height / 2 + oy
            soff = 6 + lift * 30
            sx, sy = int(x - (sh.width - img.width) / 2 + soff * 0.4), int(y - (sh.height - img.height) / 2 + soff)
            sm = Image.eval(sh, lambda v, a=a * (1 - 0.4 * lift): int(v * a))
            frame.paste((0, 0, 0), (sx, sy, sx + sm.width, sy + sm.height), sm)
            if a < 1:
                al = img.getchannel("A").point(lambda v, a=a: int(v * a))
                frame.paste(img, (int(round(x)), int(round(y))), al)
            else:
                frame.paste(img, (int(round(x)), int(round(y))), img)
        z = 1.0 + 0.045 * self.inten * ease_motion(p)
        return camera(frame, self.W, self.H, z, 0.5, 0.5) if z > 1.001 else frame

    # ---------------- rajada (cortes rápidos na batida)
    def _setup_rajada(self):
        from PIL import Image
        n = len(self.infos)
        bs = [b for b in self.beats if 0.15 < b < self.dur - 0.15]
        cuts = [0.0]
        if bs and len(bs) >= n - 1:
            step = max(1, len(bs) // max(1, n - 1))
            cuts += [bs[min(len(bs) - 1, (k + 1) * step - 1)] for k in range(n - 1)]
        else:
            cuts += [self.dur * (k + 1) / n for k in range(n - 1)]
        self.cuts = sorted(set(round(c, 3) for c in cuts))[:n]
        self.srcs = [(_prep(info, self.W * 1.2, self.H * 1.2, self.cache), info) for info in self.infos[:len(self.cuts)]]
        self.white = Image.new("RGB", (self.W, self.H), (255, 255, 255))

    def _draw_rajada(self, t):
        from PIL import Image
        k = min(max(i for i, c in enumerate(self.cuts) if c <= t + 1e-6), len(self.srcs) - 1)
        dt = t - self.cuts[k]
        nxt = self.cuts[k + 1] if k + 1 < len(self.cuts) else self.dur
        span = max(0.2, nxt - self.cuts[k])
        src, info = self.srcs[k]
        punch = 1.0 + 0.10 * (1 - ease_out_cubic(dt / 0.3))
        frame = shot(src, info, self.W, self.H, "zoom_in" if k % 2 else "zoom_out", dt / span, 0.8 * self.inten, punch)
        if dt < 0.09 and k > 0:
            frame = Image.blend(frame, self.white, 0.45 * (1 - dt / 0.09))
        return self._bars(frame)

    def draw(self, t):
        return getattr(self, f"_draw_{self.layout}")(t)


# ------------------------------------------------------------------ render de uma cena → arquivo
def scene_key(spec, W, H, dur, beats, inten, pulse, kind, letterbox=False):
    blob = json.dumps({k: spec.get(k) for k in ("layout", "fotos", "movimento", "foco_x", "foco_y")} |
                      {"W": W, "H": H, "d": round(dur, 3), "b": [round(b, 2) for b in beats], "i": inten, "p": pulse, "k": kind,
                       "lb": letterbox, "v": ENGINE_VERSION}, sort_keys=True)
    return hashlib.sha1(blob.encode()).hexdigest()[:10]


def render_scene(job):
    """job: dict com spec, infos, W, H, dur, fps, beats, inten, pulse, out, kind, ffmpeg, seed, letterbox."""
    import tempfile
    out = Path(job["out"])
    if out.exists() and out.stat().st_size > 0:
        return str(out)
    sc = Scene(job["spec"], job["infos"], job["W"], job["H"], job["dur"], job["beats"], job["inten"], job["pulse"], job["seed"],
               job.get("letterbox", False))
    frames = int(math.ceil(job["dur"] * job["fps"])) + 1
    tmp = out.with_suffix(".part.mp4")
    q = ["-preset", "veryfast", "-crf", "20"] if job["kind"] == "preview" else ["-preset", "veryfast", "-crf", "12"]
    cmd = [job["ffmpeg"], "-hide_banner", "-nostdin", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
           "-s", f"{job['W']}x{job['H']}", "-r", str(job["fps"]), "-i", "-", "-an", "-c:v", "libx264", *q, "-pix_fmt", "yuv420p", str(tmp)]
    with tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=err)
        try:
            for f in range(frames):
                im = sc.draw(f / job["fps"])
                if im.mode != "RGB":
                    im = im.convert("RGB")
                proc.stdin.write(im.tobytes())
            proc.stdin.close()
        except BrokenPipeError:
            pass
        except Exception:
            proc.kill()
            raise
        rc = proc.wait()
        if rc != 0:
            err.seek(0)
            raise RuntimeError(f"Cena {job['spec'].get('layout')} ({len(job['infos'])} foto(s)): FFmpeg falhou ao gravar a cena.\n"
                               + err.read().decode("utf-8", "replace")[-3000:] + "\nComando: " + " ".join(cmd))
    tmp.replace(out)
    return str(out)
