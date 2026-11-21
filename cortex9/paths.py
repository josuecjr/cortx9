"""Onde o CorteX9 guarda dados, prévias e finais."""
import os
import sys
from pathlib import Path


def _base() -> Path:
    env = os.environ.get("CORTEX9_HOME")
    if env:
        return Path(env).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "CorteX9"
    return Path.home() / ".cortex9"


BASE = _base()
DB_PATH = BASE / "cortex9.db"
WORK = BASE / "episodios"          # pastas de trabalho de cada episódio
LUTS = BASE / "luts"               # biblioteca de LUTs prontas
LOGS = BASE / "logs"
MOVIES = Path(os.environ.get("CORTEX9_MOVIES", Path.home() / "Movies" / "CorteX9")).expanduser()
DEFAULT_INBOX = MOVIES / "Entrada"
DEFAULT_OUTPUT = MOVIES / "Finais"

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".mts", ".avi"}


def ensure():
    for p in (BASE, WORK, LUTS, LOGS, DEFAULT_INBOX, DEFAULT_OUTPUT):
        p.mkdir(parents=True, exist_ok=True)
