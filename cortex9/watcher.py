"""Pasta vigiada: quando um vídeo novo termina de ser copiado, o episódio entra na fila sozinho."""
import logging
import threading
import time
from pathlib import Path

from . import db, jobs, paths
from . import ffmpeg as ff

log = logging.getLogger("cortex9.watcher")
STABLE_CHECKS = 3      # tamanho igual em 3 leituras seguidas…
INTERVAL = 3.0         # …com 3s entre elas (≈ 9s sem crescer)
_sizes: dict[str, list] = {}


def scan_once():
    for wf in db.q("SELECT * FROM watch_folders WHERE enabled=1"):
        folder = Path(wf["path"]).expanduser()
        if not folder.is_dir():
            continue
        for f in folder.iterdir():
            if f.is_dir() and not f.name.startswith("."):
                _photo_folder(f, wf["preset_id"])
                continue
            if f.name.startswith(".") or f.suffix.lower() not in paths.VIDEO_EXTS or not f.is_file():
                continue
            key = str(f)
            if db.one("SELECT 1 FROM seen_files WHERE path=?", key):
                continue
            try:
                st = f.stat()
            except OSError:
                continue
            hist = _sizes.setdefault(key, [])
            hist.append((st.st_size, st.st_mtime))
            del hist[:-STABLE_CHECKS]
            if len(hist) < STABLE_CHECKS or len({h for h in hist}) != 1 or st.st_size == 0:
                continue
            if time.time() - st.st_mtime < INTERVAL * 2:
                continue
            if not _readable(f):
                continue  # ainda sendo escrito (ex.: moov atom no fim)
            _sizes.pop(key, None)
            create_episode(f, wf["preset_id"])


def _readable(f):
    try:
        ff.probe(f)
        return True
    except Exception:  # noqa: BLE001
        return False


def create_episode(f: Path, preset_id):
    st = f.stat()
    ep_id = db.insert("episodes", preset_id=preset_id, source_path=str(f), title=f.stem, status="na_fila", stage="Na fila",
                      created_at=db.now(), meta={})
    work = paths.WORK / f"ep{ep_id:04d}_{f.stem[:40]}"
    db.update("episodes", ep_id, work_dir=str(work))
    db.execute("INSERT OR REPLACE INTO seen_files(path,size,mtime,episode_id) VALUES(?,?,?,?)", str(f), st.st_size, st.st_mtime, ep_id)
    jobs.enqueue("analise", "analyze", episode_id=ep_id)
    log.info("Novo episódio %s: %s", ep_id, f)
    return ep_id


def loop(stop: threading.Event):
    while not stop.wait(INTERVAL):
        try:
            scan_once()
        except Exception:  # noqa: BLE001
            log.exception("watcher")


def _photo_folder(d: Path, preset_id):
    """Subpasta de fotos solta na pasta vigiada → vídeo de fotos automático (trilha: MP3 dentro da pasta ou a do kit)."""
    from . import photos
    if db.one("SELECT 1 FROM seen_folders WHERE path=?", str(d)):
        return
    if photos.folder_ready(d):
        try:
            pid = photos.create(d, preset_id)
            db.execute("INSERT OR REPLACE INTO seen_folders(path, project_id) VALUES(?,?)", str(d), pid)
        except ValueError as e:
            log.info("Pasta %s ignorada: %s", d, e)
            db.execute("INSERT OR REPLACE INTO seen_folders(path, project_id) VALUES(?,NULL)", str(d))
