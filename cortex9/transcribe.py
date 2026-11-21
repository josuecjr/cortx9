"""Transcrição local com Whisper (MLX no Apple Silicon; faster-whisper como reserva)."""
import json
import logging
import platform
import sys
from pathlib import Path

from . import db
from .errors import StepError

log = logging.getLogger("cortex9.transcribe")

MLX_REPOS = {
    "large-v3-turbo": "mlx-community/whisper-large-v3-turbo",
    "medium": "mlx-community/whisper-medium-mlx",
    "small": "mlx-community/whisper-small-mlx",
}


def backend_name():
    if sys.platform == "darwin" and platform.machine() == "arm64":
        try:
            import mlx_whisper  # noqa: F401
            return "mlx-whisper (Apple Silicon)"
        except ImportError:
            pass
    try:
        import faster_whisper  # noqa: F401
        return "faster-whisper"
    except ImportError:
        pass
    try:
        import whisper  # noqa: F401
        return "openai-whisper"
    except ImportError:
        return None


def transcribe(wav: Path, language="pt") -> dict:
    model = db.setting("whisper_modelo", "large-v3-turbo")
    name = backend_name()
    if not name:
        raise StepError("transcrição", "Nenhum Whisper instalado.",
                        "Rode o instalador do CorteX9 de novo; ele instala o mlx-whisper para Apple Silicon.")
    log.info("Transcrevendo com %s (%s)", name, model)
    if name.startswith("mlx"):
        import mlx_whisper
        res = mlx_whisper.transcribe(str(wav), path_or_hf_repo=MLX_REPOS.get(model, model),
                                     language=language, word_timestamps=True,
                                     condition_on_previous_text=False)
        segs = [{"start": s["start"], "end": s["end"], "text": s["text"].strip(),
                 "words": [{"w": w["word"].strip(), "start": w["start"], "end": w["end"]} for w in s.get("words", [])]}
                for s in res["segments"]]
    elif name == "faster-whisper":
        from faster_whisper import WhisperModel
        m = WhisperModel(model, device="auto", compute_type="int8")
        it, _info = m.transcribe(str(wav), language=language, word_timestamps=True, vad_filter=True)
        segs = [{"start": s.start, "end": s.end, "text": s.text.strip(),
                 "words": [{"w": w.word.strip(), "start": w.start, "end": w.end} for w in (s.words or [])]} for s in it]
    else:
        import whisper
        m = whisper.load_model(model)
        res = m.transcribe(str(wav), language=language, word_timestamps=True)
        segs = [{"start": s["start"], "end": s["end"], "text": s["text"].strip(),
                 "words": [{"w": w["word"].strip(), "start": w["start"], "end": w["end"]} for w in s.get("words", [])]}
                for s in res["segments"]]
    segs = [s for s in segs if s["text"]]
    for s in segs:  # garante palavras mesmo sem timestamps por palavra
        if not s["words"]:
            toks = s["text"].split()
            dur = (s["end"] - s["start"]) / max(1, len(toks))
            s["words"] = [{"w": t, "start": s["start"] + i * dur, "end": s["start"] + (i + 1) * dur} for i, t in enumerate(toks)]
    return {"backend": name, "model": model, "language": language, "segments": segs}


def load(path: Path):
    return json.loads(path.read_text()) if path.exists() else None
