"""Vídeo de fotos: a IA organiza, anima e sincroniza fotos com a trilha escolhida.

Fluxo: pasta de fotos + trilha → preparo (orientação, cor, enquadramento sem distorção) → batidas da trilha →
plano da IA diretora (ordem, tempo, movimento, transições, textos, motion graphics) → prévias (variações) →
aprovação → render final em alta.
"""
import base64
import json
import logging
import math
import random
import re
import subprocess
import time
from datetime import date
from pathlib import Path

from . import db, director, eventlog, luts, notify, paths, scenes, services, style
from . import ffmpeg as ff
from .errors import StepError, explain

log = logging.getLogger("cortex9.photos")

IMG_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".tif", ".tiff"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".aif", ".aiff", ".flac"}
MAX_FOTOS = 120
FPS = 30

SIZES = {"16:9": (1920, 1080), "9:16": (1080, 1920), "1:1": (1080, 1080), "4:5": (1080, 1350)}
MOVIMENTOS = ["zoom_in", "zoom_out", "pan_esquerda", "pan_direita", "pan_cima", "pan_baixo", "deriva"]
TRANSICOES = ["corte_seco", "fade", "fadeblack", "fadewhite", "dissolve", "smoothleft", "smoothright", "smoothup",
              "smoothdown", "circleopen", "radial", "zoomin", "hblur", "slideleft", "slideright", "wipeleft", "squeezeh", "pixelize"]
TEXTO_ESTILOS = ["titulo", "legenda", "destaque", "lower_third"]
ESTILOS = ["auto", "cinematografico", "dinamico", "elegante", "divertido", "emocionante"]
EFEITOS_F = ["vinheta", "granulado", "letterbox", "flash_batida", "luz_quente"]

DEFAULTS = {"titulo": "", "subtitulo": "", "estilo": "auto", "formatos": ["16:9"], "duracao": 0, "trilha": "", "contexto": "",
            "pasta_saida": "",      # vazio = "Pasta dos finais" do kit (padrão ~/Movies/CorteX9/Finais/AAAA-MM-DD/cliente/projeto)
            "enviar_drive": True,   # copia o final para o Google Drive depois de exportar
            "pasta_drive": ""}      # dentro do Drive; vazio = CorteX9/AAAA-MM-DD/cliente/projeto


# ------------------------------------------------------------------ banco
def create(folder, preset_id=None, **opts):
    folder = Path(folder).expanduser()
    if not folder.is_dir():
        raise ValueError(f"Pasta de fotos não encontrada: {folder}")
    photos = list_photos(folder)
    if len(photos) < 2:
        raise ValueError("A pasta precisa ter pelo menos 2 fotos (JPG, PNG, HEIC, WebP ou TIFF).")
    if preset_id is None:
        preset_id = db.one("SELECT id FROM presets ORDER BY id LIMIT 1")["id"]
    o = {**DEFAULTS, **{k: v for k, v in opts.items() if v not in (None, "")}}
    if o.get("pasta_saida"):
        out = Path(o["pasta_saida"]).expanduser()
        try:
            out.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise ValueError(f"Não consegui usar a pasta de destino {out}: {e.strerror or e}") from None
        o["pasta_saida"] = str(out)
    if not o.get("trilha"):
        inside = [f for f in sorted(folder.iterdir()) if f.suffix.lower() in AUDIO_EXTS]
        if inside:
            o["trilha"] = str(inside[0])
    pid = db.insert("photo_projects", preset_id=preset_id, folder=str(folder), title=o["titulo"] or folder.name, options=o,
                    status="na_fila", stage="Na fila", progress=0, created_at=db.now(), meta={})
    work = paths.WORK / f"fotos{pid:04d}_{re.sub(r'[^\w-]+', '_', folder.name)[:40]}"
    db.update("photo_projects", pid, work_dir=str(work))
    from . import jobs
    jobs.enqueue("analise", "photo_analyze", payload={"project_id": pid})
    eventlog.info(f"Projeto criado: {folder} ({len(photos)} fotos) · formatos {', '.join(o['formatos'])} · trilha: {o.get('trilha') or 'nenhuma'}"
                  + f" · finais em: {o.get('pasta_saida') or 'pasta do kit'} · Drive: {'sim' if o.get('enviar_drive', True) else 'não'}",
                  kind="fotos", ref=pid, step="criação")
    return pid


def get(pid):
    return db.one("SELECT * FROM photo_projects WHERE id=?", pid)


def _set(pid, **kv):
    db.update("photo_projects", pid, **kv)


def _stage(pid, name, progress=None, status=None):
    kv = {"stage": name}
    if progress is not None:
        kv["progress"] = progress
    if status:
        kv["status"] = status
    _set(pid, **kv)
    eventlog.info(name, kind="fotos", ref=pid)


def _warn(pid, msg):
    p = get(pid)
    meta = p["meta"] or {}
    meta.setdefault("avisos", [])
    if msg not in meta["avisos"]:
        meta["avisos"].append(msg)
        eventlog.warn(msg, kind="fotos", ref=pid)
    _set(pid, meta=meta)


def list_photos(folder: Path):
    return sorted([f for f in folder.iterdir() if f.is_file() and not f.name.startswith(".") and f.suffix.lower() in IMG_EXTS],
                  key=lambda f: f.name.lower())


# ------------------------------------------------------------------ preparo das fotos (sem distorção)
def _open(path):
    from PIL import Image, ImageOps
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except ImportError:
        pass
    im = Image.open(path)
    im = ImageOps.exif_transpose(im)  # respeita a orientação da câmera/celular
    return im.convert("RGB")


def _exif_date(path):
    try:
        from PIL import Image
        ex = Image.open(path).getexif()
        v = ex.get(36867) or ex.get(306) or (ex.get_ifd(0x8769) or {}).get(36867)
        return str(v) if v else None
    except Exception:  # noqa: BLE001
        return None


def _autocolor(im):
    """Correção automática suave: balanço de branco (mundo cinza parcial) + níveis."""
    from PIL import Image, ImageOps, ImageStat
    small = im.resize((64, 64))
    r, g, b = ImageStat.Stat(small).mean
    gray = (r + g + b) / 3 or 1

    def k(c):
        return max(0.88, min(1.14, 1 + (gray / max(c, 1) - 1) * 0.6))
    kr, kg, kb = k(r), k(g), k(b)
    if max(abs(kr - 1), abs(kg - 1), abs(kb - 1)) > 0.01:
        ch = im.split()
        im = Image.merge("RGB", [c.point(lambda v, f=f: min(255, int(v * f))) for c, f in zip(ch, (kr, kg, kb))])
    # níveis com ganho limitado (máx. 1,6×): corrige sem transformar a foto em outra
    h = im.convert("L").histogram()
    n = sum(h) or 1
    acc, lo = 0, 0
    for v in range(256):
        acc += h[v]
        if acc > n * 0.003:
            lo = v
            break
    acc, hi = 0, 255
    for v in range(255, -1, -1):
        acc += h[v]
        if acc > n * 0.006:
            hi = v
            break
    if hi - lo < 10:
        return im
    gain = min(1.6, 255 / (hi - lo))
    lo = min(lo, 40)
    if gain <= 1.02 and lo < 3:
        return im
    lut = [max(0, min(255, int((v - lo) * gain + 0.5))) for v in range(256)]
    return im.point(lut * 3)


# ------------------------------------------------------------------ ajuste automático de qualidade
def quality_profile(t):
    """Mede a foto (pela miniatura) e decide os ajustes: exposição, contraste, cor, ruído e nitidez."""
    from PIL import ImageFilter, ImageOps, ImageStat, ImageChops
    g0 = t.convert("L")
    g = _autocolor(t).convert("L")  # mede depois dos níveis, para não somar correções
    st = ImageStat.Stat(g)
    lum, contr = st.mean[0], st.stddev[0]
    sat = ImageStat.Stat(t.convert("HSV").split()[1]).mean[0]
    hist = g.histogram()
    n = sum(hist) or 1
    sombras = sum(hist[:16]) / n      # pretos estourados
    luzes = sum(hist[240:]) / n       # brancos estourados
    gn = ImageOps.autocontrast(g, cutoff=1)  # nitidez medida com contraste normalizado (foto escura não vira "borrada")
    edges = ImageStat.Stat(gn.filter(ImageFilter.FIND_EDGES)).var[0]
    ruido = ImageStat.Stat(ImageChops.difference(gn, gn.filter(ImageFilter.MedianFilter(3)))).mean[0]  # grão fino, não textura
    q = {"luminancia": round(lum), "contraste": round(contr), "saturacao": round(sat), "nitidez": round(edges, 1),
         "sombras_estouradas": round(sombras, 3), "luzes_estouradas": round(luzes, 3), "ruido": round(ruido, 1)}
    gamma = 1.0
    if lum < 100:
        gamma = min(1.0, max(0.78, math.log(0.38) / math.log(max(lum, 8) / 255)))  # clareia com moderação (noite continua noite)
    elif lum > 175:
        gamma = min(1.35, math.log(0.55) / math.log(lum / 255))         # recupera foto clara demais
    q["gamma"] = round(gamma, 3)
    q["contraste_fator"] = 1.08 if contr < 40 else (0.95 if contr > 90 else 1.0)
    q["vibracao"] = 1.12 if sat < 60 else (1.05 if sat < 95 else (0.94 if sat > 170 else 1.0))
    q["reduzir_ruido"] = bool(ruido > 1.5 and lum < 130)  # medido na miniatura: foto limpa ≈ 0,2; grão de noite ≈ 3+
    q["borrada"] = edges < 120
    q["nitidez_fator"] = 140 if edges < 250 else (90 if edges < 700 else 60)
    return q


