"""Motor de cenas do vídeo de fotos.

Cada cena é desenhada quadro a quadro em Python (Pillow) com posições fracionárias (subpixel) e
enviada ao FFmpeg como vídeo bruto. Assim o zoom e o pan ficam contínuos — o filtro zoompan do
FFmpeg só anda em pixels inteiros e "trava" em movimentos lentos.

Layouts:
  unica    1 foto em tela cheia, câmera suave (zoom/pan/deriva) e pulso na batida (opcional)
  dupla    2 fotos lado a lado (ou empilhadas no vertical), entrando em lados opostos
  trio     1 foto grande + 2 menores, entrando uma por batida
  grade    4 fotos em grade, surgindo uma por batida
  colagem  3 a 5 fotos "impressas" com borda, giradas, caindo uma por batida sobre fundo desfocado
  rajada   3 a 6 fotos em cortes rápidos na batida, com punch-in e flash (para picos da música)
"""
import hashlib
import json
import math
import random
import subprocess
from pathlib import Path

LAYOUTS = {"unica": (1, 1), "dupla": (2, 2), "trio": (3, 3), "grade": (4, 4), "colagem": (3, 5), "rajada": (3, 6)}
MIN_BEATS = {"unica": 2, "dupla": 4, "trio": 5, "grade": 6, "colagem": 6, "rajada": 3}


def layout_for_count(n):
    return {1: "unica", 2: "dupla", 3: "trio", 4: "grade"}.get(n, "colagem" if n <= 5 else "rajada")


# ------------------------------------------------------------------ curvas
def ease_sine(p):
    return 0.5 - 0.5 * math.cos(math.pi * min(1.0, max(0.0, p)))


def ease_motion(p):
    """Movimento de câmera: quase velocidade constante, sem parar nas pontas (as transições cobrem as bordas)."""
    p = min(1.0, max(0.0, p))
    return 0.75 * p + 0.25 * ease_sine(p)


def ease_out_cubic(p):
    p = min(1.0, max(0.0, p))
    return 1 - (1 - p) ** 3


def ease_out_back(p, s=1.5):
    p = min(1.0, max(0.0, p)) - 1
    return 1 + (s + 1) * p ** 3 + s * p ** 2


def beat_pulse(t, beats, amp):
    """Punch na batida: sobe rápido e volta em ~0,25 s."""
    v = 0.0
    for b in beats:
        d = t - b
        if 0 <= d < 0.25:
            v = max(v, (1 - d / 0.25) ** 2)
    return 1 + amp * v


# ------------------------------------------------------------------ imagens
def _prep(info, min_w, min_h, cache):
    """Foto com ajuste automático, redimensionada para cobrir min_w×min_h (com folga para o zoom)."""
    from PIL import Image
    from .photos import _open, enhance
    key = (info["i"], int(min_w), int(min_h))
    if key in cache:
        return cache[key]
    im = _open(info.get("arquivo_hd") or info["arquivo"])
    s = max(min_w / im.width, min_h / im.height)
    im = enhance(im, info.get("qualidade"), max(1.0, s))
    nw, nh = max(1, math.ceil(im.width * s)), max(1, math.ceil(im.height * s))
    if (nw, nh) != im.size:
        im = im.resize((nw, nh), Image.LANCZOS)
    cache[key] = im
    return im


def camera(src, bw, bh, z, cx, cy):
    """Recorta src (com zoom z sobre o 'cover' e centro cx,cy normalizados) para bw×bh, com precisão subpixel."""
    from PIL import Image
    sw, sh = src.size
    s0 = max(bw / sw, bh / sh)
    k = 1.0 / (s0 * z)                      # pixels da fonte por pixel de saída
    ww, wh = bw * k, bh * k
    x0 = min(max(0.0, cx * sw - ww / 2), max(0.0, sw - ww))
    y0 = min(max(0.0, cy * sh - wh / 2), max(0.0, sh - wh))
    # resize com "box" fracionário: precisão subpixel e filtro separável (rápido e sem serrilhado)
    return src.resize((int(bw), int(bh)), Image.BICUBIC, box=(x0, y0, x0 + ww, y0 + wh))


