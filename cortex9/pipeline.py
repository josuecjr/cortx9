"""Etapas do episódio: análise → plano → prévias → aprovação → final → arquivamento."""
import json
import logging
import re
import shutil
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

from . import db, director, notify, paths, render, services, style, transcribe
from . import ffmpeg as ff
from .errors import StepError, explain

log = logging.getLogger("cortex9.pipeline")
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".aif", ".aiff", ".flac"}
IMG_EXTS = {".png", ".jpg", ".jpeg", ".webp"}


def preset_of(ep):
    p = db.one("SELECT * FROM presets WHERE id=?", ep["preset_id"]) or db.one("SELECT * FROM presets ORDER BY id LIMIT 1")
    data = dict(db.DEFAULT_PRESET)
    data.update(p["data"] or {})
    return p["id"], data


def stage(ep_id, name, progress=None, status=None):
    vals = {"stage": name}
    if progress is not None:
        vals["progress"] = progress
    if status:
        vals["status"] = status
    db.update("episodes", ep_id, **vals)
    log.info("Episódio %s: %s", ep_id, name)


def add_warning(ep_id, msg):
    ep = db.one("SELECT meta FROM episodes WHERE id=?", ep_id)
    meta = ep["meta"] or {}
    meta.setdefault("avisos", [])
    if msg not in meta["avisos"]:
        meta["avisos"].append(msg)
    db.update("episodes", ep_id, meta=meta)


def set_meta(ep_id, **kv):
    ep = db.one("SELECT meta FROM episodes WHERE id=?", ep_id)
    meta = ep["meta"] or {}
    meta.update(kv)
    db.update("episodes", ep_id, meta=meta)


# ------------------------------------------------------------------ kit do cliente
def resolve_kit(preset):
    kit = {"logo": preset.get("logo") or None, "abertura": preset.get("abertura") or None,
           "encerramento": preset.get("encerramento") or None, "trilhas": []}
    if preset.get("trilha"):
        t = Path(preset["trilha"]).expanduser()
        kit["trilhas"] += [x for x in t.rglob("*") if x.suffix.lower() in AUDIO_EXTS] if t.is_dir() else [t]
    folder = Path(preset.get("pasta_kit") or "").expanduser() if preset.get("pasta_kit") else None
    if folder and folder.is_dir():
        for f in folder.rglob("*"):
            n, ext = f.name.lower(), f.suffix.lower()
            if ext in IMG_EXTS and "logo" in n and not kit["logo"]:
                kit["logo"] = str(f)
            elif ext in ff_video_exts() and ("abertura" in n or "intro" in n or "vinheta" in n) and not kit["abertura"]:
                kit["abertura"] = str(f)
            elif ext in ff_video_exts() and ("encerramento" in n or "outro" in n or "final" in n) and not kit["encerramento"]:
                kit["encerramento"] = str(f)
            elif ext in AUDIO_EXTS:
                kit["trilhas"].append(f)
    for k in ("logo", "abertura", "encerramento"):
        if kit[k] and not Path(kit[k]).expanduser().exists():
            kit[k] = None
    return kit


def ff_video_exts():
    return paths.VIDEO_EXTS


def music_for(mood, preset, kit, work, ep_id):
    mood_n = (mood or "").lower()
    if mood_n in ("nenhuma", "sem", "sem trilha", "none"):
        return None
    if preset.get("fonte_trilha") == "epidemic" and services.get("epidemic_token"):
        try:
            p = services.epidemic_track(mood_n or "inspirador", Path(work), ep_id)
            if p:
                return p
        except Exception as e:  # noqa: BLE001
            add_warning(ep_id, f"Epidemic Sound: {explain('trilha', e).reason} Usando trilha do kit.")
    tracks = [t for t in kit["trilhas"] if Path(t).exists()]
    if not tracks:
        return None
    key = re.sub(r"[^a-z]", "", mood_n.replace("é", "e").replace("í", "i"))
    for t in tracks:
        if key and key[:4] in re.sub(r"[^a-z]", "", str(t).lower()):
            return t
    return tracks[hash(mood_n) % len(tracks)]


def _has_audio(p):
    try:
        return ff.probe(p)
    except StepError:
        return None