def enhance(im, q, upscale=1.0):
    """Aplica o ajuste automático: cor → exposição → contraste → vibração → ruído → nitidez (adaptada ao upscale)."""
    from PIL import Image, ImageEnhance, ImageFilter
    im = _autocolor(im)
    q = q or {}
    gamma = q.get("gamma", 1.0)
    if abs(gamma - 1) > 0.02:
        lut = [min(255, int(255 * ((v / 255) ** gamma) + 0.5)) for v in range(256)]
        im = im.point(lut * 3)
    if q.get("contraste_fator", 1) != 1:
        im = ImageEnhance.Contrast(im).enhance(q["contraste_fator"])
    if q.get("vibracao", 1) != 1:
        im = ImageEnhance.Color(im).enhance(q["vibracao"])
    if q.get("reduzir_ruido"):
        im = Image.blend(im, im.filter(ImageFilter.MedianFilter(3)), 0.6)
    radius = 1.0 + 0.8 * max(0.0, min(2.0, upscale - 1))   # mais nitidez quando a foto é ampliada
    percent = int(q.get("nitidez_fator", 80) * (1.2 if upscale > 1.3 else 1.0))
    return im.filter(ImageFilter.UnsharpMask(radius=radius, percent=percent, threshold=3))


def ajustes_texto(q, low_res=False):
    a = ["balanço de branco", "níveis"]
    if q["gamma"] < 0.98:
        a.append("clareou")
    elif q["gamma"] > 1.02:
        a.append("escureceu")
    if q["contraste_fator"] > 1:
        a.append("+contraste")
    elif q["contraste_fator"] < 1:
        a.append("−contraste")
    if q["vibracao"] > 1:
        a.append("+cor")
    elif q["vibracao"] < 1:
        a.append("−cor")
    if q["reduzir_ruido"]:
        a.append("redução de ruído")
    a.append("nitidez")
    if low_res:
        a.append("baixa resolução: upscale lanczos")
    if q["borrada"]:
        a.append("foto tremida/borrada")
    return a


def analyze_photo(path, thumb_dir: Path, i):
    im = _open(path)
    w, h = im.size
    thumb = thumb_dir / f"t{i:03d}.jpg"
    t = im.copy()
    t.thumbnail((448, 448))
    q = quality_profile(t)
    t = enhance(t, q, 1.0)  # miniatura já ajustada (é o que a IA e você veem)
    t.save(thumb, quality=78)
    # nitidez aproximada (variância das bordas) para descartar fotos tremidas quando há fotos demais
    from PIL import ImageFilter, ImageStat
    edges = t.convert("L").filter(ImageFilter.FIND_EDGES)
    sharp = ImageStat.Stat(edges).var[0]
    import hashlib
    from . import scenes as _sc
    tmp = {"i": i, "arquivo": str(path)}
    try:
        inter = _sc.interest(tmp, im)
    except Exception:  # noqa: BLE001
        inter = None
    return {"interesse": inter, "i": i, "arquivo": str(path), "nome": Path(path).name, "w": w, "h": h, "data": _exif_date(path),
            "nitidez": q["nitidez"], "thumb": str(thumb), "dhash": _dhashes(t), "cores": _colors(t), "qualidade": q,
            "ajustes": ajustes_texto(q, low_res=min(w, h) < 900),
            "md5": hashlib.md5(Path(path).read_bytes()).hexdigest()}


# ------------------------------------------------------------------ fotos repetidas
DUP_LIMIAR = 5  # bits diferentes (de 64) para considerar a mesma imagem


def _dhash(im):
    """Impressão digital visual (dHash 64 bits): igual mesmo após redimensionar, recomprimir ou converter HEIC→JPG."""
    from PIL import Image
    g = im.convert("L").resize((9, 8), Image.LANCZOS)
    px = list(g.getdata())
    bits = 0
    for y in range(8):
        for x in range(8):
            bits = (bits << 1) | (1 if px[y * 9 + x] > px[y * 9 + x + 1] else 0)
    return bits


def _rotations(im):
    from PIL import Image
    return [im] + [im.transpose(t) for t in (Image.ROTATE_90, Image.ROTATE_180, Image.ROTATE_270)]


def _dhashes(im):
    """Hash da imagem e das rotações (90°, 180°, 270°): pega a mesma foto salva girada."""
    return [_dhash(r) for r in _rotations(im)]


def _colors(im):
    """Assinatura de cor 4×4 (48 valores) por rotação: separa fotos de mesma composição e cores diferentes."""
    from PIL import Image
    return [list(r.convert("RGB").resize((4, 4), Image.BOX).tobytes()) for r in _rotations(im)]


COR_LIMIAR = 16  # diferença média de cor (0–255) tolerada para a mesma foto (recompressão, filtro leve do WhatsApp)


_COPY_RE = re.compile(r"(\s*\(\d+\)|[\s_-]*(copy|c[óo]pia|copia|duplicad[ao])(\s*\d+)?)$", re.I)
NOME_DIFERENTE = 20  # mesmo nome, mas imagem claramente outra (ex.: IMG_0001 de duas câmeras) → mantém as duas


def name_key(name):
    """Nome-base sem extensão nem sufixos de cópia: 'IMG_1234 (1).JPG', 'IMG_1234 cópia.heic' → 'img_1234'."""
    base = Path(name).stem.strip().lower()
    prev = None
    while prev != base:
        prev, base = base, _COPY_RE.sub("", base).strip()
    return base or Path(name).stem.lower()


def _same_image(a, b):
    if a["md5"] == b["md5"]:
        return "arquivo idêntico"
    bits = bin(a["dhash"][0]).count("1")
    if bits < 6 or bits > 58:  # foto quase lisa (preta, céu liso): o hash não é confiável, só o arquivo idêntico conta
        return None
    for r, h in enumerate(b["dhash"]):
        if bin(a["dhash"][0] ^ h).count("1") <= DUP_LIMIAR:
            cdiff = sum(abs(x - y) for x, y in zip(a["cores"][0], b["cores"][r])) / 48
            if cdiff <= COR_LIMIAR:
                return "mesma imagem" + (" (girada)" if r else "")
    return None


def dedupe(infos):
    """Mantém só uma de cada foto repetida (pelo nome de arquivo OU pelo conteúdo). Fica a de maior resolução,
    depois a mais nítida. Devolve (fotos_mantidas, relatorio)."""
    groups = []  # cada grupo: lista de (info, motivo)
    by_name = {}
    for f in infos:
        key = name_key(f["nome"])
        g = by_name.get(key)
        motivo = "mesmo nome de arquivo" if g is not None else None
        if g is not None and min(bin(f["dhash"][0] ^ h).count("1") for h in g[0][0]["dhash"]) > NOME_DIFERENTE:
            g, motivo = None, None  # nome igual, conteúdo diferente: não é repetida
        if g is None:
            for cand in groups:
                m = _same_image(f, cand[0][0])
                if m:
                    g, motivo = cand, m
                    break
        if g is None:
            g = [(f, None)]
            groups.append(g)
        else:
            g.append((f, motivo))
        by_name.setdefault(key, g)
    keep, report = [], []
    for g in groups:
        best = max(g, key=lambda x: (x[0]["w"] * x[0]["h"], round(x[0]["nitidez"], -1),
                                     name_key(x[0]["nome"]) == Path(x[0]["nome"]).stem.lower(), -x[0]["i"]))[0]
        keep.append(best)
        if len(g) > 1:
            report.append({"mantida": best["nome"],
                           "removidas": [{"nome": f["nome"], "motivo": m or "mesma imagem"} for f, m in g if f is not best]})
    keep.sort(key=lambda x: x["i"])
    return keep, report


def make_plate(info, out: Path, W, H, scale, focus=(0.5, 0.5)):
    """Gera a 'placa' da foto, maior que o quadro (para ter margem de movimento), SEM distorcer:
    - proporção parecida com o quadro → recorte inteligente no ponto de foco;
    - proporção muito diferente (ex.: foto vertical em vídeo 16:9) → foto inteira sobre fundo desfocado dela mesma."""
    from PIL import Image, ImageFilter
    im = _open(info.get("arquivo_hd") or info["arquivo"])
    PW, PH = int(W * scale) // 2 * 2, int(H * scale) // 2 * 2
    ar_img, ar_frame = im.width / im.height, PW / PH
    fill = abs(math.log(ar_img / ar_frame)) < 0.30
    up = max(PW / im.width, PH / im.height) if fill else min(PW * 0.92 / im.width, PH * 0.92 / im.height)
    im = enhance(im, info.get("qualidade"), up / scale)  # nitidez proporcional à ampliação real no quadro
    if abs(math.log(ar_img / ar_frame)) < 0.30:  # 4:3 e 3:2 em 16:9 preenchem com recorte; vertical em horizontal ganha moldura
        s = max(PW / im.width, PH / im.height)
        nw, nh = math.ceil(im.width * s), math.ceil(im.height * s)
        big = im.resize((nw, nh), Image.LANCZOS)
        fx, fy = focus
        x = int(min(max(0, fx * nw - PW / 2), nw - PW))
        y = int(min(max(0, fy * nh - PH / 2), nh - PH))
        plate = big.crop((x, y, x + PW, y + PH))
        mode = "recorte"
    else:
        s = max(PW / im.width, PH / im.height)
        bg = im.resize((max(1, int(im.width * s / 8)), max(1, int(im.height * s / 8))), Image.BILINEAR)
        bg = bg.filter(ImageFilter.GaussianBlur(6)).resize((math.ceil(im.width * s), math.ceil(im.height * s)), Image.BILINEAR)
        bx, by = (bg.width - PW) // 2, (bg.height - PH) // 2
        plate = bg.crop((bx, by, bx + PW, by + PH))
        plate = Image.eval(plate, lambda v: int(v * 0.55))  # fundo escurecido
        fit = min(PW * 0.92 / im.width, PH * 0.92 / im.height)
        fg = im.resize((int(im.width * fit), int(im.height * fit)), Image.LANCZOS)
        shadow = Image.new("RGB", (fg.width + 40, fg.height + 40), (0, 0, 0)).filter(ImageFilter.GaussianBlur(0))
        sx, sy = (PW - fg.width) // 2, (PH - fg.height) // 2
        mask = Image.new("L", shadow.size, 0)
        mask.paste(120, (20, 20, shadow.width - 20, shadow.height - 20))
        mask = mask.filter(ImageFilter.GaussianBlur(18))
        plate.paste(shadow, (sx - 20, sy - 10), mask)
        plate.paste(fg, (sx, sy))
        mode = "moldura"
    plate.save(out, quality=92)
    return mode


