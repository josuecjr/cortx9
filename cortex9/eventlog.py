"""Log de eventos do CorteX9: o que aconteceu, em qual projeto, em qual etapa e por que falhou.

Cada evento vai para o banco (aba Log) e para o arquivo logs/cortex9.log.
A fila marca o projeto do job em andamento (contexto da thread), então qualquer erro
registrado durante o job — inclusive o comando FFmpeg completo — já sai ligado ao projeto.
"""
import contextlib
import logging
import platform
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime

from . import db, paths

log = logging.getLogger("cortex9.log")
_ctx = threading.local()
MAX_DETAIL = 20000
KEEP = 20000  # eventos guardados no banco (os mais antigos saem)

SCHEMA = """
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY, ts REAL, level TEXT, ref_kind TEXT, ref_id INTEGER, step TEXT,
  message TEXT, detail TEXT);
CREATE INDEX IF NOT EXISTS events_ref ON events(ref_kind, ref_id);
"""
_ready = False
_lock = threading.Lock()

KINDS = {"fotos": "Vídeo de fotos", "episodio": "Episódio", "app": "App"}


def _ensure():
    global _ready
    if not _ready:
        with _lock:
            if not _ready:
                db.conn().executescript(SCHEMA)
                _ready = True


# ------------------------------------------------------------------ contexto (projeto do job atual)
@contextlib.contextmanager
def context(kind, ref_id, step=None):
    old = getattr(_ctx, "v", None)
    _ctx.v = (kind, ref_id, step)
    try:
        yield
    finally:
        _ctx.v = old


def current():
    return getattr(_ctx, "v", None) or (None, None, None)


# ------------------------------------------------------------------ gravar
def add(level, message, *, kind=None, ref=None, step=None, detail=None):
    """level: info | aviso | erro."""
    ck, cr, cs = current()
    kind, ref, step = kind or ck or "app", ref if ref is not None else cr, step or cs
    detail = (str(detail)[-MAX_DETAIL:] if detail else None)
    try:
        _ensure()
        db.insert("events", ts=time.time(), level=level, ref_kind=kind, ref_id=ref, step=step, message=str(message)[:2000], detail=detail)
    except Exception:  # noqa: BLE001  (o log nunca pode derrubar o app)
        pass
    pylevel = {"erro": logging.ERROR, "aviso": logging.WARNING}.get(level, logging.INFO)
    where = f"[{kind} #{ref}]" if ref is not None else f"[{kind}]"
    log.log(pylevel, "%s %s%s%s", where, f"{step}: " if step else "", message,
            f"\n{detail}" if detail and pylevel >= logging.WARNING else "", extra={"c9_event": True})


def info(message, **kw):
    add("info", message, **kw)


def warn(message, **kw):
    add("aviso", message, **kw)


def error(message, **kw):
    add("erro", message, **kw)


class DBHandler(logging.Handler):
    """Leva para a aba Log os avisos e erros de qualquer parte do app (inclusive tracebacks)."""

    def __init__(self):
        super().__init__(level=logging.WARNING)

    def emit(self, record):
        if getattr(record, "c9_event", False) or not record.name.startswith("cortex9"):
            return
        try:
            detail = self.formatter.formatException(record.exc_info) if (record.exc_info and self.formatter) else None
            if record.exc_info and not detail:
                detail = logging.Formatter().formatException(record.exc_info)
            ck, cr, cs = current()
            _ensure()
            db.insert("events", ts=record.created, level="erro" if record.levelno >= logging.ERROR else "aviso",
                      ref_kind=ck or "app", ref_id=cr, step=cs or record.name.replace("cortex9.", ""),
                      message=record.getMessage()[:2000], detail=(detail or "")[-MAX_DETAIL:] or None)
        except Exception:  # noqa: BLE001
            pass


def install():
    _ensure()
    h = DBHandler()
    h.setFormatter(logging.Formatter())
    logging.getLogger().addHandler(h)
    try:  # mantém o banco leve
        db.execute("DELETE FROM events WHERE id < (SELECT COALESCE(MAX(id),0) FROM events) - ?", KEEP)
    except Exception:  # noqa: BLE001
        pass


