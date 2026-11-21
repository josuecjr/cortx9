"""Fila em segundo plano. Cada etapa é um job; se um falhar, os outros seguem."""
import json
import logging
import threading

from . import db, pipeline
from .errors import explain

log = logging.getLogger("cortex9.jobs")
LANES = {"analise": 1, "previa": 2, "final": 1}   # workers por fila (M3 Pro aguenta 2 renders leves em paralelo)
_claim = threading.Lock()
_wake = threading.Event()


def enqueue(lane, kind, episode_id=None, cut_id=None, variation_id=None, payload=None):
    jid = db.insert("jobs", kind=kind, lane=lane, episode_id=episode_id, cut_id=cut_id, variation_id=variation_id,
                    status="queued", payload=payload or {}, created_at=db.now(), updated_at=db.now())
    _wake.set()
    return jid


def _next(lane):
    with _claim:
        j = db.one("SELECT * FROM jobs WHERE lane=? AND status='queued' ORDER BY id LIMIT 1", lane)
        if j:
            db.update("jobs", j["id"], status="running", attempts=j["attempts"] + 1, updated_at=db.now())
        return j


def _run(j):
    k = j["kind"]
    if k == "analyze":
        pipeline.analyze(j["episode_id"])
    elif k == "preview":
        pipeline.preview(j["variation_id"])
    elif k == "final":
        pipeline.final(j["cut_id"])
    elif k == "adjust":
        pipeline.adjust(j["cut_id"])
    elif k.startswith("photo_"):
        from . import photos
        pl = j["payload"] or {}
        {"photo_analyze": lambda: photos.analyze(pl["project_id"]), "photo_preview": lambda: photos.preview(pl["project_id"], pl["var"]),
         "photo_final": lambda: photos.final(pl["project_id"]), "photo_adjust": lambda: photos.adjust(pl["project_id"])}[k]()
    else:
        raise ValueError(f"job desconhecido {k}")


def _fail(j, exc):
    step = {"analyze": "análise do episódio", "preview": "prévia", "final": "render final", "adjust": "ajuste rápido",
            "photo_analyze": "análise das fotos", "photo_preview": "prévia do vídeo de fotos", "photo_final": "render final do vídeo de fotos",
            "photo_adjust": "ajuste do vídeo de fotos"}[j["kind"]]
    err = explain(step, exc).as_dict()
    payload = json.dumps(err, ensure_ascii=False)
    db.update("jobs", j["id"], status="failed", error=payload, updated_at=db.now())
    if j["kind"].startswith("photo_"):
        pid = (j["payload"] or {}).get("project_id")
        if j["kind"] != "photo_preview":  # prévia já marca a variação com erro
            db.update("photo_projects", pid, status="erro", error=payload, stage=f"Erro: {err['motivo']}")
        return
    log.error("Job %s (%s) falhou: %s — %s", j["id"], j["kind"], err["motivo"], err["detalhe"][-300:])
    if j["kind"] == "analyze":
        db.update("episodes", j["episode_id"], status="erro", error=payload, stage=f"Erro: {err['motivo']}")
    elif j["kind"] == "preview":
        db.update("variations", j["variation_id"], status="erro", error=payload)
        pipeline.refresh_cut_status(j["cut_id"])
    elif j["kind"] in ("final", "adjust"):
        db.update("cuts", j["cut_id"], status="erro", error=payload)
        if j["kind"] == "final":
            pipeline.maybe_finish_episode(j["episode_id"])


def worker(lane, stop):
    while not stop.is_set():
        j = _next(lane)
        if not j:
            _wake.wait(2)
            _wake.clear()
            continue
        try:
            _run(j)
            db.update("jobs", j["id"], status="done", error=None, updated_at=db.now())
        except Exception as e:  # noqa: BLE001
            log.exception("job %s", j["id"])
            try:
                _fail(j, e)
            except Exception:  # noqa: BLE001
                log.exception("falha registrando erro")


def start(stop):
    ts = []
    for lane, n in LANES.items():
        for i in range(n):
            t = threading.Thread(target=worker, args=(lane, stop), name=f"fila-{lane}-{i}", daemon=True)
            t.start()
            ts.append(t)
    return ts


def retry_job(job_id):
    j = db.one("SELECT * FROM jobs WHERE id=?", job_id)
    if not j:
        return
    db.update("jobs", job_id, status="queued", error=None)
    if j["kind"].startswith("photo_"):
        pid = (j["payload"] or {}).get("project_id")
        st = {"photo_analyze": "na_fila", "photo_preview": "gerando_previas", "photo_final": "renderizando_final", "photo_adjust": "ajustando"}[j["kind"]]
        db.update("photo_projects", pid, status=st, error=None, stage="De volta à fila")
        if j["kind"] == "photo_preview":
            from . import photos
            p = photos.get(pid)
            vs = p["variations"]
            vs[(j["payload"] or {})["var"]].update(status="na_fila", erro=None)
            db.update("photo_projects", pid, variations=vs, previews_ready_at=None)
        _wake.set()
        return
    if j["kind"] == "analyze":
        db.update("episodes", j["episode_id"], status="na_fila", error=None, stage="Na fila")
    elif j["kind"] == "preview":
        db.update("variations", j["variation_id"], status="na_fila", error=None)
        db.update("cuts", j["cut_id"], status="gerando_previa", error=None)
        db.update("episodes", j["episode_id"], previews_ready_at=None, status="gerando_previas")
    elif j["kind"] in ("final", "adjust"):
        db.update("cuts", j["cut_id"], status="aprovado" if j["kind"] == "final" else "ajustando", error=None)
    _wake.set()


def retry_failed(episode_id=None, cut_id=None):
    rows = db.q("SELECT id FROM jobs WHERE status='failed' AND (? IS NULL OR episode_id=?) AND (? IS NULL OR cut_id=?)",
                episode_id, episode_id, cut_id, cut_id)
    for r in rows:
        retry_job(r["id"])
    return len(rows)


def summary():
    rows = db.q("SELECT lane, status, COUNT(*) n FROM jobs WHERE status IN ('queued','running','failed') GROUP BY lane, status")
    return rows