# ------------------------------------------------------------------ 1) análise e plano
def analyze(ep_id):
    ep = db.one("SELECT * FROM episodes WHERE id=?", ep_id)
    preset_id, preset = preset_of(ep)
    src = Path(ep["source_path"])
    work = Path(ep["work_dir"])
    work.mkdir(parents=True, exist_ok=True)
    stage(ep_id, "Lendo o vídeo", 0.02, status="processando")
    meta = ff.probe(src)
    set_meta(ep_id, video=meta)

    stage(ep_id, "Extraindo áudio", 0.05)
    wav = work / "audio16k.wav"
    if not wav.exists():
        if meta["has_audio"]:
            ff.run(["-i", str(src), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)], step="extrair áudio")
    tr_path = work / "transcricao.json"
    tr = transcribe.load(tr_path)
    if tr is None:
        stage(ep_id, "Transcrevendo (Whisper local)", 0.1)
        try:
            tr = transcribe.transcribe(wav, preset.get("idioma", "pt")) if wav.exists() else {"segments": []}
        except Exception as e:  # noqa: BLE001
            err = explain("transcrição", e)
            add_warning(ep_id, f"Transcrição: {err.reason} {err.fix} Os cortes seguem sem legenda, guiados pelo silêncio.")
            tr = {"segments": [], "erro": err.as_dict()}
        tr_path.write_text(json.dumps(tr, ensure_ascii=False))
    set_meta(ep_id, transcricao={"backend": tr.get("backend"), "segmentos": len(tr.get("segments", []))})

    stage(ep_id, "Extraindo quadros-chave", 0.35)
    frames = keyframes(src, meta, work, int(preset.get("rotacao_yolobox", 0)))

    stage(ep_id, "IA diretora montando o plano de edição", 0.45)
    plan, engine, warns = director.make_plan(ep_id, frames, tr, preset, meta, preset_id)
    for w in warns:
        add_warning(ep_id, w)
    (work / "plano.json").write_text(json.dumps(plan, ensure_ascii=False, indent=1))
    set_meta(ep_id, motor_plano=engine, resumo=plan.get("resumo_episodio"), analise_cenas=plan.get("analise_cenas", []))

    stage(ep_id, "Limpando respiros, cor e legendas", 0.6)
    kit = resolve_kit(preset)
    per_line = int((preset.get("legenda") or {}).get("palavras_por_linha", 3))
    all_lines, cut_chunks = [], []
    for c in plan["cortes"]:
        prepare_cut(c, src, meta, tr, preset)
        chunks = render.caption_chunks(render.cut_words(tr, c["inicio"], c["fim"]), per_line)
        cut_chunks.append(chunks)
        all_lines += [" ".join(w["w"] for w in ch) for ch in chunks]
    if all_lines:
        stage(ep_id, "Revisando legendas", 0.7)
        fixed, warn = director.review_captions(all_lines, ep_id, preset.get("contexto", ""))
        if warn:
            add_warning(ep_id, warn)
        k = 0
        for chunks in cut_chunks:
            for ch in chunks:
                new = fixed[k].split()
                if len(new) == len(ch):
                    for w, t in zip(ch, new):
                        w["w"] = t
                k += 1
    if preset.get("voz") == "elevenlabs":
        stage(ep_id, "Tratamento de voz (ElevenLabs)", 0.75)
    for i, c in enumerate(plan["cortes"]):
        c["_legendas"] = cut_chunks[i]
        if preset.get("voz") == "elevenlabs" and meta["has_audio"]:
            c["_voz"] = isolate_voice(ep_id, src, work, i, c)
        cid = db.insert("cuts", episode_id=ep_id, idx=i + 1, plan=c, reasoning=c.get("justificativa", ""),
                        experimental=1 if c.get("experimental") else 0, status="gerando_previa", created_at=db.now())
        for j, v in enumerate(c["variacoes"]):
            vid = db.insert("variations", cut_id=cid, idx=j + 1, spec=v, status="na_fila")
            from . import jobs
            jobs.enqueue("previa", "preview", episode_id=ep_id, cut_id=cid, variation_id=vid)
    set_meta(ep_id, kit={k: (v if k != "trilhas" else [str(x) for x in v][:20]) for k, v in kit.items()})
    stage(ep_id, f"Gerando prévias de {len(plan['cortes'])} cortes", 0.8, status="gerando_previas")
    if not plan["cortes"]:
        raise StepError("plano", "Nenhum trecho bom o suficiente foi encontrado.", "Confira se o vídeo tem fala e se a duração mínima do preset não é maior que o episódio.")


