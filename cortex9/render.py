"""Monta e executa o FFmpeg de cada variação (prévia 540p ou final em alta)."""
import json
import logging
import re
import subprocess
from pathlib import Path

from . import ffmpeg as ff
from . import luts

log = logging.getLogger("cortex9.render")

SIZES = {
    "9:16": {"final": (1080, 1920), "preview": (540, 960)},
    "1:1": {"final": (1080, 1080), "preview": (540, 540)},
    "4:5": {"final": (1080, 1350), "preview": (540, 676)},
    "16:9": {"final": (1920, 1080), "preview": (960, 540)},
}
FILLERS = re.compile(r"^(é{2,}|ã{2,}h*|hã+|ãh+|ah+n+|ahn+|hu+m+|hm+|e+h{2,}|u+h+m*|ãn+)$", re.I)  # "um", "é", "né", "tipo" são palavras reais: não removemos
FPS = 30


# ------------------------------------------------------------------ limpeza de respiros e vícios
def _norm(w):
    return re.sub(r"[^\wáéíóúâêôãõç]", "", w.lower())


def cut_words(tr, a, b):
    return [w for s in tr.get("segments", []) for w in s.get("words", []) if w["end"] > a and w["start"] < b]


def keep_ranges(words, a, b, gap=0.45, pad_before=0.07, pad_after=0.10):
    """Remove pausas longas (respiros), vícios ("ééé", "hã") e palavras gaguejadas repetidas."""
    if not words:
        return [(a, b)], []
    clean, removed = [], []
    for i, w in enumerate(words):
        n = _norm(w["w"])
        nxt = _norm(words[i + 1]["w"]) if i + 1 < len(words) else None
        if FILLERS.match(n) or (n and n == nxt and len(n) <= 6 and words[i + 1]["start"] - w["end"] < 0.4):
            removed.append(w)
            continue
        clean.append(w)
    if not clean:
        return [(a, b)], []
    ranges = []
    cs = max(a, clean[0]["start"] - pad_before)
    ce = min(b, clean[0]["end"] + pad_after)
    for w in clean[1:]:
        ws, we = max(a, w["start"] - pad_before), min(b, w["end"] + pad_after)
        if ws - ce > gap - (pad_before + pad_after) or any(r["start"] >= ce and r["end"] <= ws + 0.2 for r in removed):
            ranges.append((cs, ce))
            cs = ws
        ce = max(ce, we)
    ranges.append((cs, ce))
    merged = []
    for r in ranges:
        if merged and r[0] - merged[-1][1] < 0.05:
            merged[-1] = (merged[-1][0], r[1])
        elif r[1] - r[0] >= 0.2:
            merged.append(r)
    return merged or [(a, b)], removed


def silence_ranges(src, a, b):
    """Sem transcrição: usa detecção de silêncio do FFmpeg."""
    p = subprocess.run([ff.FFMPEG(), "-hide_banner", "-ss", f"{a}", "-t", f"{b - a}", "-i", str(src), "-vn",
                        "-af", "silencedetect=n=-35dB:d=0.6", "-f", "null", "-"], capture_output=True, text=True)
    sil = []
    st = None
    for line in p.stderr.splitlines():
        m = re.search(r"silence_start: ([\d.]+)", line)
        if m:
            st = float(m.group(1))
        m = re.search(r"silence_end: ([\d.]+)", line)
        if m and st is not None:
            sil.append((a + st + 0.15, a + float(m.group(1)) - 0.15))
            st = None
    out, cur = [], a
    for s, e in sil:
        if s > cur + 0.2:
            out.append((cur, s))
        cur = max(cur, e)
    if b > cur + 0.2:
        out.append((cur, b))
    return out or [(a, b)]


class Timeline:
    """Mapeia tempo absoluto do episódio → tempo no corte montado."""

    def __init__(self, prefix, body):
        self.prefix, self.body = prefix, body
        self.prefix_len = sum(e - s for s, e in prefix)
        self.length = self.prefix_len + sum(e - s for s, e in body)

    def map(self, t):
        acc = self.prefix_len
        for s, e in self.body:
            if t < s:
                return acc
            if t <= e:
                return acc + (t - s)
            acc += e - s
        return acc

    def contains(self, t):
        return any(s <= t <= e for s, e in self.body)


