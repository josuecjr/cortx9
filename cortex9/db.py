"""Banco SQLite do CorteX9 (thread-safe, colunas JSON)."""
import json
import sqlite3
import threading
import time

from . import paths

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS presets(id INTEGER PRIMARY KEY, name TEXT, data TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS watch_folders(id INTEGER PRIMARY KEY, path TEXT UNIQUE, preset_id INTEGER, enabled INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS seen_files(path TEXT PRIMARY KEY, size INTEGER, mtime REAL, episode_id INTEGER);
CREATE TABLE IF NOT EXISTS episodes(
  id INTEGER PRIMARY KEY, preset_id INTEGER, source_path TEXT, title TEXT, status TEXT,
  stage TEXT, progress REAL DEFAULT 0, created_at REAL, previews_ready_at REAL, finished_at REAL,
  work_dir TEXT, error TEXT, meta TEXT DEFAULT '{}');
CREATE TABLE IF NOT EXISTS cuts(
  id INTEGER PRIMARY KEY, episode_id INTEGER, idx INTEGER, plan TEXT, reasoning TEXT,
  experimental INTEGER DEFAULT 0, status TEXT, chosen_variation INTEGER, adjust_note TEXT,
  error TEXT, finals TEXT DEFAULT '[]', drive_link TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS variations(
  id INTEGER PRIMARY KEY, cut_id INTEGER, idx INTEGER, spec TEXT, preview_path TEXT, thumb_path TEXT,
  status TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS jobs(
  id INTEGER PRIMARY KEY, kind TEXT, lane TEXT, episode_id INTEGER, cut_id INTEGER, variation_id INTEGER,
  status TEXT, attempts INTEGER DEFAULT 0, error TEXT, payload TEXT DEFAULT '{}', created_at REAL, updated_at REAL);
CREATE TABLE IF NOT EXISTS style_library(
  id INTEGER PRIMARY KEY, preset_id INTEGER, cut_id INTEGER, decision TEXT, hook TEXT, duration REAL,
  music_mood TEXT, lut TEXT, effects TEXT, ai_score REAL, experimental INTEGER, note TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS costs(
  id INTEGER PRIMARY KEY, episode_id INTEGER, service TEXT, units REAL, unit_type TEXT, usd REAL, created_at REAL);
CREATE TABLE IF NOT EXISTS photo_projects(
  id INTEGER PRIMARY KEY, preset_id INTEGER, folder TEXT, title TEXT, options TEXT DEFAULT '{}', status TEXT, stage TEXT,
  progress REAL DEFAULT 0, plan TEXT, variations TEXT DEFAULT '[]', chosen INTEGER, finals TEXT DEFAULT '[]', error TEXT,
  work_dir TEXT, meta TEXT DEFAULT '{}', created_at REAL, previews_ready_at REAL, finished_at REAL);
CREATE TABLE IF NOT EXISTS seen_folders(path TEXT PRIMARY KEY, project_id INTEGER);
CREATE TABLE IF NOT EXISTS outbox(
  id INTEGER PRIMARY KEY, service TEXT, action TEXT, payload TEXT, attempts INTEGER DEFAULT 0,
  last_error TEXT, next_try_at REAL, done INTEGER DEFAULT 0, created_at REAL);
"""

JSON_COLS = {"data", "meta", "plan", "spec", "payload", "finals", "effects", "options", "variations"}


def conn() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            paths.ensure()
            _conn = sqlite3.connect(paths.DB_PATH, check_same_thread=False, isolation_level=None)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.executescript(SCHEMA)
        return _conn


def _row(r):
    if r is None:
        return None
    d = dict(r)
    for k in JSON_COLS & d.keys():
        if isinstance(d[k], str):
            try:
                d[k] = json.loads(d[k])
            except ValueError:
                pass
    return d


def _enc(v):
    return json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v


def q(sql, *args):
    with _lock:
        return [_row(r) for r in conn().execute(sql, args).fetchall()]


def one(sql, *args):
    with _lock:
        return _row(conn().execute(sql, args).fetchone())


def insert(table, **vals):
    vals = {k: _enc(v) for k, v in vals.items()}
    cols = ",".join(vals)
    with _lock:
        cur = conn().execute(f"INSERT INTO {table}({cols}) VALUES({','.join('?' * len(vals))})", tuple(vals.values()))
        return cur.lastrowid


def update(table, id_, **vals):
    if not vals:
        return
    sets = ",".join(f"{k}=?" for k in vals)
    with _lock:
        conn().execute(f"UPDATE {table} SET {sets} WHERE id=?", (*[_enc(v) for v in vals.values()], id_))


def execute(sql, *args):
    with _lock:
        conn().execute(sql, args)


def setting(key, default=None):
    r = one("SELECT value FROM settings WHERE key=?", key)
    if not r:
        return default
    try:
        return json.loads(r["value"])
    except ValueError:
        return r["value"]


def set_setting(key, value):
    execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            key, json.dumps(value, ensure_ascii=False))


def now():
    return time.time()


DEFAULT_PRESET = {
    "cliente": "Cliente padrão",
    "projeto": "Podcast",
    "formatos": ["9:16", "1:1", "16:9"],
    "rotacao_yolobox": 0,
    "pasta_kit": "",
    "trilha": "",
    "logo": "",
    "posicao_logo": "superior-direita",
    "abertura": "",
    "encerramento": "",
    "legenda": {"fonte": "Arial Black", "tamanho": 72, "cor": "#FFFFFF", "destaque": "#FFD400",
                "contorno": 5, "posicao": "inferior", "maiusculas": True, "palavras_por_linha": 3},
    "efeitos": ["zoom_punch", "flash_entrada", "fade_saida"],
    "cortes_min": 10, "cortes_max": 15,
    "duracao_min": 20, "duracao_max": 75,
    "variacoes": 3,
    "idioma": "pt",
    "voz": "local",               # local | elevenlabs
    "fonte_trilha": "kit",         # kit | epidemic
    "pasta_saida": "",
    "arquivar_drive": True,
    "contexto": "",                # descrição livre do cliente/tom, vai para a IA diretora
}


def seed():
    """Cria preset e pasta vigiada padrão na primeira execução."""
    if not one("SELECT id FROM presets LIMIT 1"):
        data = dict(DEFAULT_PRESET, pasta_saida=str(paths.DEFAULT_OUTPUT))
        pid = insert("presets", name="Padrão", data=data, created_at=now())
        insert("watch_folders", path=str(paths.DEFAULT_INBOX), preset_id=pid, enabled=1)
    # Jobs interrompidos por fechamento do app voltam para a fila
    execute("UPDATE jobs SET status='queued' WHERE status='running'")
