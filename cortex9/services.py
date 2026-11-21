"""Conexões externas: status, teste, chamadas, custo e fila de reenvio (outbox)."""
import glob
import json
import logging
import os
import shutil
import threading
import time
from pathlib import Path

import requests

from . import db, eventlog, secrets
from .errors import explain

log = logging.getLogger("cortex9.services")
HTTP_TIMEOUT = 60

# Campos: (id, rótulo, secreto?)
SERVICES = [
    {"id": "claude", "links": [("Gerar chave da Claude API", "https://console.anthropic.com/settings/keys")],
      "nome": "Claude API", "uso": "IA diretora, escolha dos cortes e revisão das legendas.",
     "campos": [("anthropic_api_key", "Chave da API (sk-ant-…)", True), ("claude_modelo", "Modelo", False)]},
    {"id": "openai", "links": [("Gerar chave da OpenAI", "https://platform.openai.com/api-keys")],
      "nome": "ChatGPT (OpenAI)", "uso": "Reserva da IA diretora se o Claude falhar.",
     "campos": [("openai_api_key", "Chave da API (sk-…)", True), ("openai_modelo", "Modelo", False)]},
    {"id": "elevenlabs", "links": [("Gerar chave da ElevenLabs", "https://elevenlabs.io/app/settings/api-keys")],
      "nome": "ElevenLabs", "uso": "Isolamento e reforço de voz quando o áudio está ruim.",
     "campos": [("elevenlabs_api_key", "Chave da API (xi-api-key)", True)]},
    {"id": "epidemic", "links": [("Pedir acesso à Partner API", "https://www.epidemicsound.com/partner-api/")],
      "nome": "Epidemic Sound", "uso": "Trilhas de fundo escolhidas pelo clima do corte.",
     "campos": [("epidemic_token", "Token de acesso (Partner API)", True), ("epidemic_base", "URL da API", False)]},
    {"id": "gdrive", "links": [("Baixar o Google Drive para desktop", "https://www.google.com/drive/download/"), ("Criar conta de serviço (links)", "https://console.cloud.google.com/iam-admin/serviceaccounts")],
      "nome": "Google Drive", "uso": "Ler o kit do projeto e arquivar finais em CorteX9/data/cliente/projeto.",
     "campos": [("drive_pasta_local", "Pasta do Google Drive no Mac", False),
                ("gdrive_service_account", "JSON da conta de serviço (opcional, gera links)", True),
                ("drive_pasta_id", "ID da pasta CorteX9 no Drive compartilhado (opcional)", False)]},
    {"id": "base44", "links": [("Abrir o Base44 para pegar a chave", "https://app.base44.com/")],
      "nome": "Base44 (API)", "uso": "Ficha técnica de cada corte aprovado, link do cliente e custo por episódio.",
     "campos": [("base44_api_key", "Chave da API do app", True), ("base44_app_id", "ID do app", False),
                ("base44_app_url", "URL pública do app", False)]},
    {"id": "celular", "links": [("Instalar o app ntfy no celular", "https://ntfy.sh/")],
      "nome": "Notificação no celular (ntfy)", "uso": "Avisa no celular quando as prévias ficam prontas.",
     "campos": [("ntfy_topico", "Tópico ntfy (app ntfy no iPhone/Android)", True)]},
]

DEFAULTS = {"claude_modelo": "claude-opus-5-5", "openai_modelo": "gpt-5",
            "epidemic_base": "https://partner-content-api.epidemicsound.com"}

# US$ por unidade (editável em Custos)
PRICES = {
    "claude:input_tokens": 4.00 / 1e6, "claude:output_tokens": 20.00 / 1e6,
    "openai:input_tokens": 1.25 / 1e6, "openai:output_tokens": 10.00 / 1e6,
    "elevenlabs:segundos": 0.0, "epidemic:downloads": 0.0,
}


LINK_URLS = {u for s in SERVICES for _a, u in s.get("links", [])}


def field(key):
    for s in SERVICES:
        for k, _l, secret in s["campos"]:
            if k == key:
                return secret
    raise KeyError(key)