def keyframes(src, meta, work, rotation):
    d = work / "quadros"
    d.mkdir(exist_ok=True)
    n = int(min(24, max(8, meta["duration"] / 60)))
    rot = {90: "transpose=1,", 180: "hflip,vflip,", 270: "transpose=2,"}.get(rotation % 360, "")
    out = []
    for i in range(n):
        t = meta["duration"] * (i + 0.5) / n
        p = d / f"q{i:02d}.jpg"
        if not p.exists():
            try:
                ff.run(["-ss", f"{t:.2f}", "-i", str(src), "-frames:v", "1", "-vf", f"{rot}scale=512:-2", "-q:v", "5", str(p)], step="quadros-chave")
            except StepError:
                continue
        if p.exists():
            out.append({"t": t, "path": str(p)})
    return out


def prepare_cut(c, src, meta, tr, preset):
    a, b = c["inicio"], c["fim"]
    words = render.cut_words(tr, a, b)
    if words:
        ranges, removed = render.keep_ranges(words, a, b)
    else:
        ranges, removed = render.silence_ranges(src, a, b), []
    c["_ranges"] = [[round(s, 3), round(e, 3)] for s, e in ranges]
    c["_limpeza"] = {"vicios_removidos": [w["w"] for w in removed],
                     "segundos_removidos": round((b - a) - sum(e - s for s, e in ranges), 1)}
    c["_cold_open"] = list(render.cold_open_range(words, a, b))
    L = b - a
    c["_wb"] = render.wb_gains(src, [a + L * f for f in (0.2, 0.5, 0.8)])


def isolate_voice(ep_id, src, work, i, c):
    raw = work / f"voz_{i:02d}_bruta.wav"
    out = work / f"voz_{i:02d}_elevenlabs.wav"
    if out.exists():
        return str(out)
    off = max(0.0, c["inicio"] - 1.0)
    try:
        ff.run(["-ss", f"{off}", "-t", f"{c['fim'] - off + 1}", "-i", str(src), "-vn", "-ac", "1", "-ar", "44100", str(raw)], step="voz")
        services.elevenlabs_isolate(raw, out, ep_id)
        return str(out)
    except Exception as e:  # noqa: BLE001
        add_warning(ep_id, f"ElevenLabs: {explain('voz', e).reason} Usando tratamento de voz local.")
        return None


# ------------------------------------------------------------------ 2) render de variação
def render_job(cut, variation, kind, fmt, out_path):
    ep = db.one("SELECT * FROM episodes WHERE id=?", cut["episode_id"])
    preset_id, preset = preset_of(ep)
    c = cut["plan"]
    v = variation["spec"] if variation else c["variacoes"][0]
    meta = (ep["meta"] or {}).get("video") or ff.probe(ep["source_path"])
    work = Path(ep["work_dir"])
    kit = resolve_kit(preset)
    a, b = c["inicio"], c["fim"]
    ranges = [tuple(r) for r in c.get("_ranges") or [[a, b]]]
    structure = v.get("estrutura") or c.get("estrutura", "linear")
    prefix = [tuple(c["_cold_open"])] if structure == "cold_open" and c.get("_cold_open") else []
    tl = render.Timeline(prefix, ranges)
    textos = [{**x, "inicio": tl.map(a + x["inicio"]), "fim": max(tl.map(a + x["fim"]), tl.map(a + x["inicio"]) + 0.6)} for x in c.get("textos", [])]
    music = music_for(v.get("clima_trilha") or c.get("clima_trilha"), preset, kit, work, ep["id"])
    job = {
        "src": ep["source_path"], "meta": meta, "a": a, "b": b, "ranges": ranges, "prefix": prefix, "kind": kind, "fmt": fmt,
        "out": str(out_path), "lut": v.get("lut") or c["lut"], "efeitos": v.get("efeitos") if v.get("efeitos") is not None else c.get("efeitos"),
        "zooms": [{"abs_inicio": a + z["inicio"], "abs_fim": a + z["fim"], "intensidade": z["intensidade"]} for z in c.get("zooms", [])],
        "textos": textos, "hook": v.get("gancho") or c.get("gancho"), "captions": c.get("_legendas", []),
        "style": preset.get("legenda"), "music": music,
        "music_in": tl.map(a + c.get("musica_entra", 0)) if c.get("musica_entra") else 0.0,
        "music_out": tl.map(a + c.get("musica_sai", b - a)), "logo": kit["logo"], "logo_pos": preset.get("posicao_logo"),
        "rotation": int(preset.get("rotacao_yolobox", 0)), "wb": tuple(c.get("_wb", (1, 1, 1))), "work": str(work),
        "voice_wav": c.get("_voz"), "trans_in": c.get("transicao_entrada"), "trans_out": c.get("transicao_saida"),
        "fontsdir": str(Path(preset["pasta_kit"]).expanduser()) if preset.get("pasta_kit") and Path(preset["pasta_kit"]).expanduser().is_dir() else None,
    }
    for key, kk in (("intro", "abertura"), ("outro", "encerramento")):
        if kit.get(kk):
            m = _has_audio(kit[kk])
            if m:
                job[key], job[f"{key}_has_audio"], job[f"{key}_dur"] = kit[kk], m["has_audio"], m["duration"]
    return render.build_and_run(job)


