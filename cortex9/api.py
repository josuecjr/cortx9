"""Métodos chamados pela interface (janela nativa)."""
import json
import logging
import os
from pathlib import Path
from urllib.parse import quote

from . import VERSION, db, eventlog, jobs, luts, paths, pipeline, services, style, transcribe, watcher
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
    eventlog._ensure()
    erros = db.one("SELECT COUNT(*) n FROM events WHERE level='erro' AND ts>?", db.now() - 24 * 3600)["n"]
    return {"versao": VERSION, "erros_recentes": erros, "episodios": out, "fila": q, "envios_pendentes": pend,
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


def open_url(url):
    """Abre no navegador a página onde se cria a chave (só links conhecidos das Conexões)."""
    if url not in services.LINK_URLS and not str(url).startswith("https://drive.google.com/"):
        raise ValueError("Link não reconhecido.")
    import subprocess
    import sys
    import webbrowser
    if sys.platform == "darwin":
        subprocess.Popen(["open", url])
    else:
        webbrowser.open(url)
    return True


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
    rows = db.q("SELECT id, title, status, stage, progress, created_at, previews_ready_at, finished_at, error, folder, variations FROM photo_projects ORDER BY id DESC LIMIT 60")
    for r in rows:
        r["erro"] = _err(r.pop("error"))
        vs = r.pop("variations") or []
        if not r["erro"] and r["status"] == "erro":  # projetos antigos: motivo estava só na versão
            r["erro"] = next((v.get("erro") for v in vs if v.get("erro")), None)
        r["travado"] = _photo_stalled(r)
        r["erros_log"] = (db.one("SELECT COUNT(*) n FROM events WHERE ref_kind='fotos' AND ref_id=? AND level='erro'", r["id"]) or {}).get("n", 0) if _has_events() else 0
    first = db.one("SELECT data FROM presets ORDER BY id LIMIT 1")
    kit = {**db.DEFAULT_PRESET, **((first or {}).get("data") or {})}
    return {"projetos": rows, "estilos": photos.ESTILOS, "formatos": list(photos.SIZES),
            "destino_padrao": str(Path(kit.get("pasta_saida") or paths.DEFAULT_OUTPUT).expanduser()) + "/AAAA-MM-DD/cliente/projeto",
            "drive_local": services.get("drive_pasta_local") or "", "drive_api": bool(services.get("gdrive_service_account"))}


def _has_events():
    eventlog._ensure()
    return True


def photo_project(id):
    from . import photos  # noqa: F811
    p = photos.get(id)
    if not p:
        return None
    plan, meta = p["plan"] or {}, p["meta"] or {}
    fotos = {f["i"]: f for f in meta.get("fotos", [])}
    tempos = plan.get("_tempos") or []
    cenas = []
    for k, c in enumerate(plan.get("cenas", [])):
        ids = c.get("fotos") or ([c["foto"]] if c.get("foto") is not None else [])
        fs = [fotos.get(i, {}) for i in ids]
        cenas.append({"n": k + 1, "layout": c.get("layout", "unica"), "ato": c.get("ato"), "foto": ", ".join(f.get("nome", "?") for f in fs),
                      "thumbs": [media(f.get("thumb")) for f in fs], "thumb": media(fs[0].get("thumb")) if fs else None,
                      "inicio": tempos[k] if k < len(tempos) else None, "fim": tempos[k + 1] if k + 1 < len(tempos) else None,
                      "movimento": c.get("movimento"), "transicao": c.get("transicao"), "texto": c.get("texto"), "estilo_texto": c.get("estilo_texto"),
                      "motivo": c.get("motivo"), "ajustes": sorted({a for f in fs for a in f.get("ajustes", [])})})
    cost = services.episode_cost(-id)
    _pid, preset = pipeline.preset_of(p)
    opts = {**photos.DEFAULTS, **(p["options"] or {})}
    destino = {"computador": str(photos.output_dir(p, preset, opts)), "drive_pasta": photos.drive_subpath(preset, opts),
               "enviar_drive": bool(opts.get("enviar_drive", True) and preset.get("arquivar_drive", True)),
               "drive_configurado": bool(services.get("drive_pasta_local") or services.get("gdrive_service_account")),
               "drive": meta.get("drive")}
    erro = _err(p["error"])
    if not erro and p["status"] == "erro":
        erro = next((v.get("erro") for v in (p["variations"] or []) if v.get("erro")), None)
    return {"travado": _photo_stalled(p), "destino": destino,"id": p["id"], "titulo": p["title"], "status": p["status"], "etapa": p["stage"], "progresso": p["progress"], "pasta": p["folder"],
            "opcoes": p["options"], "erro": erro, "avisos": meta.get("avisos", []), "motor": meta.get("motor"),
            "musica": meta.get("musica"), "trilha": meta.get("trilha"), "titulo_video": plan.get("titulo"), "justificativa": plan.get("justificativa"),
            "fotos_total": len(fotos), "fotos_usadas": len({i for c in plan.get("cenas", []) for i in (c.get("fotos") or [c.get("foto")])}),
            "plano_antigo": bool(plan.get("cenas")) and not any(c.get("layout") for c in plan.get("cenas", [])), "cenas": cenas, "duplicadas": meta.get("duplicadas", []), "escolhida": p["chosen"], "custo": cost, "ajuste": meta.get("ajuste"),
            "finais": [{**f, "url": media(f["arquivo"])} for f in (p["finals"] or [])],
            "variacoes": [{**{k: v for k, v in x.items() if k not in ("video", "thumb")}, "video": media(x.get("video")), "thumb": media(x.get("thumb"))}
                          for x in (p["variations"] or [])]}


def create_photo_video(pasta, trilha="", titulo="", subtitulo="", estilo="auto", formatos=None, duracao=0, contexto="", preset_id=None,
                       pasta_saida="", enviar_drive=True, pasta_drive=""):
    from . import photos
    return photos.create(pasta, preset_id, trilha=trilha, titulo=titulo, subtitulo=subtitulo, estilo=estilo,
                         formatos=formatos or ["16:9"], duracao=float(duracao or 0), contexto=contexto,
                         pasta_saida=pasta_saida, enviar_drive=bool(enviar_drive), pasta_drive=pasta_drive)


def photo_set_destination(id, pasta_saida=None, enviar_drive=None, pasta_drive=None):
    """Muda o destino do final (computador e Drive) antes de aprovar."""
    from . import photos
    p = photos.get(id)
    if not p:
        raise ValueError("Projeto não encontrado.")
    o = dict(p["options"] or {})
    if pasta_saida is not None:
        if pasta_saida.strip():
            out = Path(pasta_saida).expanduser()
            out.mkdir(parents=True, exist_ok=True)
            o["pasta_saida"] = str(out)
        else:
            o["pasta_saida"] = ""
    if enviar_drive is not None:
        o["enviar_drive"] = bool(enviar_drive)
    if pasta_drive is not None:
        o["pasta_drive"] = pasta_drive.strip().strip("/")
    db.update("photo_projects", id, options=o)
    eventlog.info(f"Destino alterado · computador: {o.get('pasta_saida') or 'pasta do kit'} · Drive: "
                  + (f"sim ({o.get('pasta_drive') or 'pasta padrão'})" if o.get("enviar_drive", True) else "não"), kind="fotos", ref=id, step="destino")
    return photo_project(id)


# ------------------------------------------------------------------ log
def log_events(nivel=None, tipo=None, ref=None, texto=None, limite=400):
    return {"eventos": eventlog.query(level=nivel or None, kind=tipo or None, ref=ref, text=(texto or "").strip() or None, limit=min(int(limite or 400), 2000)),
            "projetos": eventlog.refs()}


def log_diagnostics():
    return eventlog.diagnostics()


def log_export(nivel=None, tipo=None, ref=None, texto=None):
    path = eventlog.export_file(level=nivel or None, kind=tipo or None, ref=ref, text=(texto or "").strip() or None)
    pipeline.reveal(path)
    return path


def log_text(nivel=None, tipo=None, ref=None, texto=None):
    return eventlog.export_text(level=nivel or None, kind=tipo or None, ref=ref, text=(texto or "").strip() or None)


def log_open_file():
    f = paths.LOGS / "cortex9.log"
    pipeline.reveal(f)
    return str(f)


def log_clear():
    eventlog.clear()
    eventlog.info("Log limpo pelo usuário.")
    return True


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


def _photo_active(id):
    return db.one("SELECT COUNT(*) n FROM jobs WHERE status IN ('queued','running') AND kind LIKE 'photo_%' AND json_extract(payload,'$.project_id')=?", id)["n"]


def _photo_stalled(r):
    return r["status"] in ("na_fila", "processando", "gerando_previas", "ajustando", "renderizando_final") and not _photo_active(r["id"])


def photo_replan(id):
    """Refaz o plano com a direção nova (cenas com várias fotos, atos, rajada no pico) e gera novas prévias."""
    from . import photos
    p = photos.get(id)
    if not p:
        raise ValueError("Projeto não encontrado.")
    if _photo_active(id):
        raise ValueError("Este projeto ainda está processando. Espere terminar.")
    meta = p["meta"] or {}
    meta["avisos"] = []
    db.update("photo_projects", id, meta=meta, plan=None, variations=[], chosen=None, status="na_fila", stage="Refazendo o plano", error=None,
              previews_ready_at=None, progress=0)
    jobs.enqueue("analise", "photo_analyze", payload={"project_id": id})
    eventlog.info("Plano refeito com a nova direção (cenas com várias fotos).", kind="fotos", ref=id)
    return True


def photo_retry(id):
    from . import photos
    rows = db.q("SELECT id FROM jobs WHERE status='failed' AND kind LIKE 'photo_%' AND json_extract(payload,'$.project_id')=?", id)
    for r in rows:
        jobs.retry_job(r["id"])
    if rows:
        eventlog.info(f"Tentar de novo: {len(rows)} etapa(s) voltaram para a fila.", kind="fotos", ref=id)
        return len(rows)
    # Nada falhou "oficialmente", mas o projeto está parado (ex.: app fechado no meio do render): refaz o que faltou.
    p = photos.get(id)
    if not p or _photo_active(id):
        return 0
    if not p["plan"]:
        db.update("photo_projects", id, status="na_fila", stage="De volta à fila", error=None)
        jobs.enqueue("analise", "photo_analyze", payload={"project_id": id})
        n = 1
    elif p["status"] == "renderizando_final":
        jobs.enqueue("final", "photo_final", payload={"project_id": id})
        n = 1
    else:
        vs = p["variations"] or []
        todo = [i for i, v in enumerate(vs) if v.get("status") != "pronta"]
        for i in todo:
            vs[i].update(status="na_fila", erro=None)
        db.update("photo_projects", id, variations=vs, status="gerando_previas", error=None, previews_ready_at=None,
                  stage=f"Gerando prévias ({len(vs) - len(todo)}/{len(vs)})")
        for i in todo:
            jobs.enqueue("previa", "photo_preview", payload={"project_id": id, "var": i})
        n = len(todo)
    eventlog.info(f"Tentar de novo: projeto estava parado sem nada na fila; {n} etapa(s) recolocadas.", kind="fotos", ref=id)
    return n


def photo_delete(id):
    db.execute("DELETE FROM jobs WHERE kind LIKE 'photo_%' AND status IN ('queued','failed') AND json_extract(payload,'$.project_id')=?", id)
    db.execute("DELETE FROM photo_projects WHERE id=?", id)
    return True


METHODS = {k: v for k, v in dict(globals()).items() if callable(v) and not k.startswith("_") and k not in ("media", "Path", "quote") and getattr(v, "__module__", "") == __name__}