def get(key):
    if field(key):
        return secrets.get(key)
    v = db.setting(key)
    if not v and key == "drive_pasta_local":
        v = detect_drive_folder()
    return v or DEFAULTS.get(key)


def set_values(values: dict):
    for k, v in values.items():
        if field(k):
            if v not in (None, "", "••••••"):  # campo mascarado = mantém
                secrets.set(k, v.strip())
            elif v == "":
                pass
        else:
            db.set_setting(k, (v or "").strip())


def clear_secret(key):
    secrets.delete(key)


def detect_drive_folder():
    for base in glob.glob(str(Path.home() / "Library" / "CloudStorage" / "GoogleDrive-*")):
        for sub in ("Meu Drive", "My Drive"):
            p = Path(base) / sub
            if p.exists():
                return str(p)
    return ""


# ---------------------------------------------------------------- custo
def price(service, unit):
    custom = db.setting("precos", {}) or {}
    return float(custom.get(f"{service}:{unit}", PRICES.get(f"{service}:{unit}", 0.0)))


def add_cost(episode_id, service, units, unit_type):
    usd = units * price(service, unit_type)
    db.insert("costs", episode_id=episode_id, service=service, units=units, unit_type=unit_type, usd=usd, created_at=db.now())
    return usd


def episode_cost(episode_id):
    rows = db.q("SELECT service, unit_type, SUM(units) units, SUM(usd) usd FROM costs WHERE episode_id=? GROUP BY service, unit_type", episode_id)
    return {"total_usd": round(sum(r["usd"] or 0 for r in rows), 4), "itens": rows}


# ---------------------------------------------------------------- status/teste
_status: dict[str, dict] = {}


def status_all():
    out = []
    for s in SERVICES:
        missing = [lbl for k, lbl, secret in s["campos"] if secret and not secrets.has(k) and k not in OPTIONAL]
        st = _status.get(s["id"])
        if missing and s["id"] not in ("gdrive",):
            state = {"estado": "faltando_chave", "mensagem": "Falta: " + ", ".join(missing)}
        elif st:
            state = st
        else:
            state = {"estado": "nao_testado", "mensagem": "Clique em Testar."}
        fields = []
        for k, lbl, secret in s["campos"]:
            if secret:
                fields.append({"key": k, "label": lbl, "secret": True, "value": "••••••" if secrets.has(k) else ""})
            else:
                fields.append({"key": k, "label": lbl, "secret": False, "value": get(k) or ""})
        out.append({**{k: s[k] for k in ("id", "nome", "uso")}, **state, "campos": fields,
                    "links": [{"rotulo": a, "url": b} for a, b in s.get("links", [])]})
    return {"servicos": out, "cofre": secrets.storage_name()}


OPTIONAL = {"gdrive_service_account"}


def test(service_id):
    try:
        msg = TESTS[service_id]()
        _status[service_id] = {"estado": "conectado", "mensagem": msg, "testado_em": time.time()}
    except Exception as e:  # noqa: BLE001
        err = explain(f"Conexão {service_id}", e)
        _status[service_id] = {"estado": "erro", "mensagem": f"{err.reason} {err.fix}", "detalhe": err.detail[-400:], "testado_em": time.time()}
    return _status[service_id]


def _need(key, nome):
    v = get(key)
    if not v:
        raise RuntimeError(f"401 chave ausente para {nome}")
    return v


def _t_claude():
    import anthropic
    c = anthropic.Anthropic(api_key=_need("anthropic_api_key", "Claude"))
    m = c.models.retrieve(get("claude_modelo"))
    return f"OK — modelo {m.id} disponível."


def _t_openai():
    r = requests.get("https://api.openai.com/v1/models", headers={"Authorization": f"Bearer {_need('openai_api_key', 'OpenAI')}"}, timeout=20)
    r.raise_for_status()
    return "OK — reserva pronta."


def _t_eleven():
    r = requests.get("https://api.elevenlabs.io/v1/user/subscription", headers={"xi-api-key": _need("elevenlabs_api_key", "ElevenLabs")}, timeout=20)
    r.raise_for_status()
    d = r.json()
    return f"OK — {d.get('character_count', 0)}/{d.get('character_limit', '?')} créditos usados."