# ------------------------------------------------------------------ trilha: batidas e energia
def analyze_music(path: Path):
    import numpy as np
    sr = 22050
    p = subprocess.run([ff.FFMPEG(), "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"], capture_output=True)
    if p.returncode != 0 or not p.stdout:
        raise StepError("trilha", "Não consegui ler a trilha.", "Use um arquivo MP3, WAV ou M4A que toque no Mac.", p.stderr.decode()[-500:])
    x = np.frombuffer(p.stdout, dtype=np.float32)
    dur = len(x) / sr
    hop, n = 512, 1024
    frames = max(1, (len(x) - n) // hop)
    idx = np.arange(n)[None, :] + hop * np.arange(frames)[:, None]
    spec = np.abs(np.fft.rfft(x[idx] * np.hanning(n), axis=1))
    logspec = np.log1p(spec * 10)
    flux = np.maximum(0, np.diff(logspec, axis=0)).sum(axis=1)
    flux = np.concatenate([[0], flux])
    flux = (flux - flux.mean()) / (flux.std() + 1e-9)
    fps_env = sr / hop
    # andamento por autocorrelação (60–180 BPM)
    ac = np.correlate(flux, flux, mode="full")[len(flux) - 1:]
    lags = np.arange(len(ac))
    bpm_l = (fps_env * 60 / np.maximum(lags, 1))
    mask = (bpm_l >= 60) & (bpm_l <= 180)
    weight = np.exp(-0.5 * (np.log2(bpm_l / 110) / 0.9) ** 2)  # preferência suave por ~110 BPM
    lag = int(lags[mask][np.argmax((ac * weight)[mask])]) if mask.any() else int(fps_env / 2)
    if 1 <= lag < len(ac) - 1:  # interpolação parabólica: período fracionário (BPM preciso)
        a0, a1, a2 = ac[lag - 1], ac[lag], ac[lag + 1]
        den = a0 - 2 * a1 + a2
        flag = lag + (0.5 * (a0 - a2) / den if den else 0)
    else:
        flag = float(lag)
    bpm = fps_env * 60 / flag
    phase = int(np.argmax([flux[np.round(o + np.arange(0, frames - o, flag)).astype(int).clip(0, frames - 1)].sum() for o in range(lag)]))
    beats = [round((phase + k * flag) / fps_env, 3) for k in range(int((frames - phase) / flag) + 1)]
    # energia por segundo (para achar partes calmas, crescendo e "drops")
    rms = np.sqrt(np.convolve(x ** 2, np.ones(sr) / sr, mode="valid")[::sr // 2] + 1e-12)
    rms = rms / (rms.max() + 1e-9)
    energy = [round(float(v), 2) for v in rms]
    drops = [round(i / 2, 1) for i in range(4, len(rms)) if rms[i] - rms[max(0, i - 4):i].mean() > 0.25][:12]
    return {"duracao": round(dur, 2), "bpm": round(float(bpm), 1), "batidas": beats, "energia_meio_segundo": energy, "picos": drops}


# ------------------------------------------------------------------ plano (IA diretora)
_n, _s = {"type": "number"}, {"type": "string"}
_obj = director._obj
PLANO_FOTOS = _obj({
    "titulo": _s, "subtitulo": _s, "texto_final": _s, "lut": {"type": "string", "enum": list(luts.LUTS)},
    "efeitos": {"type": "array", "items": {"type": "string", "enum": EFEITOS_F}},
    "justificativa": _s,
    "cenas": {"type": "array", "items": _obj({
        "ato": {"type": "string", "enum": ["abertura", "apresentacao", "desenvolvimento", "climax", "final"]},
        "layout": {"type": "string", "enum": list(scenes.LAYOUTS)},
        "fotos": {"type": "array", "items": {"type": "integer"}}, "batidas": {"type": "integer"},
        "movimento": {"type": "string", "enum": MOVIMENTOS}, "foco_x": _n, "foco_y": _n,
        "transicao": {"type": "string", "enum": TRANSICOES},
        "texto": _s, "estilo_texto": {"type": "string", "enum": TEXTO_ESTILOS + ["nenhum"]},
        "motivo": _s})},
    "variacoes": {"type": "array", "items": _obj({
        "nome": _s, "lut": {"type": "string", "enum": list(luts.LUTS)}, "intensidade_movimento": _n,
        "transicao_padrao": {"type": "string", "enum": TRANSICOES + ["do_plano"]},
        "efeitos": {"type": "array", "items": {"type": "string", "enum": EFEITOS_F}}, "justificativa": _s})},
})

SYSTEM_FOTOS = """Você é a diretora de edição do CorteX9. Sua tarefa é transformar uma pasta de fotos num vídeo EMOCIONANTE, sincronizado com a trilha, com cara de filme de evento ou de marca. NÃO é um slideshow: um slideshow mostra uma foto por vez, na ordem dos arquivos, com o mesmo ritmo do começo ao fim. Você vai contar uma história em atos, montar cenas com várias fotos e fazer a energia subir e descer com a música.

## Como pensar
1. Olhe todas as fotos e descubra a HISTÓRIA: quem são as pessoas, onde é, o que acontece, quais são os momentos fortes (abraços, risadas, palco, produto, grupo, detalhe bonito, surpresa).
2. Agrupe fotos do MESMO momento (mesma situação, mesmas pessoas, mesmo lugar) em uma cena com várias fotos. Fotos parecidas viram dupla, trio, grade ou colagem, e não cenas separadas.
3. Monte a ordem pela NARRATIVA, não pela ordem dos arquivos nem só pela data: abertura de impacto → apresentação (lugar, pessoas) → desenvolvimento (momentos, variedade) → CLÍMAX no pico da trilha → final emocionante.
4. Use a energia da trilha ("energia_meio_segundo" e "picos"): partes calmas pedem cenas mais longas, como a única foto heroica com movimento lento ou a dupla; crescendo pede trio, grade e colagem com fotos entrando na batida; picos pedem RAJADA de cortes rápidos.

## Layouts ("layout" + "fotos" = índices das fotos, na ordem em que entram)
- "unica" (1 foto): a foto heroica em tela cheia com movimento de câmera. Use nas fotos mais fortes: abertura, momento-chave e final. No máximo 40% das cenas.
- "dupla" (2 fotos): lado a lado, entram de lados opostos. Ótimo para duas fotos verticais, antes/depois ou duas reações ao mesmo momento.
- "trio" (3 fotos): uma grande e duas menores, entrando uma por batida. Uma foto principal e dois detalhes do mesmo momento.
- "grade" (4 fotos): grade 2×2 com as fotos surgindo uma por batida. Para equipe, participantes, produtos ou "muita coisa aconteceu".
- "colagem" (3 a 5 fotos): fotos impressas com borda branca caindo uma por batida sobre a mesa. Clima afetivo e nostálgico, bastidores e confraternização.
- "rajada" (3 a 6 fotos): cortes rápidos, um por batida, com punch-in e flash. Para os PICOS da música e o clímax.
Pelo menos metade das fotos usadas deve estar em cenas com 2 ou mais fotos. Varie os layouts e não repita o mesmo layout três vezes seguidas.
- NÃO coloque duas fotos quase iguais (mesmo grupo na mesma pose, mesmo enquadramento) na mesma cena: escolha a melhor e descarte a outra.
- Fotos de "baixa resolução" ficam melhor em dupla, trio, grade ou colagem do que em "unica" de tela cheia.
- Numa cena com várias fotos, a PRIMEIRA da lista é a principal (fica no maior painel ou por cima na colagem).

## Outras regras
- Use cada foto no máximo uma vez. Descarte fotos tremidas, fracas ou quase iguais.
- "batidas": quanto tempo a cena dura, em batidas. Referência: unica 4 a 8; dupla 5 a 7; trio 6 a 8; grade 7 a 9; colagem igual ao número de fotos + 4; rajada igual ao número de fotos (1 batida por foto). A soma deve cobrir a duração alvo. Faça uma rajada cair exatamente no maior pico.
- "movimento" e "foco_x"/"foco_y" (0 a 1): câmera e ponto de interesse da cena (rosto principal, assunto). Valem principalmente para "unica".
- "transicao": transição DE ENTRADA da cena (a primeira cena ignora). Calmo: fade, dissolve, smooth*. Energia: corte_seco, zoomin, slide*, fadewhite. Antes e depois da rajada, prefira corte_seco.
- "texto": pouco e forte. Título na primeira cena (estilo "titulo"), "lower_third" para lugar, data ou nome, e 2 ou 3 frases curtas de emoção ("destaque" ou "legenda") nos momentos-chave. Nas outras cenas, "" e "nenhum".
- "ato": a qual ato a cena pertence.
- "titulo"/"subtitulo": se o usuário não deu, crie a partir do que você vê. "texto_final": frase curta que emocione.
- "lut": pelo clima das fotos e da música (epico, intimista, noir, sonho, saturado, dessaturado).
- "variacoes": 2 variações. A primeira, principal, usa "transicao_padrao": "do_plano" e "intensidade_movimento" 1.0. A segunda é mais enérgica (intensidade 1.3 a 1.5, outra LUT, "flash_batida").
- "justificativa": 2 frases para o usuário, sobre a história e o ritmo escolhidos.
"""


def _ai_plan(pid, infos, music, opts, preset, preset_id, target):
    content = []
    for f in infos:
        qual = "; tremida/borrada" if (f.get("qualidade") or {}).get("borrada") else ""
        qual += "; baixa resolução" if min(f["w"], f["h"]) < 900 else ""
        ori = "vertical" if f["h"] > f["w"] * 1.1 else ("horizontal" if f["w"] > f["h"] * 1.1 else "quadrada")
        content.append({"type": "text", "text": f"Foto {f['i']} — {f['nome']} ({f['w']}x{f['h']}, {ori}{', ' + f['data'] if f['data'] else ''}{qual})"})
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                   "data": base64.standard_b64encode(Path(f["thumb"]).read_bytes()).decode()}})
    beat_len = 60 / music["bpm"] if music else 0.5
    content.append({"type": "text", "text": json.dumps({
        "pedido_do_usuario": {k: opts.get(k) for k in ("titulo", "subtitulo", "estilo", "contexto")},
        "formato": opts["formatos"][0], "duracao_alvo_s": round(target, 1), "total_fotos": len(infos),
        "trilha": {"bpm": music["bpm"], "duracao_batida_s": round(beat_len, 3), "total_batidas_no_alvo": int(target / beat_len),
                   "energia_meio_segundo": music["energia_meio_segundo"][: int(target * 2) + 1], "picos": music["picos"]} if music else None,
        "kit_cliente": {k: preset.get(k) for k in ("cliente", "projeto", "contexto")},
        "historico_de_gosto": style.as_prompt(preset_id),
    }, ensure_ascii=False) + "\n\nDevolva o plano do vídeo."})
    errs = []
    for name, fn in (("Claude", director._claude), ("ChatGPT (reserva)", director._openai)):
        try:
            return fn(SYSTEM_FOTOS, content, PLANO_FOTOS, -pid, **({"effort": "high"} if fn is director._claude else {})), name, errs
        except Exception as e:  # noqa: BLE001
            errs.append(f"{name}: {explain('IA diretora', e).reason}")
    return None, None, errs


