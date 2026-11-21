"""Utilitários FFmpeg/ffprobe com aceleração VideoToolbox quando disponível."""
import functools
import json
import logging
import os
import shutil
import subprocess
import sys
import time

from .errors import StepError

log = logging.getLogger("cortex9.ffmpeg")


def _bin(name):
    # ffmpeg-full (Homebrew) primeiro: traz libass (textos/legendas), que o ffmpeg "enxuto" do Homebrew pode não ter.
    for p in (f"/opt/homebrew/opt/ffmpeg-full/bin/{name}", f"/usr/local/opt/ffmpeg-full/bin/{name}",
              shutil.which(name), f"/opt/homebrew/bin/{name}", f"/usr/local/bin/{name}"):
        if p and os.path.exists(p):
            return p
    raise StepError("ffmpeg", f"{name} não encontrado.", "Rode o instalador do CorteX9 de novo (ele instala o FFmpeg pelo Homebrew).")


def FFMPEG():
    return _bin("ffmpeg")


def FFPROBE():
    return _bin("ffprobe")


def _cmdline(cmd):
    import shlex
    return " ".join(shlex.quote(str(c)) for c in cmd)


def run(args, step="ffmpeg", timeout=None):
    from . import eventlog
    from .errors import explain
    cmd = [FFMPEG(), "-hide_banner", "-nostdin", "-y", *map(str, args)]
    log.debug("ffmpeg %s", " ".join(cmd[1:]))
    t0 = time.time()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        err = explain(step, TimeoutError(f"FFmpeg passou de {timeout:.0f} s e foi interrompido (travou)"))
        out = e.stderr or ""
        out = out.decode("utf-8", "replace") if isinstance(out, bytes) else out
        eventlog.error(f"FFmpeg travou e foi interrompido depois de {timeout:.0f} s", step=step,
                       detail=f"Comando:\n{_cmdline(cmd)}\n\nSaída do FFmpeg:\n{out[-6000:]}")
        raise err from None
    if p.returncode != 0:
        tail = p.stderr[-6000:]
        err = explain(step, RuntimeError(p.stderr[-3000:]))
        eventlog.error(f"FFmpeg falhou (código {p.returncode}): {err.reason}", step=step,
                       detail=f"Comando:\n{_cmdline(cmd)}\n\nSaída do FFmpeg (final):\n{tail}")
        raise err
    log.debug("ffmpeg ok em %.1fs", time.time() - t0)
    return p.stderr


def probe(path):
    p = subprocess.run([FFPROBE(), "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
                       capture_output=True, text=True)
    if p.returncode != 0:
        raise StepError("leitura do vídeo", "Não consegui ler o arquivo de vídeo.",
                        "Confira se a cópia terminou e se o arquivo abre no QuickTime.", p.stderr)
    info = json.loads(p.stdout)
    v = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    a = next((s for s in info["streams"] if s["codec_type"] == "audio"), None)
    if not v:
        raise StepError("leitura do vídeo", "O arquivo não tem trilha de vídeo.", "Use o arquivo de gravação do programa (PGM) do YoloBox.")
    num, den = (v.get("avg_frame_rate") or "30/1").split("/")
    fps = float(num) / float(den) if float(den) else 30.0
    rot = 0
    for sd in v.get("side_data_list", []) or []:
        if "rotation" in sd:
            rot = int(sd["rotation"])
    rot = int(v.get("tags", {}).get("rotate", rot))
    return {
        "duration": float(info["format"].get("duration", 0) or 0),
        "width": int(v["width"]), "height": int(v["height"]), "fps": fps if 1 < fps < 121 else 30.0,
        "rotation": rot, "has_audio": a is not None,
    }


@functools.lru_cache
def encoders():
    p = subprocess.run([FFMPEG(), "-hide_banner", "-encoders"], capture_output=True, text=True)
    return {name for name in ("h264_videotoolbox", "hevc_videotoolbox", "libx264", "aac_at") if name in p.stdout}


@functools.lru_cache
def filters():
    p = subprocess.run([FFMPEG(), "-hide_banner", "-filters"], capture_output=True, text=True)
    names = set()
    for line in p.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 3 and "->" in parts[2]:
            names.add(parts[1])
    return names


def has_filter(name):
    try:
        return name in filters()
    except Exception:  # noqa: BLE001
        return False


def capabilities():
    """O que este FFmpeg consegue fazer (vai para o diagnóstico da aba Log)."""
    enc = encoders()
    return {"textos/legendas (libass)": has_filter("subtitles"), "LUT 3D": has_filter("lut3d"), "transições (xfade)": has_filter("xfade"),
            "movimento (zoompan)": has_filter("zoompan"), "loudnorm": has_filter("loudnorm"),
            "VideoToolbox": "h264_videotoolbox" in enc, "libx264": "libx264" in enc}


def hw_available():
    return sys.platform == "darwin" and "h264_videotoolbox" in encoders()


def video_args(kind, force_software=False):
    """kind: 'preview' (540p leve) ou 'final' (alta qualidade)."""
    hw = hw_available() and not force_software
    if kind == "preview":
        if hw:
            return ["-c:v", "h264_videotoolbox", "-b:v", "1200k", "-maxrate", "1600k", "-realtime", "1", "-prio_speed", "1"]
        return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "30", "-maxrate", "1600k", "-bufsize", "3200k"]
    if hw:
        return ["-c:v", "h264_videotoolbox", "-b:v", "16M", "-maxrate", "22M", "-profile:v", "high", "-allow_sw", "1"]
    return ["-c:v", "libx264", "-preset", "slow", "-crf", "17", "-profile:v", "high"]


def audio_args(kind):
    return ["-c:a", "aac", "-b:a", "96k" if kind == "preview" else "256k", "-ar", "48000"]


def rgb_mean(path, t):
    """Média RGB de um quadro (para balanço de branco / igualar câmeras)."""
    p = subprocess.run([FFMPEG(), "-v", "error", "-ss", f"{t:.2f}", "-i", str(path), "-frames:v", "1",
                        "-vf", "scale=64:-2,scale=1:1:flags=area", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                       capture_output=True)
    if len(p.stdout) >= 3:
        return tuple(p.stdout[:3])
    return None


def esc_filter_path(p):
    """Escapa caminho para uso dentro de um filtergraph."""
    return str(p).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'").replace(",", "\\,").replace("[", "\\[").replace("]", "\\]")