def _t_epidemic():
    r = requests.get(f"{get('epidemic_base')}/v0/tracks/search", params={"term": "epic", "limit": 1},
                     headers={"Authorization": f"Bearer {_need('epidemic_token', 'Epidemic Sound')}"}, timeout=20)
    r.raise_for_status()
    return "OK — busca de trilhas respondendo."


def _t_drive():
    local = get("drive_pasta_local")
    msgs = []
    if local:
        p = Path(local)
        if not p.exists():
            raise RuntimeError(f"No such file or directory: {local}")
        test = p / "CorteX9" / ".teste_escrita"
        test.parent.mkdir(exist_ok=True)
        test.write_text("ok")
        test.unlink()
        msgs.append(f"pasta local OK ({local})")
    if secrets.has("gdrive_service_account"):
        tok = _drive_token()
        r = requests.get("https://www.googleapis.com/drive/v3/files", params={"pageSize": 1, "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"},
                         headers={"Authorization": f"Bearer {tok}"}, timeout=20)
        r.raise_for_status()
        msgs.append("API OK (links ativados)")
    if not msgs:
        raise RuntimeError("401 configure a pasta do Google Drive para desktop ou a conta de serviço")
    return "; ".join(msgs)


def _t_base44():
    r = requests.get(_b44_url("CorteFicha"), headers=_b44_headers(), params={"limit": 1}, timeout=20)
    r.raise_for_status()
    return "OK — entidade CorteFicha acessível."


def _t_ntfy():
    notify_phone("CorteX9", "Teste de conexão do celular ✅")
    return "OK — notificação de teste enviada."


TESTS = {"claude": _t_claude, "openai": _t_openai, "elevenlabs": _t_eleven,
         "epidemic": _t_epidemic, "gdrive": _t_drive, "base44": _t_base44, "celular": _t_ntfy}


# ---------------------------------------------------------------- ElevenLabs
def elevenlabs_isolate(wav_in: Path, wav_out: Path, episode_id=None):
    key = _need("elevenlabs_api_key", "ElevenLabs")
    with open(wav_in, "rb") as f:
        r = requests.post("https://api.elevenlabs.io/v1/audio-isolation", headers={"xi-api-key": key},
                          files={"audio": (wav_in.name, f, "audio/wav")}, timeout=600)
    r.raise_for_status()
    wav_out.write_bytes(r.content)
    from .ffmpeg import probe
    try:
        dur = probe(wav_in)["duration"]
    except Exception:  # noqa: BLE001
        dur = 0
    add_cost(episode_id, "elevenlabs", dur, "segundos")
    return wav_out


# ---------------------------------------------------------------- Epidemic Sound
def epidemic_track(mood: str, dest_dir: Path, episode_id=None):
    tok = _need("epidemic_token", "Epidemic Sound")
    h = {"Authorization": f"Bearer {tok}"}
    terms = {"épico": "epic cinematic", "epico": "epic cinematic", "intimista": "intimate acoustic", "noir": "dark noir",
             "sonho": "dreamy ambient", "saturado": "upbeat energetic", "dessaturado": "minimal calm", "tenso": "tension",
             "alegre": "happy upbeat", "inspirador": "inspiring"}
    r = requests.get(f"{get('epidemic_base')}/v0/tracks/search", params={"term": terms.get(mood, mood), "limit": 5}, headers=h, timeout=30)
    r.raise_for_status()
    tracks = r.json().get("tracks") or r.json().get("data") or []
    if not tracks:
        return None
    t = tracks[0]
    tid = t.get("id")
    dest = dest_dir / f"epidemic_{tid}.mp3"
    if dest.exists():
        return dest
    d = requests.get(f"{get('epidemic_base')}/v0/tracks/{tid}/download", params={"format": "mp3", "quality": "normal"}, headers=h, timeout=30)
    d.raise_for_status()
    url = d.json().get("url")
    audio = requests.get(url, timeout=120)
    audio.raise_for_status()
    dest.write_bytes(audio.content)
    add_cost(episode_id, "epidemic", 1, "downloads")
    return dest