def preview(variation_id):
    v = db.one("SELECT * FROM variations WHERE id=?", variation_id)
    cut = db.one("SELECT * FROM cuts WHERE id=?", v["cut_id"])
    ep = db.one("SELECT * FROM episodes WHERE id=?", cut["episode_id"])
    _pid, preset = preset_of(ep)
    db.update("variations", variation_id, status="renderizando", error=None)
    fmt = (preset.get("formatos") or ["9:16"])[0]
    out = Path(ep["work_dir"]) / "previas" / f"corte{cut['idx']:02d}_v{v['idx']}.mp4"
    res = render_job(cut, v, "preview", fmt, out)
    thumb = render.thumbnail(out, out.with_suffix(".jpg"), t=res["intro"] + min(1.5, (res["duration"] - res["intro"]) / 2))  # pula a abertura
    db.update("variations", variation_id, status="pronta", preview_path=str(out), thumb_path=str(thumb))
    refresh_cut_status(cut["id"])


def refresh_cut_status(cut_id):
    cut = db.one("SELECT * FROM cuts WHERE id=?", cut_id)
    if cut["status"] not in ("gerando_previa", "ajustando"):
        return
    vs = db.q("SELECT status FROM variations WHERE cut_id=?", cut_id)
    if all(v["status"] in ("pronta", "erro") for v in vs):
        ok = any(v["status"] == "pronta" for v in vs)
        db.update("cuts", cut_id, status="previa_pronta" if ok else "erro",
                  error=None if ok else json.dumps({"motivo": "Nenhuma variação renderizou.", "como_resolver": "Veja o erro de cada variação e clique em Tentar de novo."}, ensure_ascii=False))
    check_episode_previews(cut["episode_id"])


def check_episode_previews(ep_id):
    ep = db.one("SELECT * FROM episodes WHERE id=?", ep_id)
    if ep["previews_ready_at"] or ep["status"] not in ("gerando_previas",):
        return
    pending = db.one("SELECT COUNT(*) n FROM cuts WHERE episode_id=? AND status IN ('gerando_previa','ajustando')", ep_id)["n"]
    if pending:
        done = db.one("SELECT COUNT(*) n FROM variations v JOIN cuts c ON c.id=v.cut_id WHERE c.episode_id=? AND v.status IN ('pronta','erro')", ep_id)["n"]
        total = db.one("SELECT COUNT(*) n FROM variations v JOIN cuts c ON c.id=v.cut_id WHERE c.episode_id=?", ep_id)["n"] or 1
        db.update("episodes", ep_id, progress=0.8 + 0.2 * done / total, stage=f"Gerando prévias ({done}/{total})")
        return
    n = db.one("SELECT COUNT(*) n FROM cuts WHERE episode_id=? AND status='previa_pronta'", ep_id)["n"]
    mins = (time.time() - ep["created_at"]) / 60
    db.update("episodes", ep_id, previews_ready_at=db.now(), status="aguardando_aprovacao", stage="Prévias prontas para aprovação", progress=1.0)
    notify.send("CorteX9 — prévias prontas", f"{ep['title']}: {n} cortes prontos para aprovar (levou {mins:.0f} min).")


# ------------------------------------------------------------------ 3) aprovação
def approve(cut_id, variation_id=None):
    cut = db.one("SELECT c.*, e.preset_id FROM cuts c JOIN episodes e ON e.id=c.episode_id WHERE c.id=?", cut_id)
    if variation_id is None:
        v = db.one("SELECT * FROM variations WHERE cut_id=? AND status='pronta' ORDER BY idx LIMIT 1", cut_id)
        variation_id = v["id"] if v else None
    if variation_id is None:
        raise StepError("aprovação", "Este corte não tem prévia pronta.", "Aguarde a prévia ou clique em Tentar de novo.")
    v = db.one("SELECT * FROM variations WHERE id=?", variation_id)
    db.update("cuts", cut_id, status="aprovado", chosen_variation=variation_id, error=None)
    style.record(cut, v["spec"], "aprovado")
    for other in db.q("SELECT * FROM variations WHERE cut_id=? AND id<>?", cut_id, variation_id):
        style.record(cut, other["spec"], "variacao_preterida")
    from . import jobs
    jobs.enqueue("final", "final", episode_id=cut["episode_id"], cut_id=cut_id)
    db.update("episodes", cut["episode_id"], status="renderizando_finais")


