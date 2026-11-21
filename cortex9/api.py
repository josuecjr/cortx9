"""Métodos chamados pela interface (janela nativa)."""
import json
import logging
import os
from pathlib import Path
from urllib.parse import quote

from . import VERSION, db, jobs, luts, paths, pipeline, services, style, transcribe, watcher
from . import ffmpeg as ff

log = logging.getLogger("cortex9.api")
ROOT = Path(__file__).resolve().parent.parent
_window = None  # janela pywebview (para diálogos nativos)


def media(p):
    return f"/media?p={quote(str(p))}" if p else None


def _err(s):
    if not s:
        return None
    try:
        return json.loads(s)
    except (ValueError, TypeError):
        return {"motivo": str(s)}


# ------------------------------------------------------------------ painel
def state():
    eps = db.q("SELECT * FROM episodes ORDER BY id DESC LIMIT 60")
    out = []
    for e in eps:
        counts = {r["status"]: r["n"] for r in db.q("SELECT status, COUNT(*) n FROM cuts WHERE episode_id=? GROUP BY status", e["id"])}
        out.append({"id": e["id"], "titulo": e["title"], "status": e["status"], "etapa": e["stage"], "progresso": e["progress"],
                    "criado": e["created_at"], "previas_em": e["previews_ready_at"], "concluido_em": e["finished_at"],
                    "cortes": counts, "erro": _err(e["error"]), "avisos": (e["meta"] or {}).get("avisos", []),
                    "preset": (db.one("SELECT name FROM presets WHERE id=?", e["preset_id"]) or {}).get("name")})
    q = jobs.summary()
    pend = db.one("SELECT COUNT(*) n FROM outbox WHERE done=0")["n"]
    return {"versao": VERSION, "episodios": out, "fila": q, "envios_pendentes": pend,
            "pastas": [w["path"] for w in db.q("SELECT path FROM watch_folders WHERE enabled=1")],
            "whisper": transcribe.backend_name(), "hw": ff.hw_available()}


def episode(id):
    e = db.one("SELECT * FROM episodes WHERE id=?", id)
    if not e:
        return None
    cuts = []
    for c in db.q("SELECT * FROM cuts WHERE episode_id=? ORDER BY idx", id):
        vs = db.q("SELECT * FROM variations WHERE cut_id=? ORDER BY idx", c["id"])
        p = c["plan"] or {}
        cuts.append({
            "id": c["id"], "idx": c["idx"], "status": c["status"], "experimental": bool(c["experimental"]),
            "escolhida": c["chosen_variation"], "erro": _err(c["error"]), "ajuste": c["adjust_note"],
            "titulo": p.get("titulo"), "gancho": p.get("gancho"), "tema": p.get("tema"), "nota": p.get("nota"),
            "inicio": p.get("inicio"), "fim": p.get("fim"), "lut": p.get("lut"), "trilha": p.get("clima_trilha"),
            "efeitos": p.get("efeitos"), "estrutura": p.get("estrutura"), "justificativa": c["reasoning"],
            "limpeza": p.get("_limpeza"), "zooms": len(p.get("zooms", [])), "textos": [t["texto"] for t in p.get("textos", [])],
            "finais": [{**f, "url": media(f["arquivo"])} for f in (c["finals"] or [])], "drive": c["drive_link"],
            "variacoes": [{"id": v["id"], "idx": v["idx"], "status": v["status"], "spec": v["spec"], "erro": _err(v["error"]),
                           "video": media(v["preview_path"]), "thumb": media(v["thumb_path"])} for v in vs],
        })
    m = e["meta"] or {}
    _pid, pr = pipeline.preset_of(e)
    return {"formato": (pr.get("formatos") or ["9:16"])[0], "id": e["id"], "titulo": e["title"], "status": e["status"], "etapa": e["stage"], "progresso": e["progress"],
            "fonte": e["source_path"], "erro": _err(e["error"]), "avisos": m.get("avisos", []), "motor": m.get("motor_plano"),
            "resumo": m.get("resumo"), "cenas": m.get("analise_cenas", []), "link_cliente": m.get("link_cliente"),
            "criado": e["created_at"], "previas_em": e["previews_ready_at"], "concluido_em": e["finished_at"],
            "custo": services.episode_cost(id), "cortes": cuts, "transcricao": m.get("transcricao")}


def choose(cut_id, variation_id):
    db.update("cuts", cut_id, chosen_variation=variation_id)
    return True


def approve(cut_id, variation_id=None):
    pipeline.approve(cut_id, variation_id)
    return True


def reject(cut_id, note=""):
    pipeline.reject(cut_id, note)
    return True


def adjust(cut_id, note):
    if not (note or "").strip():
        raise ValueError("Escreva o ajuste desejado (ex.: 'mais curto', 'gancho: VOCÊ NÃO SABIA DISSO', 'outra LUT').")
    pipeline.request_adjust(cut_id, note.strip())
    return True