def motion(mov, p, inten, fx=0.5, fy=0.45):
    """(zoom, cx, cy) da câmera no instante p (0..1) para o movimento escolhido."""
    e = ease_motion(p)
    a = 0.13 * inten
    if mov == "zoom_in":
        return 1 + a * e, fx, fy
    if mov == "zoom_out":
        return 1 + a * (1 - e), fx, fy
    z = 1.12 + 0.04 * inten
    span = (1 - 1 / z) * 0.92          # anda só dentro da folga da foto (nunca encosta na borda e para)
    if mov == "pan_esquerda":
        return z, 0.5 + span / 2 - span * e, 0.5
    if mov == "pan_direita":
        return z, 0.5 - span / 2 + span * e, 0.5
    if mov == "pan_cima":
        return z, 0.5, 0.5 + span / 2 - span * e
    if mov == "pan_baixo":
        return z, 0.5, 0.5 - span / 2 + span * e
    # deriva: leve diagonal com zoom lento, também dentro da folga
    zd = 1.06 + 0.05 * inten * e
    sp = (1 - 1 / 1.06) * 0.8
    return zd, 0.5 - sp / 2 + sp * e, 0.5 + sp / 4 - sp / 2 * e


def _rounded_mask(w, h, r):
    from PIL import Image, ImageDraw
    m = Image.new("L", (int(w), int(h)), 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, int(w) - 1, int(h) - 1), radius=int(r), fill=255)
    return m