def cold_open_range(words, a, b):
    """Trecho de ~3,5s de clímax para abrir o corte (corte criativo)."""
    if not words:
        mid = a + (b - a) * 0.66
        return (mid, min(b, mid + 3.5))
    best, best_score = None, -1
    for i, w in enumerate(words):
        if w["start"] < a + (b - a) * 0.4:
            continue
        j = i
        while j < len(words) and words[j]["end"] - w["start"] < 3.5:
            j += 1
        chunk = words[i:j]
        text = " ".join(x["w"] for x in chunk)
        score = len(chunk) + 4 * text.count("!") + 3 * text.count("?")
        if score > best_score:
            best, best_score = (w["start"], min(b, chunk[-1]["end"] + 0.1)), score
    return best or (a, min(b, a + 3.5))


# ------------------------------------------------------------------ legendas ASS
def _ass_color(hexc, alpha="00"):
    h = hexc.lstrip("#")
    if len(h) != 6:
        h = "FFFFFF"
    return f"&H{alpha}{h[4:6]}{h[2:4]}{h[0:2]}".upper()


def _ass_t(t):
    t = max(0, t)
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def _esc(s):
    return s.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def build_ass(path, W, H, style, captions, hook, textos, tl, L):
    cs = style or {}
    font = cs.get("fonte", "Arial Black")
    size = int(cs.get("tamanho", 72) * H / 1920 * (1.0 if H >= W else 1.35))
    col, hi = _ass_color(cs.get("cor", "#FFFFFF")), _ass_color(cs.get("destaque", "#FFD400"))
    outline = max(1, int(cs.get("contorno", 5) * H / 1920))
    pos = cs.get("posicao", "inferior")
    align = {"inferior": 2, "meio": 5, "superior": 8}.get(pos, 2)
    margin_v = int(H * (0.18 if pos == "inferior" else 0.08))
    upper = cs.get("maiusculas", True)
    hook_size = int(size * 1.25)
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}", "WrapStyle: 0", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Leg,{font},{size},{col},{hi},&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,{outline},2,{align},{int(W * .06)},{int(W * .06)},{margin_v},1",
        f"Style: Hook,{font},{hook_size},&H00FFFFFF,&H00FFFFFF,&H00000000,{_ass_color(cs.get('destaque', '#FFD400'), '20')},-1,0,0,0,100,100,0,0,3,{outline + 4},0,8,{int(W * .07)},{int(W * .07)},{int(H * .12)},1",
        f"Style: Txt,{font},{int(size * 1.1)},{hi},&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,{outline},2,5,{int(W * .08)},{int(W * .08)},0,1",
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    hook_end = 0
    if hook:
        hook_end = min(3.2, L)
        txt = _esc(hook.upper() if upper else hook)
        lines.append(f"Dialogue: 2,{_ass_t(0.05)},{_ass_t(hook_end)},Hook,,0,0,0,,{{\\fad(80,200)\\fscx40\\fscy40\\t(0,180,\\fscx108\\fscy108)\\t(180,260,\\fscx100\\fscy100)}}{txt}")
    for chunk in captions:
        ws = [(w, tl.map(w["start"]), tl.map(w["end"])) for w in chunk if tl.contains(w["start"]) or tl.contains(w["end"])]
        if not ws:
            continue
        cstart, cend = ws[0][1], max(ws[-1][2], ws[0][1] + 0.3)
        if cend <= hook_end:
            continue
        for i, (w, ws_t, we_t) in enumerate(ws):
            s = max(ws_t, cstart, hook_end)
            e = ws[i + 1][1] if i + 1 < len(ws) else cend
            if e - s < 0.04:
                continue
            parts = []
            for j, (w2, _a, _b) in enumerate(ws):
                t = _esc(w2["w"].upper() if upper else w2["w"])
                parts.append(f"{{\\c{hi}\\fscx108\\fscy108}}{t}{{\\c{col}\\fscx100\\fscy100}}" if j == i else t)
            lines.append(f"Dialogue: 0,{_ass_t(s)},{_ass_t(e)},Leg,,0,0,0,,{' '.join(parts)}")
    for x in textos or []:
        s, e = x["inicio"], x["fim"]
        t = _esc(x["texto"].upper() if upper else x["texto"])
        anim = {"pop": "\\fscx30\\fscy30\\t(0,160,\\fscx100\\fscy100)\\fad(0,150)",
                "deslizar": f"\\move({-W // 2},{H // 2},{W // 2},{H // 2},0,250)\\fad(0,150)",
                "digitar": "\\fad(250,150)\\fsp8\\t(0,400,\\fsp0)", "fade": "\\fad(300,300)"}.get(x.get("animacao"), "\\fad(200,200)")
        lines.append(f"Dialogue: 1,{_ass_t(s)},{_ass_t(e)},Txt,,0,0,0,,{{{anim}}}{t}")
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def caption_chunks(words, per_line=3):
    chunks, cur = [], []
    for w in words:
        if FILLERS.match(_norm(w["w"])):
            continue
        cur.append(w)
        if len(cur) >= per_line or re.search(r"[.!?,]$", w["w"]):
            chunks.append(cur)
            cur = []
    if cur:
        chunks.append(cur)
    return chunks