# ------------------------------------------------------------------ ler
def query(level=None, kind=None, ref=None, text=None, limit=500, after_id=None):
    _ensure()
    sql, args = "SELECT * FROM events WHERE 1=1", []
    if level == "erro":
        sql += " AND level='erro'"
    elif level == "aviso":
        sql += " AND level IN ('erro','aviso')"
    if kind:
        sql += " AND ref_kind=?"
        args.append(kind)
    if ref is not None:
        sql += " AND ref_id=?"
        args.append(ref)
    if text:
        sql += " AND (message LIKE ? OR detail LIKE ? OR step LIKE ?)"
        args += [f"%{text}%"] * 3
    if after_id:
        sql += " AND id>?"
        args.append(after_id)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    return db.q(sql, *args)


def refs():
    """Projetos que aparecem no log (para o filtro)."""
    _ensure()
    out = []
    for r in db.q("SELECT ref_kind, ref_id, MAX(ts) ts, SUM(level='erro') erros FROM events WHERE ref_id IS NOT NULL GROUP BY ref_kind, ref_id ORDER BY ts DESC LIMIT 100"):
        if r["ref_kind"] == "fotos":
            p = db.one("SELECT title FROM photo_projects WHERE id=?", r["ref_id"])
        elif r["ref_kind"] == "episodio":
            p = db.one("SELECT title FROM episodes WHERE id=?", r["ref_id"])
        else:
            p = None
        out.append({"kind": r["ref_kind"], "id": r["ref_id"], "erros": r["erros"],
                    "nome": f"{KINDS.get(r['ref_kind'], r['ref_kind'])} #{r['ref_id']}" + (f" · {p['title']}" if p else "")})
    return out


def clear():
    _ensure()
    db.execute("DELETE FROM events")


# ------------------------------------------------------------------ diagnóstico
def diagnostics():
    from . import VERSION
    from . import ffmpeg as ff
    d = {"CorteX9": VERSION, "macOS/sistema": platform.platform(), "processador": platform.machine(),
         "Python": sys.version.split()[0], "dados": str(paths.BASE), "finais (padrão)": str(paths.DEFAULT_OUTPUT)}
    try:
        exe = ff.FFMPEG()
        d["FFmpeg"] = exe
        p = subprocess.run([exe, "-hide_banner", "-version"], capture_output=True, text=True, timeout=10)
        d["FFmpeg versão"] = (p.stdout.splitlines() or ["?"])[0]
        caps = ff.capabilities()
        d["FFmpeg recursos"] = ", ".join(f"{k}: {'sim' if v else 'NÃO'}" for k, v in caps.items())
        d["Render"] = "VideoToolbox (GPU)" if ff.hw_available() else "software (libx264)"
    except Exception as e:  # noqa: BLE001
        d["FFmpeg"] = f"PROBLEMA: {e}"
    try:
        total, _used, free = shutil.disk_usage(paths.BASE)
        d["disco livre"] = f"{free / 1e9:.1f} GB de {total / 1e9:.0f} GB"
    except OSError:
        pass
    return d


def export_text(**filters):
    rows = query(limit=5000, **filters)
    lines = ["CorteX9 — log exportado em " + datetime.now().strftime("%d/%m/%Y %H:%M:%S"), "", "== Diagnóstico =="]
    lines += [f"{k}: {v}" for k, v in diagnostics().items()]
    lines += ["", f"== Eventos ({len(rows)}, mais recentes primeiro) =="]
    for r in rows:
        when = datetime.fromtimestamp(r["ts"]).strftime("%d/%m %H:%M:%S")
        ref = f"{KINDS.get(r['ref_kind'], r['ref_kind'])}" + (f" #{r['ref_id']}" if r["ref_id"] is not None else "")
        lines.append(f"[{when}] {r['level'].upper():5} {ref} · {r['step'] or '-'} · {r['message']}")
        if r["detail"]:
            lines += ["    " + x for x in r["detail"].splitlines()]
    return "\n".join(lines) + "\n"


def export_file(**filters):
    from pathlib import Path
    dest_dir = Path.home() / "Desktop"
    if not dest_dir.is_dir():
        dest_dir = paths.LOGS
    out = dest_dir / f"CorteX9-log-{datetime.now():%Y-%m-%d_%H%M%S}.txt"
    out.write_text(export_text(**filters), encoding="utf-8")
    return str(out)