def _similar(a, b):
    """Mesmo momento? (fotos tiradas perto no tempo ou com cores parecidas)."""
    da, db_ = a.get("data"), b.get("data")
    if da and db_ and da[:13] == db_[:13]:  # mesma hora
        try:
            from datetime import datetime
            fa, fb = (datetime.strptime(x[:19], "%Y:%m:%d %H:%M:%S") for x in (da, db_))
            if abs((fa - fb).total_seconds()) < 240:
                return True
        except ValueError:
            pass
    ca, cb = a.get("cores", [[0] * 48])[0], b.get("cores", [[0] * 48])[0]
    return sum(abs(x - y) for x, y in zip(ca, cb)) / 48 < 38


def _local_plan(infos, music, opts, target):
    """Sem IA: agrupa fotos do mesmo momento em cenas, varia os layouts e põe rajada no pico da trilha."""
    rnd = random.Random(len(infos))
    order = sorted(infos, key=lambda f: (f["data"] or "9999", f["nome"].lower()))
    sharp = sorted(order, key=lambda f: (-(f["w"] >= f["h"]), -(min(f["w"], f["h"]) >= 900), -f["nitidez"]))
    hero_open = sharp[0]
    hero_end = next((f for f in sharp[1:] if f is not hero_open), None) if len(order) > 4 else None
    rest = [f for f in order if f is not hero_open and f is not hero_end]
    # grupos de fotos do mesmo momento (perto no tempo ou com cores parecidas)
    gid, g = [], 0
    for k, f in enumerate(rest):
        if k and not _similar(rest[k - 1], f):
            g += 1
        gid.append(g)
    beat = 60 / music["bpm"] if music else 0.5
    energy = (music or {}).get("energia_meio_segundo") or [0.5]
    picos = (music or {}).get("picos") or [target * 0.7]
    peak_t = max(picos, key=lambda x: energy[min(len(energy) - 1, int(x * 2))])
    estilo = opts.get("estilo", "auto")
    soft = ["fade", "dissolve", "smoothleft", "smoothright", "smoothup", "hblur"]
    hard = ["corte_seco", "zoomin", "slideleft", "slideright", "fadewhite", "circleopen"]
    cenas = [{"ato": "abertura", "layout": "unica", "fotos": [hero_open["i"]], "batidas": 6, "movimento": "zoom_out",
              "foco_x": 0.5, "foco_y": 0.42, "transicao": "fade", "texto": "", "estilo_texto": "nenhum", "motivo": "abertura com a foto mais forte"}]
    t = 6 * beat
    pattern = ["dupla", "trio", "unica", "grade", "colagem", "dupla", "unica", "trio", "grade", "colagem"]
    need = {"unica": 1, "dupla": 2, "trio": 3, "grade": 4, "colagem": 4, "rajada": 5}
    pos, step, last, rajada_done = 0, 0, "unica", False
    while pos < len(rest):
        left = len(rest) - pos
        glen = 1
        while pos + glen < len(rest) and gid[pos + glen] == gid[pos] and glen < 5:
            glen += 1
        e = energy[min(len(energy) - 1, int(t * 2))]
        if not rajada_done and left >= 4 and t >= peak_t - 5 * beat:
            lay, rajada_done, motivo = "rajada", True, "rajada de cortes no pico da trilha"
        elif 2 <= glen <= 5:
            lay = {2: "dupla", 3: "trio" if last != "trio" else "colagem", 4: "grade" if last != "grade" else "colagem", 5: "colagem"}[glen]
            motivo = "fotos do mesmo momento juntas"
        else:
            lay = pattern[step % len(pattern)]
            if lay == last:
                lay = pattern[(step + 1) % len(pattern)]
            step += 1
            motivo = "variação de ritmo"
        n = min(need[lay] if lay not in ("dupla", "trio", "grade", "colagem") or glen < 2 else min(glen, scenes.LAYOUTS[lay][1]), left)
        if lay == "colagem" and n < 3 or lay == "rajada" and n < 3:
            lay = scenes.layout_for_count(n)
        lay = lay if n >= scenes.LAYOUTS[lay][0] else scenes.layout_for_count(n)
        grp = rest[pos:pos + n]
        pos += n
        b = {"unica": 5, "dupla": 6, "trio": 7, "grade": 8, "colagem": n + 4, "rajada": n}[lay]
        if estilo == "dinamico" or e > 0.75:
            b = max(scenes.MIN_BEATS[lay], int(b * 0.75))
        f0 = grp[0]
        mov = rnd.choice(["zoom_in", "zoom_out", "deriva"]) if f0["w"] < f0["h"] else rnd.choice(MOVIMENTOS)
        trans = "corte_seco" if lay == "rajada" or last == "rajada" else rnd.choice(hard if (e >= 0.6 or estilo == "dinamico") else soft)
        cenas.append({"ato": "climax" if lay == "rajada" else "desenvolvimento", "layout": lay, "fotos": [f["i"] for f in grp], "batidas": b,
                      "movimento": mov, "foco_x": 0.5, "foco_y": 0.42, "transicao": trans, "texto": "", "estilo_texto": "nenhum", "motivo": motivo})
        t += b * beat
        last = lay
    if hero_end:
        cenas.append({"ato": "final", "layout": "unica", "fotos": [hero_end["i"]], "batidas": 8, "movimento": "zoom_in", "foco_x": 0.5,
                      "foco_y": 0.42, "transicao": "fadeblack" if last != "rajada" else "fadewhite", "texto": "", "estilo_texto": "nenhum",
                      "motivo": "final com foto forte e movimento lento"})
    title = opts.get("titulo") or Path(infos[0]["arquivo"]).parent.name.replace("_", " ")
    cenas[0].update(texto=title, estilo_texto="titulo")
    if len(cenas) > 2 and opts.get("subtitulo"):
        cenas[1].update(texto=opts["subtitulo"], estilo_texto="lower_third")
    return {"titulo": title, "subtitulo": opts.get("subtitulo", ""), "texto_final": opts.get("subtitulo") or "",
            "lut": "intimista" if estilo in ("elegante", "emocionante") else "epico", "efeitos": ["vinheta"],
            "justificativa": "Plano local: fotos do mesmo momento agrupadas em cenas (dupla, trio, grade, colagem), rajada no pico da trilha e abertura/final com as fotos mais fortes.",
            "cenas": cenas, "variacoes": []}


def scenes_movs():
    return MOVIMENTOS


def _drop_lookalikes(ids, byi, limiar=14):
    """Na mesma cena, duas fotos quase iguais (mesmo grupo, mesma pose) ficam feias juntas: mantém a primeira."""
    keep = []
    for i in ids:
        a = byi.get(i) or {}
        dup = False
        for j in keep:
            b = byi.get(j) or {}
            if a.get("dhash") and b.get("dhash"):
                dist = min(bin(a["dhash"][0] ^ h).count("1") for h in b["dhash"])
                cdiff = sum(abs(x - y) for x, y in zip(a["cores"][0], b["cores"][0])) / 48 if a.get("cores") and b.get("cores") else 99
                if dist <= limiar and cdiff <= 28:
                    dup = True
                    break
        if not dup:
            keep.append(i)
    return keep