# ------------------------------------------------------------------ cor automática
def wb_gains(src, times, rotation_filter=""):
    """Balanço de branco pelo mundo cinza (média dos quadros do corte)."""
    vals = [v for v in (ff.rgb_mean(src, t) for t in times) if v]
    if not vals:
        return 1.0, 1.0, 1.0
    r = sum(v[0] for v in vals) / len(vals)
    g = sum(v[1] for v in vals) / len(vals)
    b = sum(v[2] for v in vals) / len(vals)
    gray = (r + g + b) / 3 or 1

    def k(c):
        return max(0.85, min(1.18, 1 + (gray / max(c, 1) - 1) * 0.7))  # correção parcial, natural
    return round(k(r), 3), round(k(g), 3), round(k(b), 3)


# ------------------------------------------------------------------ montagem do FFmpeg
def build_and_run(job):
    """job: dict com src, meta, a, b, ranges, prefix, kind, fmt, out, lut, efeitos, zooms (abs), textos (rel),
    hook, captions, style, music, music_in, music_out, logo, logo_pos, intro, outro, voice_wav, rotation, wb, work."""
    kind = job["kind"]
    W, H = SIZES.get(job["fmt"], SIZES["9:16"])[kind]
    src, a, b = job["src"], job["a"], job["b"]
    off = max(0.0, a - 1.0)  # início do trecho lido do arquivo
    tl = Timeline(job["prefix"], job["ranges"])
    L = tl.length
    work = Path(job["work"])
    ass = work / f"{Path(job['out']).stem}.ass"
    build_ass(ass, W, H, job["style"], job["captions"], job["hook"], job["textos"], tl, L)

    inputs = ["-ss", f"{off:.3f}", "-t", f"{b - off + 1:.3f}", "-i", str(src)]
    idx = 1
    voice_in = "0:a"
    if job.get("voice_wav"):
        inputs += ["-ss", f"{off:.3f}", "-t", f"{b - off + 1:.3f}", "-i", str(job["voice_wav"])]
        voice_in = f"{idx}:a"
        idx += 1
    music_idx = logo_idx = intro_idx = outro_idx = None
    if job.get("music"):
        inputs += ["-stream_loop", "-1", "-i", str(job["music"])]
        music_idx, idx = idx, idx + 1
    if job.get("logo"):
        inputs += ["-loop", "1", "-i", str(job["logo"])]
        logo_idx, idx = idx, idx + 1
    for key in ("intro", "outro"):
        if job.get(key):
            inputs += ["-i", str(job[key])]
            if key == "intro":
                intro_idx = idx
            else:
                outro_idx = idx
            idx += 1

    f = []
    # corpo: select dos trechos mantidos (tempos relativos ao offset)
    sel = "+".join(f"between(t,{s - off:.3f},{e - off:.3f})" for s, e in job["ranges"])
    nparts = 2 if job["prefix"] else 1
    if nparts == 2:
        f.append("[0:v]split=2[vb0][vp0]")
        f.append(f"[{voice_in}]asplit=2[ab0][ap0]")
        vsrc, asrc = "[vb0]", "[ab0]"
    else:
        vsrc, asrc = "[0:v]", f"[{voice_in}]"
    f.append(f"{vsrc}select='{sel}',setpts=N/FRAME_RATE/TB,fps={FPS}[vbody]")
    f.append(f"{asrc}aselect='{sel}',asetpts=N/SR/TB,aresample=48000[abody]")
    if nparts == 2:
        ps, pe = job["prefix"][0]
        f.append(f"[vp0]trim=start={ps - off:.3f}:end={pe - off:.3f},setpts=PTS-STARTPTS,fps={FPS}[vpre]")
        f.append(f"[ap0]atrim=start={ps - off:.3f}:end={pe - off:.3f},asetpts=PTS-STARTPTS,aresample=48000[apre]")
        f.append("[vpre][apre][vbody][abody]concat=n=2:v=1:a=1[vcat][acat]")
        vcur, acur = "[vcat]", "[acat]"
    else:
        vcur, acur = "[vbody]", "[abody]"

    # vídeo: rotação YoloBox → cor automática → LUT → enquadramento → zoom → efeitos → legendas → logo
    vf = []
    rot = int(job.get("rotation", 0)) % 360
    vf += {90: ["transpose=1"], 180: ["hflip", "vflip"], 270: ["transpose=2"]}.get(rot, [])
    rr, gg, bb = job.get("wb", (1, 1, 1))
    vf.append(f"colorchannelmixer=rr={rr}:gg={gg}:bb={bb}")
    vf.append("normalize=blackpt=black:whitept=white:smoothing=48:independence=0:strength=0.55")  # exposição/contraste e iguala câmeras
    vf.append(f"lut3d=file='{ff.esc_filter_path(luts.path_for(job['lut']))}':interp=tetrahedral")
    ar = W / H
    vf.append(f"crop=w='min(iw,ih*{ar:.5f})':h='min(ih,iw/{ar:.5f})'")
    src_h = job["meta"]["height"] if rot in (0, 180) else job["meta"]["width"]
    upscale = kind == "final" and src_h < H * 0.98
    vf.append(f"scale={W}:{H}:flags={'lanczos' if kind == 'final' else 'fast_bilinear'},setsar=1")
    if upscale:
        vf.append("unsharp=5:5:0.55:5:5:0")  # upscale leve
    zexpr = _zoom_expr(job, tl, L)
    if zexpr != "1":
        vf.append(f"zoompan=z='{zexpr}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={W}x{H}:fps={FPS}")
    ef = set(job.get("efeitos") or [])
    if "vinheta" in ef:
        vf.append("vignette=angle=PI/5")
    if "granulado" in ef:
        vf.append("noise=alls=7:allf=t")
    if "letterbox" in ef:
        vf.append(f"drawbox=x=0:y=0:w=iw:h={int(H * .09)}:color=black:t=fill,drawbox=x=0:y={H - int(H * .09)}:w=iw:h={int(H * .09)}:color=black:t=fill")
    if "glitch_entrada" in ef or job.get("trans_in") == "glitch":
        vf.append("rgbashift=rh=10:bh=-10:gv=4:enable='lt(t,0.45)'")
    if "flash_entrada" in ef or job.get("trans_in") == "flash":
        vf.append("fade=t=in:st=0:d=0.3:color=white")
    elif job.get("trans_in") == "fade_preto":
        vf.append("fade=t=in:st=0:d=0.4")
    if "fade_saida" in ef or job.get("trans_out") == "fade_preto":
        vf.append(f"fade=t=out:st={max(0, L - 0.5):.2f}:d=0.5")
    vf.append(f"subtitles=filename='{ff.esc_filter_path(ass)}'" + (f":fontsdir='{ff.esc_filter_path(job['fontsdir'])}'" if job.get("fontsdir") else ""))
    f.append(f"{vcur}{','.join(vf)}[vmain]")
    vcur = "[vmain]"
    if logo_idx is not None:
        lw = int(W * 0.16)
        x, y = {"superior-direita": (f"W-w-{int(W * .04)}", f"{int(H * .04)}"), "superior-esquerda": (f"{int(W * .04)}", f"{int(H * .04)}"),
                "inferior-direita": (f"W-w-{int(W * .04)}", f"H-h-{int(H * .04)}"), "inferior-esquerda": (f"{int(W * .04)}", f"H-h-{int(H * .04)}")}.get(job.get("logo_pos"), (f"W-w-{int(W * .04)}", f"{int(H * .04)}"))
        f.append(f"[{logo_idx}:v]scale={lw}:-1,format=rgba,colorchannelmixer=aa=0.9[logo]")
        f.append(f"{vcur}[logo]overlay={x}:{y}:shortest=1[vlogo]")
        vcur = "[vlogo]"

    # áudio: tratamento de voz → trilha com ducking (sidechain)
    af = "highpass=f=80,lowpass=f=15000,afftdn=nf=-25,acompressor=threshold=-18dB:ratio=3:attack=5:release=80:makeup=2,aformat=channel_layouts=stereo"
    f.append(f"{acur}{af}[voz]")
    acur = "[voz]"
    if music_idx is not None:
        mi, mo = job.get("music_in", 0.0), min(L, job.get("music_out", L) or L)
        mlen = max(0.5, mo - mi)
        f.append("[voz]asplit=2[voz1][vozsc]")
        f.append(f"[{music_idx}:a]aresample=48000,aformat=channel_layouts=stereo,atrim=0:{mlen:.3f},asetpts=PTS-STARTPTS,"
                 f"afade=t=in:st=0:d=1.2,afade=t=out:st={max(0, mlen - 1.8):.3f}:d=1.8,volume=0.22,adelay={int(mi * 1000)}|{int(mi * 1000)},apad[mus]")
        f.append("[mus][vozsc]sidechaincompress=threshold=0.04:ratio=8:attack=15:release=350[musd]")
        f.append("[voz1][musd]amix=inputs=2:duration=first:normalize=0[mix]")
        acur = "[mix]"
    f.append(f"{acur}loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[amain]")
    acur = "[amain]"

    # abertura/encerramento
    segs_v, segs_a = [], []
    for key, ii in (("intro", intro_idx), ("outro", outro_idx)):
        if ii is None:
            continue
        has_a = job.get(f"{key}_has_audio", True)
        f.append(f"[{ii}:v]scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={FPS},format=yuv420p[{key}v]")
        if has_a:
            f.append(f"[{ii}:a]aresample=48000,aformat=channel_layouts=stereo[{key}a]")
        else:
            f.append(f"anullsrc=r=48000:cl=stereo,atrim=0:{job.get(key + '_dur', 3):.3f}[{key}a]")
    order = (["intro"] if intro_idx is not None else []) + ["main"] + (["outro"] if outro_idx is not None else [])
    if len(order) > 1:
        f.append(f"{vcur}format=yuv420p[mainv]")
        pairs = "".join(f"[{o}v][{o}a]" if o != "main" else f"[mainv]{acur}" for o in order)
        f.append(f"{pairs}concat=n={len(order)}:v=1:a=1[vout][aout]")
        vcur, acur = "[vout]", "[aout]"
        total = L + sum(job.get(f"{o}_dur", 0) for o in order if o != "main")
    else:
        total = L

    graph = ";\n".join(f)
    script = work / f"{Path(job['out']).stem}.filtergraph"
    script.write_text(graph)
    out = Path(job["out"])
    out.parent.mkdir(parents=True, exist_ok=True)
    common = [*inputs, *ff.filter_script_args(script), "-map", vcur, "-map", acur, "-t", f"{total + 0.05:.3f}",
              "-pix_fmt", "yuv420p", *ff.audio_args(kind), "-movflags", "+faststart"]
    try:
        ff.run([*common, *ff.video_args(kind), str(out)], step=f"render {kind}")
    except Exception as e:  # noqa: BLE001
        if ff.hw_available():  # VideoToolbox falhou → software
            log.warning("VideoToolbox falhou, tentando software: %s", e)
            ff.run([*common, *ff.video_args(kind, force_software=True), str(out)], step=f"render {kind}")
        else:
            raise
    return {"path": str(out), "duration": total, "size": (W, H), "intro": job.get("intro_dur", 0) if intro_idx is not None else 0}


def _zoom_expr(job, tl, L):
    parts = []
    for z in job.get("zooms") or []:
        s, e, k = tl.map(z["abs_inicio"]), tl.map(z["abs_fim"]), z["intensidade"] - 1
        if e - s < 0.3:
            continue
        ramp = min(0.25, (e - s) / 3)
        parts.append(f"if(between(it,{s:.2f},{e:.2f}),{k:.3f}*min(1,min((it-{s:.2f})/{ramp:.2f},({e:.2f}-it)/{ramp:.2f})),0)")
    ef = set(job.get("efeitos") or [])
    if "zoom_lento" in ef:
        parts.append(f"0.08*it/{max(L, 1):.2f}")
    if not parts or ("zoom_punch" not in ef and "zoom_lento" not in ef and not job.get("zooms")):
        return "1"
    return "1+" + "+".join(parts)


def thumbnail(video, out, t=1.0):
    ff.run(["-ss", f"{t}", "-i", str(video), "-frames:v", "1", "-vf", "scale=360:-2", "-q:v", "4", str(out)], step="miniatura")
    return out


def dump(obj):
    return json.dumps(obj, ensure_ascii=False, indent=1)