def approve_all(episode_id):
    return pipeline.approve_all(episode_id)


def retry(episode_id=None, cut_id=None):
    return jobs.retry_failed(episode_id, cut_id)


def delete_episode(episode_id):
    db.execute("DELETE FROM jobs WHERE episode_id=? AND status IN ('queued','failed')", episode_id)
    db.execute("DELETE FROM variations WHERE cut_id IN (SELECT id FROM cuts WHERE episode_id=?)", episode_id)
    db.execute("DELETE FROM cuts WHERE episode_id=?", episode_id)
    db.execute("DELETE FROM episodes WHERE id=?", episode_id)
    return True


def add_file(path, preset_id=None):
    p = Path(path).expanduser()
    if not p.exists():
        raise ValueError(f"Arquivo não encontrado: {p}")
    if preset_id is None:
        preset_id = db.one("SELECT id FROM presets ORDER BY id LIMIT 1")["id"]
    return watcher.create_episode(p, preset_id)


def reveal(path):
    pipeline.reveal(path)
    return True


# ------------------------------------------------------------------ kits (presets) e pastas
def presets():
    return {"presets": [{"id": p["id"], "nome": p["name"], "dados": {**db.DEFAULT_PRESET, **(p["data"] or {})}} for p in db.q("SELECT * FROM presets ORDER BY id")],
            "luts": luts.catalog(), "padrao": db.DEFAULT_PRESET}


def save_preset(nome, dados, id=None):
    if id:
        db.update("presets", id, name=nome, data=dados)
        return id
    return db.insert("presets", name=nome, data=dados, created_at=db.now())


def delete_preset(id):
    if db.one("SELECT 1 FROM watch_folders WHERE preset_id=?", id):
        raise ValueError("Este kit está ligado a uma pasta vigiada. Troque o kit da pasta antes de apagar.")
    db.execute("DELETE FROM presets WHERE id=?", id)
    return True


def folders():
    return [{"id": w["id"], "pasta": w["path"], "preset_id": w["preset_id"], "ativa": bool(w["enabled"]),
             "existe": Path(w["path"]).expanduser().is_dir()} for w in db.q("SELECT * FROM watch_folders ORDER BY id")]


def save_folder(pasta, preset_id, ativa=True, id=None):
    p = Path(pasta).expanduser()
    p.mkdir(parents=True, exist_ok=True)
    if id:
        db.update("watch_folders", id, path=str(p), preset_id=preset_id, enabled=1 if ativa else 0)
        return id
    return db.insert("watch_folders", path=str(p), preset_id=preset_id, enabled=1 if ativa else 0)


def delete_folder(id):
    db.execute("DELETE FROM watch_folders WHERE id=?", id)
    return True


def pick(kind="folder"):
    """Abre o seletor nativo do macOS."""
    if not _window:
        return None
    import webview
    t = webview.FOLDER_DIALOG if kind == "folder" else webview.OPEN_DIALOG
    r = _window.create_file_dialog(t)
    return r[0] if r else None


# ------------------------------------------------------------------ conexões
def connections():
    return services.status_all()


def save_connection(values):
    services.set_values(values)
    return services.status_all()


def test_connection(id):
    return services.test(id)


def clear_key(key):
    services.clear_secret(key)
    return services.status_all()


def outbox():
    return db.q("SELECT id, service, action, attempts, last_error, next_try_at, done, created_at FROM outbox ORDER BY id DESC LIMIT 50")


def outbox_retry_now():
    db.execute("UPDATE outbox SET next_try_at=0 WHERE done=0")
    services.outbox_tick()
    return outbox()


# ------------------------------------------------------------------ estilo, custos, histórico
def style_library():
    rows = db.q("SELECT s.*, c.idx, c.episode_id FROM style_library s LEFT JOIN cuts c ON c.id=s.cut_id WHERE decision<>'variacao_preterida' ORDER BY s.id DESC LIMIT 100")
    return {"resumo": style.summary(), "texto": style.as_prompt(), "itens": rows}


def costs():
    rows = db.q("""SELECT e.id, e.title, e.status, COALESCE(SUM(c.usd),0) usd FROM episodes e LEFT JOIN costs c ON c.episode_id=e.id
                   GROUP BY e.id ORDER BY e.id DESC LIMIT 100""")
    rows += db.q("""SELECT -p.id id, '[fotos] ' || p.title title, p.status, COALESCE(SUM(c.usd),0) usd FROM photo_projects p
                    LEFT JOIN costs c ON c.episode_id=-p.id GROUP BY p.id ORDER BY p.id DESC LIMIT 50""")
    by_service = db.q("SELECT service, unit_type, SUM(units) units, SUM(usd) usd FROM costs GROUP BY service, unit_type")
    done = [r for r in rows if r["status"] == "concluido"]
    media = round(sum(r["usd"] for r in done) / len(done), 4) if done else 0
    prices = {k: services.price(*k.split(":")) for k in services.PRICES}
    return {"episodios": rows, "por_servico": by_service, "media_por_episodio": media, "precos": prices}