def _shadow(w, h, r, blur, alpha=150):
    from PIL import Image, ImageDraw, ImageFilter
    pad = int(blur * 3)
    m = Image.new("L", (int(w) + 2 * pad, int(h) + 2 * pad), 0)
    ImageDraw.Draw(m).rounded_rectangle((pad, pad + blur // 2, pad + int(w), pad + int(h) + blur // 2), radius=int(r), fill=alpha)
    return m.filter(ImageFilter.GaussianBlur(blur)), pad


def _bg(info, W, H, cache, dark=0.42):
    """Fundo: a própria foto desfocada e escurecida (sem bordas pretas)."""
    from PIL import Image, ImageFilter
    key = ("bg", info["i"], W, H)
    if key in cache:
        return cache[key]
    im = _prep(info, W / 6, H / 6, cache).copy()
    s = max((W / 6) / im.width, (H / 6) / im.height)
    im = im.resize((max(1, math.ceil(im.width * s)), max(1, math.ceil(im.height * s))), Image.BILINEAR)
    x, y = (im.width - int(W / 6)) // 2, (im.height - int(H / 6)) // 2
    im = im.crop((x, y, x + int(W / 6), y + int(H / 6))).filter(ImageFilter.GaussianBlur(3)).resize((int(W * 1.1), int(H * 1.1)), Image.BICUBIC)
    im = Image.eval(im, lambda v: int(v * dark))
    cache[key] = im
    return im


def _bg_frame(bg, W, H, p):
    # fundo desfocado: sem movimento (não aparece e economiza tempo)
    key = (W, H)
    c = _BG_CACHE.get(id(bg))
    if not c or c[0] != key:
        c = (key, camera(bg, W, H, 1.0, 0.5, 0.5))
        _BG_CACHE[id(bg)] = c
    return c[1].copy()


_BG_CACHE = {}


# ------------------------------------------------------------------ painéis (dupla, trio, grade)
def _panels(layout, W, H):
    """Retângulos (x, y, w, h) dos painéis no quadro."""
    g = round(min(W, H) * 0.035)
    wide = W >= H * 1.15
    tall = H >= W * 1.15
    if layout == "dupla":
        if tall:
            ph = (H - 3 * g) / 2
            return [(g, g, W - 2 * g, ph), (g, 2 * g + ph, W - 2 * g, ph)]
        pw = (W - 3 * g) / 2
        return [(g, g * 1.6, pw, H - 3.2 * g), (2 * g + pw, g * 1.6, pw, H - 3.2 * g)]
    if layout == "trio":
        if tall:
            bh = (H - 3 * g) * 0.6
            sw = (W - 3 * g) / 2
            sh = H - 3 * g - bh
            return [(g, g, W - 2 * g, bh), (g, 2 * g + bh, sw, sh), (2 * g + sw, 2 * g + bh, sw, sh)]
        bw = (W - 3 * g) * (0.58 if wide else 0.55)
        sw = W - 3 * g - bw
        sh = (H - 3 * g) / 2
        return [(g, g, bw, H - 2 * g), (2 * g + bw, g, sw, sh), (2 * g + bw, 2 * g + sh, sw, sh)]
    # grade 2×2
    cw, ch = (W - 3 * g) / 2, (H - 3 * g) / 2
    return [(g, g, cw, ch), (2 * g + cw, g, cw, ch), (g, 2 * g + ch, cw, ch), (2 * g + cw, 2 * g + ch, cw, ch)]


def _assign(infos, rects):
    """Põe cada foto no painel de proporção mais parecida (foto vertical no painel alto)."""
    left = list(infos)
    out = []
    for (_x, _y, w, h) in sorted(rects, key=lambda r: -r[2] * r[3]):
        ar = w / h
        best = min(left, key=lambda f: abs(math.log((f["w"] / f["h"]) / ar)))
        left.remove(best)
        out.append(((_x, _y, w, h), best))
    return out


def _entrance_times(n, beats, dur):
    """Instantes de entrada: nas batidas da cena (ou espaçados) — a primeira entra logo."""
    bs = [b for b in beats if 0.05 < b < dur * 0.6]
    times = [0.0]
    for b in bs:
        if len(times) >= n:
            break
        if b - times[-1] >= 0.22:
            times.append(b)
    while len(times) < n:
        times.append(times[-1] + min(0.45, dur * 0.12))
    return times[:n]


class Scene:
    def __init__(self, spec, infos, W, H, dur, beats, inten, pulse, seed):
        self.spec, self.infos, self.W, self.H, self.dur = spec, infos, W, H, dur
        self.beats, self.inten, self.pulse = beats, inten, pulse
        self.rnd = random.Random(seed)
        self.cache = {}
        self.layout = spec["layout"]
        getattr(self, f"_setup_{self.layout}")()

    # ---------------- única
    def _setup_unica(self):
        from .photos import make_plate  # mantém "recorte" x "moldura" (foto vertical em vídeo horizontal)
        info = self.infos[0]
        ar_img, ar_fr = info["w"] / info["h"], self.W / self.H
        if abs(math.log(ar_img / ar_fr)) < 0.30:
            self.src = _prep(info, self.W * 1.3, self.H * 1.3, self.cache)
            self.moldura = False
        else:
            plate = self.spec["_platedir"] / f"m{info['i']:03d}_{self.W}x{self.H}.jpg"
            if not plate.exists():
                make_plate(info, plate, self.W, self.H, 1.3, (self.spec.get("foco_x", 0.5), self.spec.get("foco_y", 0.45)))
            from PIL import Image
            self.src = Image.open(plate).convert("RGB")
            self.moldura = True

    def _draw_unica(self, t):
        p = t / self.dur
        fx, fy = (0.5, 0.5) if self.moldura else (self.spec.get("foco_x", 0.5), self.spec.get("foco_y", 0.45))
        z, cx, cy = motion(self.spec.get("movimento", "zoom_in"), p, self.inten * (0.6 if self.moldura else 1.0), fx, fy)
        if self.pulse:
            z *= beat_pulse(t, self.beats, 0.035)
        return camera(self.src, self.W, self.H, z, cx, cy)

    # ---------------- painéis
    def _setup_panels(self):
        from PIL import Image
        rects = _panels(self.layout, self.W, self.H)
        self.items = []
        r = min(self.W, self.H) * 0.018
        for (x, y, w, h), info in _assign(self.infos, rects):
            src = _prep(info, w * 1.2, h * 1.2, self.cache)
            sh, pad = _shadow(w, h, r, max(6, int(min(self.W, self.H) * 0.012)))
            self.items.append({"rect": (x, y, w, h), "src": src, "mask": _rounded_mask(w, h, r), "shadow": sh, "pad": pad,
                               "mov": self.rnd.choice(["zoom_in", "zoom_out", "deriva"]), "info": info})
        # ordem de entrada: o maior primeiro, depois leitura (esquerda→direita, cima→baixo)
        self.items.sort(key=lambda it: (-(it["rect"][2] * it["rect"][3]) if self.layout == "trio" else 0, it["rect"][1] // 10, it["rect"][0]))
        times = _entrance_times(len(self.items), self.beats, self.dur)
        for k, it in enumerate(self.items):
            it["t0"] = times[k]
            it["dir"] = (-1 if (it["rect"][0] + it["rect"][2] / 2) < self.W / 2 else 1)
        self.bg = _bg(self.infos[0], self.W, self.H, self.cache)
        self.black = Image.new("RGB", (self.W, self.H), (0, 0, 0))

    _setup_dupla = _setup_trio = _setup_grade = _setup_panels

    def _draw_panels(self, t):
        from PIL import Image
        p = t / self.dur
        frame = _bg_frame(self.bg, self.W, self.H, p)
        for it in self.items:
            dt = t - it["t0"]
            if dt < 0:
                continue
            x, y, w, h = it["rect"]
            a = min(1.0, dt / 0.35)
            if self.layout == "grade":
                sc = 0.82 + 0.18 * ease_out_back(min(1.0, dt / 0.42))
                ox, oy = 0.0, 0.0
            else:
                sc = 1.0
                ox = it["dir"] * (1 - ease_out_cubic(min(1.0, dt / 0.55))) * (w * 0.6 + self.W * 0.08)
                oy = 0.0
            z, cx, cy = motion(it["mov"], min(1.0, max(0.0, dt / max(0.5, self.dur - it["t0"]))), 0.55 * self.inten, 0.5, 0.42)
            if self.pulse:
                z *= beat_pulse(t, self.beats, 0.02)
            cw, ch = max(2, int(round(w * sc))), max(2, int(round(h * sc)))
            content = camera(it["src"], cw, ch, z, cx, cy)
            mask = it["mask"] if (cw, ch) == it["mask"].size else it["mask"].resize((cw, ch), Image.BILINEAR)
            px = int(round(x + ox + (w - cw) / 2))
            py = int(round(y + oy + (h - ch) / 2))
            if a < 1:
                mask = Image.eval(mask, lambda v, a=a: int(v * a))
            sh = it["shadow"] if sc == 1.0 else it["shadow"].resize((it["shadow"].width * cw // int(w), it["shadow"].height * ch // int(h)))
            sm = Image.eval(sh, lambda v, a=a: int(v * a)) if a < 1 else sh
            sx, sy = px - it["pad"], py - it["pad"]
            frame.paste((0, 0, 0), (sx, sy, sx + sm.width, sy + sm.height), sm)
            frame.paste(content, (px, py), mask)
        return frame

    _draw_dupla = _draw_trio = _draw_grade = _draw_panels

    # ---------------- colagem (fotos impressas caindo)
    def _setup_colagem(self):
        from PIL import Image, ImageOps
        W, H = self.W, self.H
        n = len(self.infos)
        base = min(W, H) * {3: 0.58, 4: 0.48}.get(n, 0.42)
        border = max(6, int(base * 0.035))
        slots = self._collage_slots(n)
        self.prints = []
        for k, info in enumerate(self.infos):
            ar = info["w"] / info["h"]
            pw, ph = (base, base / ar) if ar >= 1 else (base * ar, base)
            if ar >= 1 and W >= H:
                pw, ph = pw * 1.1, ph * 1.1
            img = _prep(info, pw, ph, self.cache)
            img = camera(img, int(pw), int(ph), 1.0, 0.5, 0.45)
            card = ImageOps.expand(img, border=border, fill=(246, 244, 238))
            ang = self.rnd.uniform(-8, 8)
            rgba = card.convert("RGBA").rotate(ang, resample=Image.BICUBIC, expand=True)
            sh, pad = _shadow(card.width, card.height, 2, max(8, int(base * 0.03)), 170)
            sh = sh.rotate(ang, resample=Image.BICUBIC, expand=True)
            cx, cy = slots[k]
            self.prints.append({"img": rgba, "shadow": sh, "cx": cx * W, "cy": cy * H, "drop": self.rnd.uniform(-1, 1)})
        times = _entrance_times(n, self.beats, self.dur)
        for k, pr in enumerate(self.prints):
            pr["t0"] = times[k]
        self.bg = _bg(self.infos[0], W, H, self.cache, dark=0.5)
        self.black = Image.new("RGB", (W, H), (0, 0, 0))

    def _collage_slots(self, n):
        wide = self.W >= self.H
        presets = {
            3: [(0.30, 0.45), (0.70, 0.40), (0.50, 0.60)] if wide else [(0.42, 0.28), (0.58, 0.52), (0.45, 0.74)],
            4: [(0.27, 0.36), (0.73, 0.34), (0.32, 0.66), (0.70, 0.66)] if wide else [(0.35, 0.25), (0.65, 0.40), (0.38, 0.60), (0.62, 0.78)],
            5: [(0.22, 0.36), (0.50, 0.30), (0.78, 0.38), (0.36, 0.68), (0.66, 0.66)] if wide else [(0.34, 0.20), (0.66, 0.34), (0.36, 0.50), (0.64, 0.66), (0.42, 0.82)],
        }
        sl = presets.get(n, presets[5])[:n]
        return [(x + self.rnd.uniform(-0.03, 0.03), y + self.rnd.uniform(-0.03, 0.03)) for x, y in sl]

    def _draw_colagem(self, t):
        from PIL import Image
        p = t / self.dur
        frame = _bg_frame(self.bg, self.W, self.H, p)
        for pr in self.prints:
            dt = t - pr["t0"]
            if dt < 0:
                continue
            q = min(1.0, dt / 0.42)
            sc = 1.0 + 0.35 * (1 - ease_out_cubic(q))
            a = min(1.0, dt / 0.18)
            img, sh = pr["img"], pr["shadow"]
            if sc != 1.0:
                img = img.resize((max(2, int(img.width * sc)), max(2, int(img.height * sc))), Image.BILINEAR)
                sh = sh.resize((max(2, int(sh.width * sc)), max(2, int(sh.height * sc))), Image.BILINEAR)
            off = (sc - 1) * 60  # sombra se afasta enquanto a foto está "no ar"
            x = pr["cx"] - img.width / 2
            y = pr["cy"] - img.height / 2 + pr["drop"] * (1 - ease_out_cubic(q)) * self.H * 0.04
            sa = Image.eval(sh, lambda v, a=a: int(v * a)) if a < 1 else sh
            sx, sy = int(x - (sh.width - img.width) / 2 + off), int(y - (sh.height - img.height) / 2 + off)
            frame.paste((0, 0, 0), (sx, sy, sx + sa.width, sy + sa.height), sa)
            if a < 1:
                r, g, b, al = img.split()
                img = Image.merge("RGBA", (r, g, b, Image.eval(al, lambda v, a=a: int(v * a))))
            frame.paste(img, (int(x), int(y)), img)
        # leve aproximação de câmera na colagem inteira
        z = 1.0 + 0.05 * self.inten * ease_motion(p)
        return camera(frame, self.W, self.H, z, 0.5, 0.5) if z > 1.001 else frame

    # ---------------- rajada (cortes rápidos na batida)
    def _setup_rajada(self):
        from PIL import Image
        n = len(self.infos)
        bs = [b for b in self.beats if 0.15 < b < self.dur - 0.15]
        cuts = [0.0]
        step = max(1, round(len(bs) / max(1, n - 1))) if bs else 0
        if bs and len(bs) >= n - 1:
            cuts += [bs[min(len(bs) - 1, (k + 1) * step - 1)] for k in range(n - 1)]
        else:
            cuts += [self.dur * (k + 1) / n for k in range(n - 1)]
        self.cuts = sorted(set(round(c, 3) for c in cuts))[:n]
        self.srcs = []
        for info in self.infos[:len(self.cuts)]:
            ar_img = info["w"] / info["h"]
            if abs(math.log(ar_img / (self.W / self.H))) < 0.45:
                self.srcs.append((_prep(info, self.W * 1.25, self.H * 1.25, self.cache), info))
            else:  # foto com outra proporção: cobre o quadro (rajada é corte de impacto)
                self.srcs.append((_prep(info, self.W * 1.25, self.H * 1.25, self.cache), info))
        self.white = Image.new("RGB", (self.W, self.H), (255, 255, 255))

    def _draw_rajada(self, t):
        from PIL import Image
        k = max(i for i, c in enumerate(self.cuts) if c <= t + 1e-6)
        k = min(k, len(self.srcs) - 1)
        dt = t - self.cuts[k]
        nxt = self.cuts[k + 1] if k + 1 < len(self.cuts) else self.dur
        span = max(0.2, nxt - self.cuts[k])
        src, _info = self.srcs[k]
        z = 1.0 + 0.16 * (1 - ease_out_cubic(min(1.0, dt / 0.28))) + 0.05 * self.inten * (dt / span)
        cx = 0.5 + (0.03 if k % 2 else -0.03) * (dt / span)
        frame = camera(src, self.W, self.H, z, cx, 0.45)
        if dt < 0.10 and k > 0:  # flash no corte
            frame = Image.blend(frame, self.white, 0.55 * (1 - dt / 0.10))
        return frame

    def draw(self, t):
        return getattr(self, f"_draw_{self.layout}")(t)


# ------------------------------------------------------------------ render de uma cena → arquivo
def scene_key(spec, W, H, dur, beats, inten, pulse, kind):
    blob = json.dumps({k: spec.get(k) for k in ("layout", "fotos", "movimento", "foco_x", "foco_y")} |
                      {"W": W, "H": H, "d": round(dur, 3), "b": [round(b, 2) for b in beats], "i": inten, "p": pulse, "k": kind, "v": 3},
                      sort_keys=True)
    return hashlib.sha1(blob.encode()).hexdigest()[:10]


def render_scene(job):
    """job: dict com spec, infos, W, H, dur, fps, beats, inten, pulse, out, kind, ffmpeg, seed."""
    import tempfile
    out = Path(job["out"])
    if out.exists() and out.stat().st_size > 0:
        return str(out)
    sc = Scene(job["spec"], job["infos"], job["W"], job["H"], job["dur"], job["beats"], job["inten"], job["pulse"], job["seed"])
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