# ---------------------------------------------------------------- Google Drive
def _drive_token():
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account
    info = json.loads(secrets.get("gdrive_service_account"))
    creds = service_account.Credentials.from_service_account_info(info, scopes=["https://www.googleapis.com/auth/drive"])
    creds.refresh(Request())
    return creds.token


def _drive_folder(tok, name, parent):
    h = {"Authorization": f"Bearer {tok}"}
    qy = f"name = '{name.replace(chr(39), ' ')}' and '{parent}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    r = requests.get("https://www.googleapis.com/drive/v3/files", params={"q": qy, "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}, headers=h, timeout=30)
    r.raise_for_status()
    if r.json()["files"]:
        return r.json()["files"][0]["id"]
    r = requests.post("https://www.googleapis.com/drive/v3/files", params={"supportsAllDrives": "true"}, headers=h, timeout=30,
                      json={"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent]})
    r.raise_for_status()
    return r.json()["id"]


def drive_archive(payload):
    """payload: arquivos[], data, cliente, projeto, destino (opcional, relativo ao Meu Drive).
    Copia para a pasta do Google Drive para desktop e/ou sobe pela API. Retorna links."""
    if payload.get("destino"):
        sub = [x for x in str(payload["destino"]).strip("/").split("/") if x]
    else:
        sub = ["CorteX9", payload["data"], payload["cliente"], payload["projeto"]]
    result = {"local": [], "links": []}
    local = get("drive_pasta_local")
    if local:
        dest = Path(local, *sub)
        dest.mkdir(parents=True, exist_ok=True)
        for f in payload["arquivos"]:
            target = dest / Path(f).name
            if not target.exists():
                shutil.copy2(f, target)
            result["local"].append(str(target))
    if secrets.has("gdrive_service_account") and get("drive_pasta_id"):
        tok = _drive_token()
        parent = get("drive_pasta_id")
        for s in (sub[1:] if sub and sub[0] == "CorteX9" else sub):  # drive_pasta_id já é a pasta CorteX9
            parent = _drive_folder(tok, s, parent)
        h = {"Authorization": f"Bearer {tok}"}
        for f in payload["arquivos"]:
            meta = {"name": Path(f).name, "parents": [parent]}
            init = requests.post("https://www.googleapis.com/upload/drive/v3/files", params={"uploadType": "resumable", "supportsAllDrives": "true", "fields": "id,webViewLink"},
                                 headers={**h, "Content-Type": "application/json; charset=UTF-8"}, json=meta, timeout=30)
            init.raise_for_status()
            with open(f, "rb") as fh:
                up = requests.put(init.headers["Location"], data=fh, headers={"Content-Type": "video/mp4"}, timeout=3600)
            up.raise_for_status()
            result["links"].append(up.json().get("webViewLink"))
    if not local and not result["links"]:
        raise RuntimeError("401 Google Drive não configurado")
    if payload.get("cut_id"):
        link = (result["links"] or [None])[0] or (result["local"] or [""])[0]
        db.update("cuts", payload["cut_id"], drive_link=link)
    if payload.get("photo_id"):
        from . import photos
        where = "/".join(sub)
        photos._drive_state(payload["photo_id"], "enviado", f"No Google Drive: {where}", result["links"] or result["local"])
    return result


# ---------------------------------------------------------------- Base44
def _b44_url(entity, rid=None):
    app = get("base44_app_id")
    if not app:
        raise RuntimeError("401 ID do app Base44 ausente")
    base = f"https://app.base44.com/api/apps/{app}/entities/{entity}"
    return f"{base}/{rid}" if rid else base


def _b44_headers():
    return {"api_key": _need("base44_api_key", "Base44"), "Content-Type": "application/json"}


def base44_upsert(payload):
    entity, data, key = payload["entidade"], payload["dados"], payload.get("chave")
    # campos que dependem de outras etapas são resolvidos na hora do envio
    if payload.get("cut_id"):
        c = db.one("SELECT drive_link FROM cuts WHERE id=?", payload["cut_id"])
        data["link_drive"] = (c or {}).get("drive_link") or data.get("link_drive", "")
    if payload.get("episode_id"):
        data.setdefault("custo_estimado_usd", episode_cost(payload["episode_id"])["total_usd"])
    rid = None
    if key:
        r = requests.get(_b44_url(entity), headers=_b44_headers(), params={"q": json.dumps({key: data[key]})}, timeout=30)
        if r.ok and isinstance(r.json(), list) and r.json():
            rid = r.json()[0].get("id")
    if rid:
        r = requests.put(_b44_url(entity, rid), headers=_b44_headers(), json=data, timeout=30)
    else:
        r = requests.post(_b44_url(entity), headers=_b44_headers(), json=data, timeout=30)
    r.raise_for_status()
    rec = r.json()
    rid = rec.get("id", rid)
    if entity == "EpisodioCliente" and payload.get("episode_id"):
        url = (get("base44_app_url") or "").rstrip("/")
        link = f"{url}/Episodio?id={rid}" if url else ""
        ep = db.one("SELECT meta FROM episodes WHERE id=?", payload["episode_id"])
        meta = ep["meta"] or {}
        meta["link_cliente"] = link
        db.update("episodes", payload["episode_id"], meta=meta)
    return {"id": rid}


# ---------------------------------------------------------------- celular
def notify_phone(title, msg):
    topic = secrets.get("ntfy_topico")
    if not topic:
        return False
    r = requests.post(f"https://ntfy.sh/{topic}", data=msg.encode(), headers={"Title": title.encode("utf-8").decode("latin-1", "ignore"), "Tags": "clapper"}, timeout=20)
    r.raise_for_status()
    return True


# ---------------------------------------------------------------- outbox (fila de reenvio)
HANDLERS = {
    ("gdrive", "arquivar"): drive_archive,
    ("base44", "upsert"): base44_upsert,
    ("celular", "notificar"): lambda p: notify_phone(p["titulo"], p["mensagem"]),
}


def enqueue(service, action, payload):
    db.insert("outbox", service=service, action=action, payload=payload, next_try_at=db.now(), created_at=db.now())


def outbox_tick():
    rows = db.q("SELECT * FROM outbox WHERE done=0 AND next_try_at<=? ORDER BY id LIMIT 5", db.now())
    for r in rows:
        try:
            res = HANDLERS[(r["service"], r["action"])](r["payload"])
            db.update("outbox", r["id"], done=1, last_error=None)
            kind, ref = _outbox_ref(r["payload"])
            eventlog.info(f"Envio {r['service']} ({r['action']}) concluído" + (f": {', '.join(res.get('links') or res.get('local') or [])}" if isinstance(res, dict) and r["service"] == "gdrive" else ""),
                          kind=kind, ref=ref, step=f"envio {r['service']}")
        except Exception as e:  # noqa: BLE001
            err = explain(f"Envio {r['service']}", e)
            att = r["attempts"] + 1
            delay = min(3600, 30 * (2 ** min(att, 7)))
            db.update("outbox", r["id"], attempts=att, last_error=json.dumps(err.as_dict(), ensure_ascii=False), next_try_at=db.now() + delay)
            kind, ref = _outbox_ref(r["payload"])
            eventlog.warn(f"Envio {r['service']} falhou (tentativa {att}, nova tentativa em {delay // 60:.0f} min): {err.reason} → {err.fix}",
                          kind=kind, ref=ref, step=f"envio {r['service']}", detail=err.detail)
            if r["service"] == "gdrive" and (r["payload"] or {}).get("photo_id"):
                from . import photos
                photos._drive_state(r["payload"]["photo_id"], "erro", f"Drive: {err.reason} {err.fix} (o app tenta de novo sozinho)")


def _outbox_ref(payload):
    payload = payload or {}
    if payload.get("photo_id"):
        return "fotos", payload["photo_id"]
    if payload.get("episode_id"):
        return "episodio", payload["episode_id"]
    if payload.get("cut_id"):
        c = db.one("SELECT episode_id FROM cuts WHERE id=?", payload["cut_id"])
        return "episodio", (c or {}).get("episode_id")
    return "app", None


def outbox_loop(stop: threading.Event):
    while not stop.wait(10):
        try:
            outbox_tick()
        except Exception:  # noqa: BLE001
            log.exception("outbox")