def sanitize(plan, infos, music, opts):
    valid = {f["i"] for f in infos}
    byi = {f["i"]: f for f in infos}
    seen, cenas = set(), []
    for c in plan.get("cenas", []):
        fotos = c.get("fotos")
        if not isinstance(fotos, list) or not fotos:
            fotos = [c.get("foto")] if c.get("foto") is not None else []   # planos antigos: uma foto por cena
        fotos = [f for f in dict.fromkeys(fotos) if f in valid and f not in seen]
        fotos = _drop_lookalikes(fotos, byi)
        if not fotos:
            continue
        lay = c.get("layout") if c.get("layout") in scenes.LAYOUTS else scenes.layout_for_count(len(fotos))
        lo, hi = scenes.LAYOUTS[lay]
        if len(fotos) < lo:
            lay = scenes.layout_for_count(len(fotos))
            lo, hi = scenes.LAYOUTS[lay]
        fotos = fotos[:hi]
        seen.update(fotos)
        c["fotos"], c["foto"], c["layout"] = fotos, fotos[0], lay
        b = int(max(1, min(24, c.get("batidas") or 4)))
        if lay == "rajada":
            b = max(b, len(fotos))
        c["batidas"] = max(b, scenes.MIN_BEATS[lay])
        c["movimento"] = c.get("movimento") if c.get("movimento") in MOVIMENTOS else "zoom_in"
        c["transicao"] = c.get("transicao") if c.get("transicao") in TRANSICOES else "fade"
        c["foco_x"] = min(1, max(0, float(c.get("foco_x", 0.5) or 0.5)))
        c["foco_y"] = min(1, max(0, float(c.get("foco_y", 0.45) or 0.45)))
        if c.get("estilo_texto") not in TEXTO_ESTILOS:
            c["estilo_texto"], c["texto"] = "nenhum", ""
        cenas.append(c)
    if sum(len(c["fotos"]) for c in cenas) < 2:
        cenas = []
    if not cenas:
        raise StepError("plano das fotos", "O plano não usou fotos suficientes.", "Clique em Tentar de novo.")
    plan["cenas"] = cenas
    plan["lut"] = plan.get("lut") if plan.get("lut") in luts.LUTS else "epico"
    plan["efeitos"] = [e for e in plan.get("efeitos", []) if e in EFEITOS_F]
    vs = [v for v in plan.get("variacoes", []) if isinstance(v, dict)][:3]
    if not vs:
        vs = [{"nome": "Principal", "lut": plan["lut"], "intensidade_movimento": 1.0, "transicao_padrao": "do_plano",
               "efeitos": plan["efeitos"], "justificativa": "Versão principal do plano."}]
    if len(vs) < 2:
        other = "sonho" if plan["lut"] != "sonho" else "saturado"
        vs.append({"nome": "Dinâmica", "lut": other, "intensidade_movimento": 1.4, "transicao_padrao": "do_plano",
                   "efeitos": list(dict.fromkeys(plan["efeitos"] + ["flash_batida"])),
                   "justificativa": "Movimentos mais fortes, flash nas batidas e outra LUT."})
    for v in vs:
        v["lut"] = v.get("lut") if v.get("lut") in luts.LUTS else plan["lut"]
        v["intensidade_movimento"] = min(2.0, max(0.5, float(v.get("intensidade_movimento") or 1)))
        v["efeitos"] = [e for e in v.get("efeitos", []) if e in EFEITOS_F]
        if v.get("transicao_padrao") not in TRANSICOES:
            v["transicao_padrao"] = "do_plano"
    if all(v["lut"] == vs[0]["lut"] for v in vs):
        vs[-1]["lut"] = "sonho" if vs[0]["lut"] != "sonho" else "saturado"
    plan["variacoes"] = vs
    return plan


def timeline(plan, music, target):
    """Converte batidas em tempos (s), encaixando as trocas nas batidas reais da trilha."""
    beats = (music or {}).get("batidas") or []
    beat = 60 / music["bpm"] if music else 0.5
    start = beats[0] if beats and beats[0] < 1.0 else 0.0
    grid = [b for b in beats if b >= start] or [i * beat for i in range(10000)]
    total_beats = sum(c["batidas"] for c in plan["cenas"])
    want = max(1, int(round((target - start) / beat))) if target else total_beats
    scale = want / total_beats if total_beats else 1
    bounds, acc = [0.0], 0.0
    for c in plan["cenas"][:-1]:
        acc += max(1, c["batidas"] * scale)
        k = min(len(grid) - 1, int(round(acc)))
        t = grid[k] if k < len(grid) else acc * beat
        if t - bounds[-1] < 0.9:  # mínimo de 0,9 s por foto
            t = bounds[-1] + 0.9
        bounds.append(round(float(t), 3))
    last = plan["cenas"][-1]["batidas"] * scale * beat if plan["cenas"] else 2.0
    end = max(target or 0, bounds[-1] + max(2.0, last))
    bounds.append(round(end, 3))
    return [float(b) for b in bounds]


# ------------------------------------------------------------------ análise completa
def analyze(pid):
    p = get(pid)
    opts = {**DEFAULTS, **(p["options"] or {})}
    from .pipeline import preset_of
    preset_id, preset = preset_of(p)
    work = Path(p["work_dir"])
    (work / "miniaturas").mkdir(parents=True, exist_ok=True)
    _stage(pid, "Lendo as fotos", 0.03, status="processando")
    files = list_photos(Path(p["folder"]))
    if len(files) < 2:
        raise StepError("fotos", "A pasta tem menos de 2 fotos.", "Coloque as fotos (JPG, PNG, HEIC) na pasta e tente de novo.")
    infos = []
    for i, f in enumerate(files):
        try:
            infos.append(analyze_photo(f, work / "miniaturas", i))
        except Exception as e:  # noqa: BLE001
            _warn(pid, f"Foto ignorada ({f.name}): {explain('foto', e).reason}")
            eventlog.warn(f"Detalhe da foto ignorada {f.name}", kind="fotos", ref=pid, detail=f"{type(e).__name__}: {e}")
        _set(pid, progress=0.03 + 0.25 * (i + 1) / len(files))
    infos, dups = dedupe(infos)
    if dups:
        n = sum(len(d["removidas"]) for d in dups)
        _warn(pid, f"{n} foto(s) repetida(s) removida(s): " + "; ".join(
            f"{d['mantida']} ← " + ", ".join(f"{r['nome']} ({r['motivo']})" for r in d["removidas"]) for d in dups[:8])
            + (" …" if len(dups) > 8 else ""))
    if len(infos) < 2:
        raise StepError("fotos", "Depois de tirar as repetidas, sobrou menos de 2 fotos.", "Adicione mais fotos diferentes na pasta.")
    low = [f for f in infos if min(f["w"], f["h"]) < 900]
    if low:
        _warn(pid, f"{len(low)} foto(s) de baixa resolução: ampliadas com upscale local (lanczos + nitidez).")
    blur = [f["nome"] for f in infos if f["qualidade"]["borrada"]]
    if blur:
        _warn(pid, f"Foto(s) tremida(s)/borrada(s): {', '.join(blur[:10])}. A IA evita usá-las em destaque.")
    if len(infos) > MAX_FOTOS:
        _warn(pid, f"{len(infos)} fotos: usei as {MAX_FOTOS} mais nítidas.")
        keep = sorted(infos, key=lambda x: -x["nitidez"])[:MAX_FOTOS]
        infos = sorted(keep, key=lambda x: x["i"])

    music, mpath = None, None
    if opts.get("trilha"):
        mpath = Path(opts["trilha"]).expanduser()
    elif preset.get("fonte_trilha") == "epidemic" and services.get("epidemic_token"):
        try:
            mpath = services.epidemic_track({"emocionante": "inspirador", "divertido": "alegre", "dinamico": "saturado"}.get(opts["estilo"], "épico"), work)
        except Exception as e:  # noqa: BLE001
            _warn(pid, f"Epidemic Sound: {explain('trilha', e).reason}")
    elif preset.get("trilha"):
        mpath = Path(preset["trilha"]).expanduser()
    if mpath and mpath.is_dir():
        mpath = next((x for x in sorted(mpath.rglob("*")) if x.suffix.lower() in AUDIO_EXTS), None)
    if mpath and mpath.exists():
        _stage(pid, "Analisando batidas da trilha", 0.32)
        music = analyze_music(mpath)
    else:
        _warn(pid, "Sem trilha: o vídeo sai com ritmo fixo e sem música. Escolha uma trilha e peça um ajuste.")
    per_photo = 2.6 if opts["estilo"] != "dinamico" else 1.8   # várias fotos por cena: menos tempo por foto
    natural = len(infos) * per_photo + 2
    target = float(opts.get("duracao") or 0) or (min(music["duracao"], max(natural, 15)) if music else natural)
    _stage(pid, "IA diretora montando o vídeo", 0.4)
    plan, engine, errs = _ai_plan(pid, infos, music, opts, preset, preset_id, target)
    for e in errs:
        _warn(pid, e)
    if plan is None:
        plan, engine = _local_plan(infos, music, opts, target), "Plano local (sem IA na nuvem)"
    plan = sanitize(plan, infos, music, opts)
    plan["_tempos"] = timeline(plan, music, target)
    plan["_alvo"] = target
    eventlog.info(f"Plano pronto ({engine}): {len(plan['cenas'])} cenas, {fmt_t(plan['_tempos'][-1])}, {len(plan['variacoes'])} versões", kind="fotos", ref=pid,
                  detail=json.dumps({k: v for k, v in plan.items() if k != "_tempos"}, ensure_ascii=False, indent=1)[:15000])
    meta = (get(pid)["meta"] or {})
    meta.update(motor=engine, fotos=infos, duplicadas=dups, trilha=str(mpath) if mpath else None,
                musica={k: music[k] for k in ("duracao", "bpm", "picos", "batidas")} if music else None)
    _set(pid, plan=plan, meta=meta, variations=[{"idx": i + 1, "spec": v, "status": "na_fila"} for i, v in enumerate(plan["variacoes"])])
    from . import jobs
    for i in range(len(plan["variacoes"])):
        jobs.enqueue("previa", "photo_preview", payload={"project_id": pid, "var": i})
    _stage(pid, f"Gerando prévias ({len(plan['cenas'])} fotos, {fmt_t(plan['_tempos'][-1])})", 0.55, status="gerando_previas")