def reject(cut_id, note=""):
    cut = db.one("SELECT c.*, e.preset_id FROM cuts c JOIN episodes e ON e.id=c.episode_id WHERE c.id=?", cut_id)
    db.update("cuts", cut_id, status="reprovado")
    style.record(cut, (cut["plan"].get("variacoes") or [{}])[0], "reprovado", note)
    maybe_finish_episode(cut["episode_id"])


def request_adjust(cut_id, note):
    cut = db.one("SELECT c.*, e.preset_id FROM cuts c JOIN episodes e ON e.id=c.episode_id WHERE c.id=?", cut_id)
    style.record(cut, (cut["plan"].get("variacoes") or [{}])[0], "ajuste", note)
    db.update("cuts", cut_id, status="ajustando", adjust_note=note)
    from . import jobs
    jobs.enqueue("analise", "adjust", episode_id=cut["episode_id"], cut_id=cut_id)


def adjust(cut_id):
    cut = db.one("SELECT * FROM cuts WHERE id=?", cut_id)
    ep = db.one("SELECT * FROM episodes WHERE id=?", cut["episode_id"])
    _pid, preset = preset_of(ep)
    work = Path(ep["work_dir"])
    tr = transcribe.load(work / "transcricao.json") or {"segments": []}
    meta = (ep["meta"] or {}).get("video")
    old = cut["plan"]
    base = {k: v for k, v in old.items() if not k.startswith("_")}
    new, warn = director.replan_cut(base, cut["adjust_note"] or "", tr, preset, meta, ep["id"])
    if warn:
        add_warning(ep["id"], warn)
    prepare_cut(new, Path(ep["source_path"]), meta, tr, preset)
    per_line = int((preset.get("legenda") or {}).get("palavras_por_linha", 3))
    new["_legendas"] = render.caption_chunks(render.cut_words(tr, new["inicio"], new["fim"]), per_line)
    if old.get("_voz") and abs(new["inicio"] - old["inicio"]) < 0.01 and abs(new["fim"] - old["fim"]) < 0.01:
        new["_voz"] = old["_voz"]
    new["_ajuste"] = cut["adjust_note"]
    db.update("cuts", cut_id, plan=new, reasoning=new.get("justificativa", ""), status="ajustando", chosen_variation=None)
    db.execute("DELETE FROM variations WHERE cut_id=?", cut_id)
    from . import jobs
    for j, v in enumerate(new["variacoes"]):
        vid = db.insert("variations", cut_id=cut_id, idx=j + 1, spec=v, status="na_fila")
        jobs.enqueue("previa", "preview", episode_id=ep["id"], cut_id=cut_id, variation_id=vid)


def approve_all(ep_id):
    n = 0
    for c in db.q("SELECT * FROM cuts WHERE episode_id=? AND status='previa_pronta'", ep_id):
        approve(c["id"], c["chosen_variation"])
        n += 1
    return n


# ------------------------------------------------------------------ 4) render final e arquivamento
def safe(s):
    return re.sub(r"[^\w\-]+", "_", s or "").strip("_")[:40] or "x"