def save_prices(precos):
    db.set_setting("precos", {k: float(v) for k, v in precos.items()})
    return costs()


def changelog():
    p = ROOT / "CHANGELOG.md"
    return p.read_text(encoding="utf-8") if p.exists() else ""


def settings():
    return {"whisper_modelo": db.setting("whisper_modelo", "large-v3-turbo"), "dados": str(paths.BASE)}


def save_settings(whisper_modelo=None):
    if whisper_modelo:
        db.set_setting("whisper_modelo", whisper_modelo)
    return settings()


def demo():
    from . import demo as d
    return d.make_demo_episode()


# ------------------------------------------------------------------ vídeo de fotos
def photo_projects():
    from . import photos
    rows = db.q("SELECT id, title, status, stage, progress, created_at, previews_ready_at, finished_at, error, folder FROM photo_projects ORDER BY id DESC LIMIT 60")
    for r in rows:
        r["erro"] = _err(r.pop("error"))
    return {"projetos": rows, "estilos": photos.ESTILOS, "formatos": list(photos.SIZES)}


def photo_project(id):
    from . import photos
    p = photos.get(id)
    if not p:
        return None
    plan, meta = p["plan"] or {}, p["meta"] or {}
    fotos = {f["i"]: f for f in meta.get("fotos", [])}
    tempos = plan.get("_tempos") or []
    cenas = [{"n": k + 1, "foto": fotos.get(c["foto"], {}).get("nome"), "thumb": media(fotos.get(c["foto"], {}).get("thumb")),
              "inicio": tempos[k] if k < len(tempos) else None, "fim": tempos[k + 1] if k + 1 < len(tempos) else None,
              "movimento": c["movimento"], "transicao": c["transicao"], "texto": c.get("texto"), "estilo_texto": c.get("estilo_texto"),
              "motivo": c.get("motivo"), "ajustes": fotos.get(c["foto"], {}).get("ajustes", [])} for k, c in enumerate(plan.get("cenas", []))]
    cost = services.episode_cost(-id)
    return {"id": p["id"], "titulo": p["title"], "status": p["status"], "etapa": p["stage"], "progresso": p["progress"], "pasta": p["folder"],
            "opcoes": p["options"], "erro": _err(p["error"]), "avisos": meta.get("avisos", []), "motor": meta.get("motor"),
            "musica": meta.get("musica"), "trilha": meta.get("trilha"), "titulo_video": plan.get("titulo"), "justificativa": plan.get("justificativa"),
            "fotos_total": len(fotos), "cenas": cenas, "duplicadas": meta.get("duplicadas", []), "escolhida": p["chosen"], "custo": cost, "ajuste": meta.get("ajuste"),
            "finais": [{**f, "url": media(f["arquivo"])} for f in (p["finals"] or [])],
            "variacoes": [{**{k: v for k, v in x.items() if k not in ("video", "thumb")}, "video": media(x.get("video")), "thumb": media(x.get("thumb"))}
                          for x in (p["variations"] or [])]}


def create_photo_video(pasta, trilha="", titulo="", subtitulo="", estilo="auto", formatos=None, duracao=0, contexto="", preset_id=None):
    from . import photos
    return photos.create(pasta, preset_id, trilha=trilha, titulo=titulo, subtitulo=subtitulo, estilo=estilo,
                         formatos=formatos or ["16:9"], duracao=float(duracao or 0), contexto=contexto)


def photo_choose(id, var):
    from . import photos
    photos.choose(id, var)
    return True


def photo_approve(id, var=None):
    from . import photos
    photos.approve(id, var)
    return True


def photo_reject(id, note=""):
    from . import photos
    photos.reject(id, note)
    return True


def photo_adjust(id, note):
    from . import photos
    if not (note or "").strip():
        raise ValueError("Escreva o ajuste (ex.: 'mais rápido', 'título: Casamento Ana & Léo', 'outra LUT', 'duração: 60').")
    photos.request_adjust(id, note.strip())
    return True


def photo_retry(id):
    rows = db.q("SELECT id FROM jobs WHERE status='failed' AND kind LIKE 'photo_%' AND json_extract(payload,'$.project_id')=?", id)
    for r in rows:
        jobs.retry_job(r["id"])
    return len(rows)


def photo_delete(id):
    db.execute("DELETE FROM jobs WHERE kind LIKE 'photo_%' AND status IN ('queued','failed') AND json_extract(payload,'$.project_id')=?", id)
    db.execute("DELETE FROM photo_projects WHERE id=?", id)
    return True


METHODS = {k: v for k, v in dict(globals()).items() if callable(v) and not k.startswith("_") and k not in ("media", "Path", "quote") and getattr(v, "__module__", "") == __name__}