def fmt_t(s):
    return f"{int(s // 60)}:{int(s % 60):02d}"


# ------------------------------------------------------------------ render
def _zoompan(mov, frames, W, H, inten):
    """Expressões de câmera suaves (smoothstep) com margem da placa."""
    p = f"(on/{max(1, frames - 1)})"
    e = f"({p}*{p}*(3-2*{p}))"
    a = 0.10 * inten
    if mov == "zoom_in":
        z = f"1+{a:.3f}*{e}"
    elif mov == "zoom_out":
        z = f"{1 + a:.3f}-{a:.3f}*{e}"
    elif mov == "deriva":
        z = f"1.06+{a * 0.3:.3f}*{e}"
    else:
        z = f"{1.08 + a * 0.4:.3f}"
    return z, e


def _xy(mov, e, fx, fy):
    cx = f"max(0,min(iw-iw/zoom,{fx:.3f}*iw-iw/zoom/2))"
    cy = f"max(0,min(ih-ih/zoom,{fy:.3f}*ih-ih/zoom/2))"
    if mov == "pan_esquerda":
        return f"(iw-iw/zoom)*(1-{e})", cy
    if mov == "pan_direita":
        return f"(iw-iw/zoom)*{e}", cy
    if mov == "pan_cima":
        return cx, f"(ih-ih/zoom)*(1-{e})"
    if mov == "pan_baixo":
        return cx, f"(ih-ih/zoom)*{e}"
    if mov == "deriva":
        return f"(iw-iw/zoom)*(0.35+0.3*{e})", f"(ih-ih/zoom)*(0.5-0.15*{e})"
    return cx, cy