def final(cut_id):
    cut = db.one("SELECT * FROM cuts WHERE id=?", cut_id)
    ep = db.one("SELECT * FROM episodes WHERE id=?", cut["episode_id"])
    _pid, preset = preset_of(ep)
    v = db.one("SELECT * FROM variations WHERE id=?", cut["chosen_variation"]) if cut["chosen_variation"] else None
    db.update("cuts", cut_id, status="renderizando_final", error=None)
    today = date.today().isoformat()
    outdir = Path(preset.get("pasta_saida") or paths.DEFAULT_OUTPUT).expanduser() / today / safe(preset["cliente"]) / safe(preset["projeto"])
    finals = []
    for fmt in preset.get("formatos") or ["9:16"]:
        name = f"{safe(preset['cliente'])}_{safe(ep['title'])}_corte{cut['idx']:02d}_{fmt.replace(':', 'x')}.mp4"
        res = render_job(cut, v, "final", fmt, outdir / name)
        finals.append({"formato": fmt, "arquivo": res["path"], "duracao": round(res["duration"], 1)})
    cover = None
    if preset.get("magnific_capa") and services.get("magnific_api_key"):
        try:
            raw = outdir / f"capa_corte{cut['idx']:02d}_bruta.jpg"
            ff.run(["-ss", "1.2", "-i", finals[0]["arquivo"], "-frames:v", "1", "-q:v", "2", str(raw)], step="capa")
            cover = outdir / f"capa_corte{cut['idx']:02d}.jpg"
            services.magnific_upscale(raw, cover, ep["id"], prompt=cut["plan"].get("tema", ""))
            raw.unlink(missing_ok=True)
        except Exception as e:  # noqa: BLE001
            add_warning(ep["id"], f"Magnific (capa do corte {cut['idx']}): {explain('capa', e).reason}")
    db.update("cuts", cut_id, status="finalizado", finals=finals)
    files = [f["arquivo"] for f in finals] + ([str(cover)] if cover and cover.exists() else [])
    if preset.get("arquivar_drive", True) and (services.get("drive_pasta_local") or services.get("gdrive_service_account")):
        services.enqueue("gdrive", "arquivar", {"arquivos": files, "data": today, "cliente": safe(preset["cliente"]),
                                                "projeto": safe(preset["projeto"]), "cut_id": cut_id})
    if services.get("base44_api_key"):
        plan = cut["plan"]
        spec = (v or {}).get("spec") or {}
        services.enqueue("base44", "upsert", {"entidade": "CorteFicha", "chave": "corte_id", "cut_id": cut_id, "episode_id": ep["id"], "dados": {
            "corte_id": f"ep{ep['id']}-c{cut['idx']}", "cliente": preset["cliente"], "projeto": preset["projeto"], "episodio": ep["title"],
            "numero_corte": cut["idx"], "titulo": plan.get("titulo"), "gancho": spec.get("gancho") or plan.get("gancho"),
            "formatos": [f["formato"] for f in finals], "duracao_s": finals[0]["duracao"], "lut": spec.get("lut") or plan.get("lut"),
            "experimental": bool(cut["experimental"]), "nota_ia": plan.get("nota"), "justificativa_ia": plan.get("justificativa"),
            "link_drive": "", "data": today}})
    maybe_finish_episode(ep["id"])


def maybe_finish_episode(ep_id):
    ep = db.one("SELECT * FROM episodes WHERE id=?", ep_id)
    open_ = db.one("SELECT COUNT(*) n FROM cuts WHERE episode_id=? AND status NOT IN ('finalizado','reprovado','erro')", ep_id)["n"]
    if open_:
        return
    finals = db.q("SELECT * FROM cuts WHERE episode_id=? AND status='finalizado' ORDER BY idx", ep_id)
    if not finals or ep["finished_at"]:
        return
    _pid, preset = preset_of(ep)
    db.update("episodes", ep_id, status="concluido", finished_at=db.now(), stage="Finais prontos")
    cost = services.episode_cost(ep_id)
    if services.get("base44_api_key"):
        services.enqueue("base44", "upsert", {"entidade": "EpisodioCliente", "chave": "episodio_id", "episode_id": ep_id, "dados": {
            "episodio_id": f"ep{ep_id}", "cliente": preset["cliente"], "projeto": preset["projeto"], "episodio": ep["title"],
            "episodio_completo": ep["source_path"], "cortes_aprovados": [f"ep{ep_id}-c{c['idx']}" for c in finals],
            "total_cortes": len(finals), "custo_estimado_usd": cost["total_usd"]}})
        rows = db.q("SELECT e.id, SUM(c.usd) usd FROM episodes e LEFT JOIN costs c ON c.episode_id=e.id WHERE e.status='concluido' GROUP BY e.id")
        media = round(sum(r["usd"] or 0 for r in rows) / max(1, len(rows)), 4)
        services.enqueue("base44", "upsert", {"entidade": "CustoEpisodio", "chave": "episodio_id", "dados": {
            "episodio_id": f"ep{ep_id}", "cliente": preset["cliente"], "custo_usd": cost["total_usd"],
            "detalhe": cost["itens"], "media_por_episodio_usd": media, "episodios_considerados": len(rows)}})
    notify.send("CorteX9 — finais prontos", f"{ep['title']}: {len(finals)} cortes renderizados em alta.")


def reveal(path):
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(path)])