def _ass(path, W, H, plan, bounds, preset, v):
    from .render import _ass_color, _ass_t, _esc
    L = preset.get("legenda") or {}
    font = L.get("fonte", "Arial Black")
    col, hi = _ass_color(L.get("cor", "#FFFFFF")), _ass_color(L.get("destaque", "#FFD400"))
    u = H / 1080 if W >= H else W / 1080
    lines = ["[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}", "ScaledBorderAndShadow: yes", "", "[V4+ Styles]",
             "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
             f"Style: Tit,{font},{int(96 * u)},{col},{col},&H64000000,&H00000000,-1,0,0,0,100,100,2,0,1,{max(1, int(2 * u))},{int(3 * u)},5,60,60,0,1",
             f"Style: Sub,{font},{int(40 * u)},{hi},{hi},&H64000000,&H00000000,0,0,0,0,100,100,6,0,1,{max(1, int(1 * u))},{int(2 * u)},5,60,60,0,1",
             f"Style: Leg,{font},{int(50 * u)},{col},{col},&H38000000,&H00000000,-1,0,0,0,100,100,1,0,3,{max(4, int(14 * u))},0,2,80,80,{int(110 * u)},1",
             f"Style: Dest,{font},{int(72 * u)},{hi},{hi},&H30000000,&H00000000,-1,0,0,0,100,100,1,0,3,{max(6, int(22 * u))},0,5,80,80,0,1",
             f"Style: LT,{font},{int(40 * u)},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,1,0,1,0,0,1,0,0,0,1",
             f"Style: Box,{font},10,{hi},{hi},&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1",
             "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]
    ev = lines.append
    total = bounds[-1]
    up = L.get("maiusculas", True)

    def T(s):
        return _esc(s.upper() if up else s)
    for k, c in enumerate(plan["cenas"]):
        txt, st = (c.get("texto") or "").strip(), c.get("estilo_texto")
        if not txt or st == "nenhum":
            continue
        s, e = bounds[k] + 0.25, max(bounds[k] + 1.2, bounds[k + 1] - 0.15)
        if st == "titulo":
            bw = int(W * 0.34)
            ev(f"Dialogue: 2,{_ass_t(s)},{_ass_t(e)},Tit,,0,0,0,,{{\\pos({W // 2},{int(H * .46)})\\fad(350,400)\\blur8\\fsp30\\t(0,700,\\blur0\\fsp2)}}{T(txt)}")
            ev(f"Dialogue: 1,{_ass_t(s + 0.35)},{_ass_t(e)},Box,,0,0,0,,{{\\an7\\pos({(W - bw) // 2},{int(H * .53)})\\fscx0\\t(0,600,\\fscx100)\\fad(0,400)\\p1}}m 0 0 l {bw} 0 {bw} {max(3, int(6 * u))} 0 {max(3, int(6 * u))}{{\\p0}}")
            sub = plan.get("subtitulo") or ""
            if sub:
                ev(f"Dialogue: 2,{_ass_t(s + 0.7)},{_ass_t(e)},Sub,,0,0,0,,{{\\pos({W // 2},{int(H * .585)})\\fad(400,400)\\alpha&HFF&\\t(0,500,\\alpha&H00&)}}{T(sub)}")
        elif st == "destaque":
            ev(f"Dialogue: 2,{_ass_t(s)},{_ass_t(e)},Dest,,0,0,0,,{{\\pos({W // 2},{H // 2})\\fscx40\\fscy40\\t(0,220,\\fscx108\\fscy108)\\t(220,320,\\fscx100\\fscy100)\\fad(0,250)}}{T(txt)}")
        elif st == "lower_third":
            x0, y0 = int(W * .06), int(H * .80)
            bw, bh = int(W * .36), int(70 * u)
            ev(f"Dialogue: 1,{_ass_t(s)},{_ass_t(e)},Box,,0,0,0,,{{\\an7\\move({x0 - bw},{y0},{x0},{y0},0,350)\\fad(0,250)\\1a&H30&\\p1}}m 0 0 l {bw} 0 {bw} {bh} 0 {bh}{{\\p0}}")
            ev(f"Dialogue: 1,{_ass_t(s)},{_ass_t(e)},Box,,0,0,0,,{{\\an7\\pos({x0},{y0})\\c&HFFFFFF&\\fscy0\\t(150,400,\\fscy100)\\fad(0,250)\\p1}}m 0 0 l {int(8 * u)} 0 {int(8 * u)} {bh} 0 {bh}{{\\p0}}")
            ev(f"Dialogue: 2,{_ass_t(s + 0.2)},{_ass_t(e)},LT,,0,0,0,,{{\\an4\\pos({x0 + int(28 * u)},{y0 + bh // 2})\\c&H101010&\\fad(250,250)\\clip({x0},{y0},{x0 + bw},{y0 + bh})}}{T(txt)}")
        else:
            ev(f"Dialogue: 2,{_ass_t(s)},{_ass_t(e)},Leg,,0,0,0,,{{\\fad(300,300)\\move({W // 2},{H - int(80 * u)},{W // 2},{H - int(110 * u)},0,500)\\an2}}{T(txt)}")
    fim = (plan.get("texto_final") or "").strip()
    if fim:
        s = max(0, total - min(3.2, (bounds[-1] - bounds[-2]) * 0.9))
        ev(f"Dialogue: 2,{_ass_t(s)},{_ass_t(total - 0.3)},Dest,,0,0,0,,{{\\pos({W // 2},{H // 2})\\fad(500,300)\\blur6\\t(0,600,\\blur0)}}{T(fim)}")
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def render(pid, var_idx, kind, fmt, out: Path):
    p = get(pid)
    from .pipeline import preset_of, resolve_kit
    _pid, preset = preset_of(p)
    plan, meta = p["plan"], p["meta"] or {}
    v = plan["variacoes"][var_idx]
    W, H = SIZES.get(fmt, SIZES["16:9"])
    if kind == "preview":
        sc = 540 / min(W, H)
        W, H = int(W * sc) // 2 * 2, int(H * sc) // 2 * 2
    bounds = plan["_tempos"]
    total = bounds[-1]
    infos = {f["i"]: f for f in meta["fotos"]}
    work = Path(p["work_dir"])
    platedir = work / f"placas_{fmt.replace(':', 'x')}_{kind}"
    platedir.mkdir(parents=True, exist_ok=True)
    inten = v.get("intensidade_movimento", 1.0)
    inputs, filt = [], []
    n = len(plan["cenas"])
    trans = []
    for k, c in enumerate(plan["cenas"]):
        t = c["transicao"] if v.get("transicao_padrao") in (None, "do_plano") or c["transicao"] == "corte_seco" else v["transicao_padrao"]
        trans.append(t)
    durs = []
    for k in range(n):
        slot = bounds[k + 1] - bounds[k]
        td = 0 if k + 1 >= n else _tdur(trans[k + 1], bounds[k + 2] - bounds[k + 1] if k + 2 <= n else 1, slot)
        durs.append((slot + td, td))
    # cenas desenhadas quadro a quadro (movimento subpixel, várias fotos por cena), em paralelo
    beats = _beats_of(meta, plan)
    pulse = inten >= 1.2 or "flash_batida" in (v.get("efeitos") or [])
    clipdir = work / f"cenas_{fmt.replace(':', 'x')}_{kind}"
    clipdir.mkdir(parents=True, exist_ok=True)
    sjobs = []
    for k, c in enumerate(plan["cenas"]):
        d, _td = durs[k]
        if not c.get("fotos"):  # plano da versão antiga: uma foto por cena
            c["fotos"], c["layout"] = [c["foto"]], "unica"
        c.setdefault("layout", scenes.layout_for_count(len(c["fotos"])))
        spec = {**{kk: c.get(kk) for kk in ("layout", "fotos", "movimento", "foco_x", "foco_y")}, "_platedir": platedir}
        rel = [round(b - bounds[k], 3) for b in beats if bounds[k] <= b < bounds[k] + d]
        key = scenes.scene_key(spec, W, H, d, rel, inten, pulse, kind, "letterbox" in (v.get("efeitos") or []) and c["layout"] in ("unica", "rajada"))
        lb = "letterbox" in (v.get("efeitos") or []) and c["layout"] in ("unica", "rajada")
        sjobs.append({"letterbox": lb, "spec": spec, "infos": [infos[i] for i in c["fotos"]], "W": W, "H": H, "dur": d, "fps": FPS, "beats": rel,
                      "inten": inten, "pulse": pulse, "kind": kind, "ffmpeg": ff.FFMPEG(), "seed": c["fotos"][0] * 7 + k,
                      "out": str(clipdir / f"c{k:02d}_{key}.mp4")})
    t0 = time.time()
    clips = _render_scenes(pid, sjobs, kind)
    eventlog.info(f"{n} cenas desenhadas em {time.time() - t0:.0f} s ({_layout_summary(plan)})", kind="fotos", ref=pid)
    for k, clip in enumerate(clips):
        d, _td = durs[k]
        inputs += ["-i", clip]
        filt.append(f"[{k}:v]trim=duration={d:.3f},setpts=PTS-STARTPTS,fps={FPS},format=yuv420p,setsar=1[c{k}]")
    cur = "[c0]"
    for k in range(1, n):
        td = durs[k - 1][1]
        tname = {"corte_seco": "fade"}.get(trans[k], trans[k])
        filt.append(f"{cur}[c{k}]xfade=transition={tname}:duration={td:.3f}:offset={bounds[k]:.3f}[x{k}]")
        cur = f"[x{k}]"
    ass = work / f"textos_{fmt.replace(':', 'x')}_{kind}_v{var_idx}.ass"
    _ass(ass, W, H, plan, bounds, preset, v)
    # LUT a 70%: dá o clima sem esmagar pretos nem estourar as cores
    filt.append(f"{cur}split[lo][lb];[lb]lut3d=file='{ff.esc_filter_path(luts.path_for(v['lut']))}':interp=tetrahedral[lg];"
                f"[lo][lg]blend=all_mode=normal:all_opacity=0.7[graded]")
    cur = "[graded]"
    vf = []
    ef = set(v.get("efeitos") or [])
    if "luz_quente" in ef:
        vf.append("colorbalance=rh=0.05:gh=0.02:bh=-0.04")
    if "vinheta" in ef:
        vf.append("vignette=angle=PI/7")
    # grão de filme: leve sempre (disfarça fotos ampliadas/comprimidas), mais forte com o efeito "granulado"
    vf.append(f"noise=alls={7 if 'granulado' in ef else 3}:allf=t")
    if "flash_batida" in ef:
        drops = [b for b in (meta.get("musica") or {}).get("picos", []) if b < total][:8]
        for b in drops:
            vf.append(f"eq=brightness=0.25:enable='between(t,{b:.2f},{b + 0.08:.2f})'")
    vf.append("fade=t=in:st=0:d=0.8")
    vf.append(f"fade=t=out:st={max(0, total - 1.2):.2f}:d=1.2")
    if ff.has_filter("subtitles"):
        vf.append(f"subtitles=filename='{ff.esc_filter_path(ass)}'")
    else:
        _warn(pid, "Textos e títulos desligados: o FFmpeg deste Mac não tem libass. Rode o instalador novo do CorteX9 para ativá-los.")
    filt.append(f"{cur}{','.join(vf)}[vmain]")
    vcur = "[vmain]"
    idx = n
    kit = resolve_kit(preset)
    if kit.get("logo"):
        inputs += ["-loop", "1", "-i", kit["logo"]]
        lw = int(min(W, H) * 0.14)
        filt.append(f"[{idx}:v]scale={lw}:-1,format=rgba,colorchannelmixer=aa=0.85[logo]")
        filt.append(f"{vcur}[logo]overlay=W-w-{int(W * .03)}:{int(H * .04)}:shortest=1[vl]")
        vcur, idx = "[vl]", idx + 1
    acur = None
    trilha = meta.get("trilha")
    if trilha and Path(trilha).exists():
        inputs += ["-i", trilha]
        filt.append(f"[{idx}:a]aresample=48000,aformat=channel_layouts=stereo,atrim=0:{total:.3f},asetpts=PTS-STARTPTS,"
                    f"afade=t=in:st=0:d=0.3,afade=t=out:st={max(0, total - 2.5):.3f}:d=2.5,loudnorm=I=-14:TP=-1.5[aout]")
        acur, idx = "[aout]", idx + 1
    script = work / f"fotos_{fmt.replace(':', 'x')}_{kind}_v{var_idx}.filtergraph"
    script.write_text(";\n".join(filt))
    out.parent.mkdir(parents=True, exist_ok=True)
    common = [*inputs, *ff.filter_script_args(script), "-map", vcur, *(["-map", acur] if acur else []),
              "-t", f"{total:.3f}", "-r", str(FPS), "-pix_fmt", "yuv420p", *(ff.audio_args(kind) if acur else []), "-movflags", "+faststart"]
    # limite de tempo: um render travado vira erro explicado no Log em vez de "Gerando prévias" para sempre
    limit = max(600, total * (20 if kind == "preview" else 60) * max(1, len(plan["cenas"]) / 20))
    step = f"vídeo de fotos ({'prévia' if kind == 'preview' else 'final'} {fmt}, V{var_idx + 1})"
    eventlog.info(f"Renderizando {W}x{H}, {n} cenas, {fmt_t(total)}" + (" com trilha" if acur else " sem trilha"), kind="fotos", ref=pid, step=step)
    try:
        ff.run([*common, *ff.video_args(kind), str(out)], step=step, timeout=limit)
    except Exception as e:  # noqa: BLE001
        if not ff.hw_available() or "Unrecognized option" in str(getattr(e, "detail", "")) or "No such filter" in str(getattr(e, "detail", "")):
            raise
        eventlog.warn("O render com VideoToolbox falhou: tentando de novo com encoder de software.", kind="fotos", ref=pid, step=step)
        ff.run([*common, *ff.video_args(kind, force_software=True), str(out)], step=step, timeout=limit * 2)
    return {"path": str(out), "duration": total}


def _beats_of(meta, plan):
    m = meta.get("musica") or {}
    if m.get("batidas"):
        return m["batidas"]
    bpm = m.get("bpm") or 120
    total = (plan.get("_tempos") or [0, 60])[-1]
    return [round(i * 60 / bpm, 3) for i in range(int(total * bpm / 60) + 2)]


def _layout_summary(plan):
    from collections import Counter
    c = Counter(x.get("layout", "unica") for x in plan["cenas"])
    return ", ".join(f"{n}× {k}" for k, n in c.most_common())


def _render_scenes(pid, sjobs, kind):
    import os
    from concurrent.futures import ThreadPoolExecutor
    ref = eventlog.current()

    def one(j):
        with eventlog.context(*ref):
            try:
                return scenes.render_scene(j)
            except Exception as e:  # noqa: BLE001
                raise StepError("cena do vídeo", f"A cena {j['spec'].get('layout')} não renderizou.",
                                "Clique em Tentar de novo. Se repetir, mande o log.", f"{type(e).__name__}: {e}") from e
    workers = max(1, min(4, (os.cpu_count() or 4) // 2))
    with ThreadPoolExecutor(workers) as ex:
        return list(ex.map(one, sjobs))


def _tdur(t, next_slot, slot):
    if t == "corte_seco":
        return 0.04
    return round(max(0.2, min(0.9, slot * 0.35, next_slot * 0.35)), 3)


# ------------------------------------------------------------------ jobs
def preview(pid, var_idx):
    p = get(pid)
    vs = p["variations"]
    vs[var_idx].update(status="renderizando", erro=None)
    _set(pid, variations=vs)
    opts = {**DEFAULTS, **(p["options"] or {})}
    out = Path(p["work_dir"]) / "previas" / f"fotos_v{var_idx + 1}.mp4"
    try:
        res = render(pid, var_idx, "preview", opts["formatos"][0], out)
        thumb = out.with_suffix(".jpg")
        ff.run(["-ss", f"{min(2.0, res['duration'] / 3):.2f}", "-i", str(out), "-frames:v", "1", "-vf", "scale=480:-2", "-q:v", "4", str(thumb)], step="miniatura")
        vs = get(pid)["variations"]
        vs[var_idx].update(status="pronta", video=str(out), thumb=str(thumb), duracao=round(res["duration"], 1))
    except Exception as e:  # noqa: BLE001
        vs = get(pid)["variations"]
        vs[var_idx].update(status="erro", erro=explain("prévia do vídeo de fotos", e).as_dict())
        _set(pid, variations=vs)
        _check_previews(pid)
        raise
    _set(pid, variations=vs)
    _check_previews(pid)


def _check_previews(pid):
    p = get(pid)
    vs = p["variations"] or []
    done = sum(1 for v in vs if v["status"] in ("pronta", "erro"))
    if done < len(vs):
        _set(pid, progress=0.55 + 0.45 * done / max(1, len(vs)), stage=f"Gerando prévias ({done}/{len(vs)})")
        return
    if p["status"] != "gerando_previas":
        return
    ok = sum(1 for v in vs if v["status"] == "pronta")
    mins = (time.time() - p["created_at"]) / 60
    first_err = next((v.get("erro") for v in vs if v.get("erro")), None)
    _set(pid, status="aguardando_aprovacao" if ok else "erro", stage="Prévias prontas para aprovação" if ok else "Nenhuma prévia renderizou",
         progress=1.0, previews_ready_at=db.now(), error=None if ok else json.dumps(first_err, ensure_ascii=False) if first_err else None)
    if ok:
        eventlog.info(f"{ok} de {len(vs)} prévias prontas (levou {mins:.0f} min)", kind="fotos", ref=pid)
    else:
        eventlog.error("Nenhuma prévia renderizou" + (f": {first_err.get('motivo', '').rstrip('.')}" if first_err else "") + ". O comando do FFmpeg e a resposta dele estão no erro logo abaixo.",
                       kind="fotos", ref=pid)
    if ok:
        notify.send("CorteX9 — vídeo de fotos", f"{p['title']}: {ok} versões prontas para aprovar (levou {mins:.0f} min).")


def choose(pid, var_idx):
    _set(pid, chosen=var_idx)


def approve(pid, var_idx=None):
    p = get(pid)
    if var_idx is None:
        var_idx = p["chosen"] if p["chosen"] is not None else next((i for i, v in enumerate(p["variations"]) if v["status"] == "pronta"), None)
    if var_idx is None or p["variations"][var_idx]["status"] != "pronta":
        raise ValueError("Escolha uma versão com prévia pronta.")
    _set(pid, chosen=var_idx, status="renderizando_final", stage="Na fila do render final", error=None)
    _record(p, var_idx, "aprovado")
    from . import jobs
    jobs.enqueue("final", "photo_final", payload={"project_id": pid})


def reject(pid, note=""):
    p = get(pid)
    _set(pid, status="reprovado", stage="Reprovado")
    _record(p, 0, "reprovado", note)


def request_adjust(pid, note):
    p = get(pid)
    _record(p, p["chosen"] or 0, "ajuste", note)
    meta = p["meta"] or {}
    meta["ajuste"] = note
    _set(pid, meta=meta, status="ajustando", stage="Ajustando o vídeo", previews_ready_at=None)
    from . import jobs
    jobs.enqueue("analise", "photo_adjust", payload={"project_id": pid})


def adjust(pid):
    p = get(pid)
    meta = p["meta"] or {}
    note = meta.get("ajuste", "")
    plan = {k: v for k, v in p["plan"].items() if not k.startswith("_")}
    infos = meta["fotos"]
    music = analyze_music(Path(meta["trilha"])) if meta.get("trilha") else None
    opts = {**DEFAULTS, **(p["options"] or {})}
    target = p["plan"]["_alvo"]
    m = re.search(r"dura[çc][ãa]o[:\s]+(\d+)", note, re.I)
    if m:
        target = float(m.group(1))
    new = None
    content = [{"type": "text", "text": f"Plano atual:\n{json.dumps(plan, ensure_ascii=False)}\n\nFotos: {json.dumps([{k: f[k] for k in ('i', 'nome', 'data')} for f in infos], ensure_ascii=False)}\n"
                f"Duração alvo: {target:.0f}s. BPM: {music['bpm'] if music else '—'}.\n\nPedido de ajuste do usuário: {note}\n\nDevolva o plano completo ajustado."}]
    for fn in (director._claude, director._openai):
        try:
            new = fn(SYSTEM_FOTOS, content, PLANO_FOTOS, -pid)
            break
        except Exception as e:  # noqa: BLE001
            log.warning("ajuste de fotos via IA falhou: %s", e)
    if new is None:
        new = _local_adjust(plan, note)
        _warn(pid, "Ajuste aplicado localmente (IA indisponível).")
    new = sanitize(new, infos, music, opts)
    new["_tempos"] = timeline(new, music, target)
    new["_alvo"] = target
    _set(pid, plan=new, chosen=None, status="gerando_previas", variations=[{"idx": i + 1, "spec": v, "status": "na_fila"} for i, v in enumerate(new["variacoes"])])
    from . import jobs
    for i in range(len(new["variacoes"])):
        jobs.enqueue("previa", "photo_preview", payload={"project_id": pid, "var": i})


def _local_adjust(plan, note):
    n = note.lower()
    for c in plan["cenas"]:
        if "rápid" in n or "rapid" in n or "dinâm" in n:
            c["batidas"] = max(1, c["batidas"] // 2)
        if "lent" in n or "calm" in n or "suave" in n:
            c["batidas"] = c["batidas"] * 2
            if c["transicao"] == "corte_seco":
                c["transicao"] = "dissolve"
        if "sem texto" in n and c.get("estilo_texto") != "titulo":
            c["texto"], c["estilo_texto"] = "", "nenhum"
    if "lut" in n or "cor" in n:
        plan["lut"] = director._other_lut(plan.get("lut", "epico"))
        for v in plan.get("variacoes", []):
            v["lut"] = director._other_lut(v.get("lut", "epico"))
    m = re.search(r"t[íi]tulo[:\s]+[\"“]?([^\"”\n]+)", note, re.I)
    if m:
        plan["titulo"] = m.group(1).strip()
        plan["cenas"][0].update(texto=plan["titulo"], estilo_texto="titulo")
    return plan


def _record(p, var_idx, decision, note=""):
    plan = p["plan"] or {}
    v = (plan.get("variacoes") or [{}])[var_idx] if plan.get("variacoes") else {}
    db.insert("style_library", preset_id=p["preset_id"], cut_id=None, decision=decision, hook=f"[fotos] {plan.get('titulo', '')}",
              duration=(plan.get("_tempos") or [0])[-1], music_mood=(p["options"] or {}).get("estilo"), lut=v.get("lut"),
              effects=v.get("efeitos") or [], ai_score=None, experimental=0, note=note, created_at=db.now())


def final(pid):
    p = get(pid)
    from .pipeline import preset_of, safe
    _pid, preset = preset_of(p)
    opts = {**DEFAULTS, **(p["options"] or {})}
    _set(pid, status="renderizando_final", stage="Renderizando o final em alta")
    today = date.today().isoformat()
    outdir = output_dir(p, preset, opts, today)
    eventlog.info(f"Destino no computador: {outdir}", kind="fotos", ref=pid, step="render final")
    finals = []
    for fmt in opts["formatos"]:
        out = outdir / f"{safe(p['title'])}_fotos_{fmt.replace(':', 'x')}.mp4"
        res = render(pid, p["chosen"] or 0, "final", fmt, out)
        finals.append({"formato": fmt, "arquivo": res["path"], "duracao": round(res["duration"], 1)})
        eventlog.info(f"Final salvo: {res['path']}", kind="fotos", ref=pid, step="render final")
    _set(pid, finals=finals, status="concluido", stage="Final pronto", finished_at=db.now(), progress=1.0)
    drive_on = opts.get("enviar_drive", True) and preset.get("arquivar_drive", True)
    if drive_on and (services.get("drive_pasta_local") or services.get("gdrive_service_account")):
        sub = drive_subpath(preset, opts, today)
        services.enqueue("gdrive", "arquivar", {"arquivos": [f["arquivo"] for f in finals], "data": today,
                                                "cliente": safe(preset["cliente"]), "projeto": safe(preset["projeto"]),
                                                "destino": sub, "photo_id": pid})
        _drive_state(pid, "na_fila", f"Enviando para o Drive: {sub}")
        eventlog.info(f"Envio ao Google Drive na fila: {sub}", kind="fotos", ref=pid, step="Drive")
    elif drive_on:
        _drive_state(pid, "nao_configurado", "Google Drive não configurado em Conexões: o final ficou só no computador.")
        _warn(pid, "Google Drive não configurado em Conexões: o final ficou só no computador.")
    if services.get("base44_api_key"):
        services.enqueue("base44", "upsert", {"entidade": "CorteFicha", "chave": "corte_id", "dados": {
            "corte_id": f"fotos{pid}", "cliente": preset["cliente"], "projeto": preset["projeto"], "episodio": p["title"],
            "numero_corte": 0, "titulo": (p["plan"] or {}).get("titulo"), "formatos": [f["formato"] for f in finals],
            "duracao_s": finals[0]["duracao"], "lut": (p["plan"]["variacoes"][p["chosen"] or 0]).get("lut"), "tipo": "video_de_fotos",
            "experimental": False, "link_drive": "", "data": today}})
    notify.send("CorteX9 — vídeo de fotos pronto", f"{p['title']}: final em {', '.join(opts['formatos'])}.")


def output_dir(p, preset, opts, today=None):
    """Pasta do computador onde o final é salvo."""
    from .pipeline import safe
    today = today or date.today().isoformat()
    if opts.get("pasta_saida"):
        return Path(opts["pasta_saida"]).expanduser()
    return Path(preset.get("pasta_saida") or paths.DEFAULT_OUTPUT).expanduser() / today / safe(preset["cliente"]) / safe(preset["projeto"])


def drive_subpath(preset, opts, today=None):
    """Pasta dentro do Google Drive (relativa a 'Meu Drive')."""
    from .pipeline import safe
    today = today or date.today().isoformat()
    custom = (opts.get("pasta_drive") or "").strip().strip("/")
    return custom or f"CorteX9/{today}/{safe(preset['cliente'])}/{safe(preset['projeto'])}"


def _drive_state(pid, estado, msg, links=None):
    meta = get(pid)["meta"] or {}
    meta["drive"] = {"estado": estado, "mensagem": msg, "links": links or [], "em": db.now()}
    _set(pid, meta=meta)


# ------------------------------------------------------------------ pasta vigiada de fotos
def folder_ready(d: Path, _cache={}):
    """Subpasta com fotos que parou de mudar (contagem e tamanhos iguais em 3 leituras)."""
    try:
        sig = tuple(sorted((f.name, f.stat().st_size) for f in d.iterdir() if f.is_file()))
    except OSError:
        return False
    if not any(Path(n).suffix.lower() in IMG_EXTS for n, _ in sig):
        return False
    hist = _cache.setdefault(str(d), [])
    hist.append(sig)
    del hist[:-3]
    return len(hist) == 3 and len(set(hist)) == 1
