"""IA diretora: analisa quadros + transcrição + preset e devolve o plano de edição (JSON).

Ordem: Claude (visão) → ChatGPT (reserva) → plano local (heurístico), para nunca travar a fila.
"""
import base64
import json
import logging
import random
import re
from pathlib import Path

from . import luts, services, style
from .errors import explain

log = logging.getLogger("cortex9.director")

LUT_IDS = list(luts.LUTS)
EFEITOS = ["zoom_punch", "zoom_lento", "flash_entrada", "fade_saida", "vinheta", "granulado", "letterbox", "glitch_entrada"]
ANIMACOES = ["pop", "digitar", "deslizar", "fade"]
ESTRUTURAS = ["linear", "cold_open"]
TRANS_IN = ["corte_seco", "flash", "fade_preto", "glitch"]
TRANS_OUT = ["corte_seco", "fade_preto"]

_num = {"type": "number"}
_str = {"type": "string"}


def _obj(props, required=None):
    return {"type": "object", "properties": props, "required": required or list(props), "additionalProperties": False}


VARIACAO = _obj({
    "nome": _str, "gancho": _str, "lut": {"type": "string", "enum": LUT_IDS}, "clima_trilha": _str,
    "efeitos": {"type": "array", "items": {"type": "string", "enum": EFEITOS}},
    "estrutura": {"type": "string", "enum": ESTRUTURAS}, "justificativa": _str})

CORTE = _obj({
    "inicio": _num, "fim": _num, "titulo": _str, "gancho": _str, "tema": _str, "nota": _num,
    "lut": {"type": "string", "enum": LUT_IDS}, "clima_trilha": _str,
    "musica_entra": _num, "musica_sai": _num,
    "zooms": {"type": "array", "items": _obj({"inicio": _num, "fim": _num, "intensidade": _num})},
    "textos": {"type": "array", "items": _obj({"inicio": _num, "fim": _num, "texto": _str,
                                               "animacao": {"type": "string", "enum": ANIMACOES}})},
    "efeitos": {"type": "array", "items": {"type": "string", "enum": EFEITOS}},
    "transicao_entrada": {"type": "string", "enum": TRANS_IN},
    "transicao_saida": {"type": "string", "enum": TRANS_OUT},
    "estrutura": {"type": "string", "enum": ESTRUTURAS},
    "experimental": {"type": "boolean"},
    "justificativa": _str,
    "variacoes": {"type": "array", "items": VARIACAO},
})

PLANO = _obj({
    "resumo_episodio": _str,
    "analise_cenas": {"type": "array", "items": _obj({"tempo": _num, "quem_fala": _str, "enquadramento": _str,
                                                       "expressao": _str, "energia": _num})},
    "cortes": {"type": "array", "items": CORTE},
})

SYSTEM = """Você é a diretora de edição do CorteX9, que transforma episódios (podcasts, entrevistas, lives gravadas no YoloBox) em cortes curtos para redes sociais em português do Brasil.
Você recebe quadros-chave do vídeo com o tempo de cada um, a transcrição com tempos, o kit do cliente e o histórico de aprovações. Analise as cenas (quem fala, enquadramento, expressão, energia) e devolva um plano de edição completo que o app executa sozinho, sem perguntar nada ao usuário.

Regras do plano:
- Escolha entre {cmin} e {cmax} cortes (menos só se o episódio for curto demais), sem sobreposição, cada um com {dmin} a {dmax} segundos. Cada corte começa e termina em frase completa, com começo forte e fim que fecha a ideia.
- "inicio"/"fim" são segundos absolutos no episódio. Tempos de "zooms", "textos", "musica_entra" e "musica_sai" são segundos RELATIVOS ao início do corte.
- "gancho": frase curta (até 8 palavras) que aparece na tela nos primeiros segundos. "nota": 0 a 10, potencial de viralizar.
- "lut": escolha a LUT pelo tema e clima do corte: epico, intimista, noir, sonho, saturado, dessaturado.
- Zooms em momentos de ênfase (intensidade 1.05 a 1.3). Textos animados só em frases de impacto.
- Exatamente um corte (ou mais, se fizer sentido) deve ter "experimental": true: o "corte criativo" que sai do padrão do cliente em estilo, estrutura (ex.: cold_open, que começa pelo clímax) ou efeito.
- Para cada corte, gere de 2 a 3 "variacoes" com gancho, clima de trilha e efeitos diferentes; pelo menos uma variação usa uma LUT diferente da principal. A primeira variação deve ser a versão principal do corte.
- "justificativa": 1 ou 2 frases explicando por que escolheu esse trecho e essas decisões (o usuário vai ler).
- Use o histórico de aprovações como referência de gosto do cliente: repita o que foi aprovado, evite o que foi reprovado.
"""


def _transcript_text(tr, max_chars=180_000):
    lines = [f"[{s['start']:.1f}-{s['end']:.1f}] {s['text']}" for s in tr.get("segments", [])]
    out = "\n".join(lines)
    return out if len(out) <= max_chars else out[:max_chars] + "\n[...transcrição longa: restante omitido...]"


def _user_content(frames, tr, preset, meta, style_text):
    content = []
    for f in frames:
        content.append({"type": "text", "text": f"Quadro em {f['t']:.1f}s"})
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                   "data": base64.standard_b64encode(Path(f["path"]).read_bytes()).decode()}})
    kit = {k: preset.get(k) for k in ("cliente", "projeto", "formatos", "efeitos", "legenda", "contexto", "cortes_min", "cortes_max", "duracao_min", "duracao_max")}
    content.append({"type": "text", "text":
                    f"Duração do episódio: {meta['duration']:.1f}s.\n\nKit do cliente (preset):\n{json.dumps(kit, ensure_ascii=False)}\n\n"
                    f"Histórico de aprovações (biblioteca de estilo):\n{style_text}\n\nTranscrição:\n{_transcript_text(tr)}\n\n"
                    "Devolva o plano de edição."})
    return content


def _system(preset):
    return SYSTEM.format(cmin=preset.get("cortes_min", 10), cmax=preset.get("cortes_max", 15),
                         dmin=preset.get("duracao_min", 20), dmax=preset.get("duracao_max", 75))


def _claude(system, content, schema, episode_id, effort="high", max_tokens=32000):
    import anthropic
    key = services.get("anthropic_api_key")
    if not key:
        raise RuntimeError("401 chave do Claude ausente")
    client = anthropic.Anthropic(api_key=key, timeout=900, max_retries=3)
    with client.beta.messages.stream(
        model=services.get("claude_modelo"),
        max_tokens=max_tokens,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",  # se o modelo recusar por política, a API refaz no modelo reserva
        thinking={"type": "adaptive"},
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        system=system,
        messages=[{"role": "user", "content": content}],
    ) as stream:
        msg = stream.get_final_message()
    services.add_cost(episode_id, "claude", msg.usage.input_tokens, "input_tokens")
    services.add_cost(episode_id, "claude", msg.usage.output_tokens, "output_tokens")
    if msg.stop_reason == "refusal":
        raise RuntimeError("Claude recusou o pedido")
    if msg.stop_reason == "max_tokens":
        raise RuntimeError("Resposta do Claude cortada (max_tokens)")
    text = next(b.text for b in msg.content if b.type == "text")
    return json.loads(text)


def _openai(system, content, schema, episode_id):
    import requests
    key = services.get("openai_api_key")
    if not key:
        raise RuntimeError("401 chave da OpenAI ausente")
    parts = []
    for c in content:
        if c["type"] == "text":
            parts.append({"type": "text", "text": c["text"]})
        else:
            parts.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{c['source']['data']}", "detail": "low"}})
    r = requests.post("https://api.openai.com/v1/chat/completions", timeout=900, headers={"Authorization": f"Bearer {key}"}, json={
        "model": services.get("openai_modelo"),
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": parts}],
        "response_format": {"type": "json_schema", "json_schema": {"name": "plano", "strict": True, "schema": schema}},
    })
    r.raise_for_status()
    d = r.json()
    u = d.get("usage", {})
    services.add_cost(episode_id, "openai", u.get("prompt_tokens", 0), "input_tokens")
    services.add_cost(episode_id, "openai", u.get("completion_tokens", 0), "output_tokens")
    return json.loads(d["choices"][0]["message"]["content"])


# ------------------------------------------------------------------ plano principal
def make_plan(episode_id, frames, tr, preset, meta, preset_id=None):
    """Retorna (plano, motor, avisos)."""
    style_text = style.as_prompt(preset_id)
    system = _system(preset)
    content = _user_content(frames, tr, preset, meta, style_text)
    warnings = []
    for name, fn in (("Claude", _claude), ("ChatGPT (reserva)", _openai)):
        try:
            plan = fn(system, content, PLANO, episode_id)
            plan = sanitize(plan, tr, preset, meta)
            if plan["cortes"]:
                return plan, name, warnings
            warnings.append(f"{name} devolveu um plano vazio.")
        except Exception as e:  # noqa: BLE001
            err = explain(f"IA diretora ({name})", e)
            warnings.append(f"{name}: {err.reason} {err.fix}")
            log.warning("%s falhou: %s", name, e)
    plan = sanitize(heuristic_plan(tr, preset, meta, preset_id), tr, preset, meta)
    return plan, "Plano local (sem IA na nuvem)", warnings


def _snap(t, tr, kind):
    """Ajusta o tempo para a borda da frase mais próxima."""
    segs = tr.get("segments") or []
    if not segs:
        return t
    key = "start" if kind == "start" else "end"
    best = min(segs, key=lambda s: abs(s[key] - t))
    return best[key] if abs(best[key] - t) < 3 else t


def sanitize(plan, tr, preset, meta):
    dur = meta["duration"]
    dmin, dmax = preset.get("duracao_min", 20), preset.get("duracao_max", 75)
    out, used = [], []
    for c in sorted(plan.get("cortes", []), key=lambda c: -float(c.get("nota", 0))):
        a = max(0.0, float(c.get("inicio", 0)))
        b = min(dur, float(c.get("fim", 0)))
        a, b = _snap(a, tr, "start"), _snap(b, tr, "end")
        if b - a < min(5, dmin) or any(a < y and b > x for x, y in used):
            continue
        if b - a > dmax * 1.2:
            b = a + dmax
        used.append((a, b))
        L = b - a
        c["inicio"], c["fim"] = round(a, 2), round(b, 2)
        c["lut"] = c.get("lut") if c.get("lut") in LUT_IDS else "epico"
        c["musica_entra"] = max(0.0, min(L, float(c.get("musica_entra", 0))))
        c["musica_sai"] = max(c["musica_entra"], min(L, float(c.get("musica_sai", L)) or L))
        c["zooms"] = [z for z in c.get("zooms", []) if 0 <= z["inicio"] < z["fim"] <= L][:8]
        for z in c["zooms"]:
            z["intensidade"] = max(1.02, min(1.35, float(z.get("intensidade", 1.12))))
        c["textos"] = [x for x in c.get("textos", []) if 0 <= x["inicio"] < x["fim"] <= L][:6]
        c["efeitos"] = [e for e in c.get("efeitos", []) if e in EFEITOS]
        c.setdefault("estrutura", "linear")
        c.setdefault("transicao_entrada", "corte_seco")
        c.setdefault("transicao_saida", "fade_preto")
        c.setdefault("experimental", False)
        vs = [v for v in c.get("variacoes", []) if isinstance(v, dict)][:3]
        if not vs:
            vs = [{"nome": "Principal", "gancho": c.get("gancho", ""), "lut": c["lut"], "clima_trilha": c.get("clima_trilha", ""),
                   "efeitos": c["efeitos"], "estrutura": c["estrutura"], "justificativa": "Versão principal do plano."}]
        want = max(2, min(3, int(preset.get("variacoes", 3))))
        while len(vs) < want:
            vs.append(_alt_variation(c, len(vs)))
        if all(v.get("lut") == c["lut"] for v in vs):  # garante a LUT como variação criativa
            vs[-1]["lut"] = _other_lut(c["lut"], c.get("tema", ""))
            vs[-1]["nome"] = (vs[-1].get("nome") or "Variação") + " · LUT " + vs[-1]["lut"]
        for v in vs:
            v["lut"] = v.get("lut") if v.get("lut") in LUT_IDS else c["lut"]
            v["efeitos"] = [e for e in v.get("efeitos", []) if e in EFEITOS]
            v["estrutura"] = v.get("estrutura") if v.get("estrutura") in ESTRUTURAS else "linear"
        c["variacoes"] = vs
        out.append(c)
        if len(out) >= preset.get("cortes_max", 15):
            break
    out.sort(key=lambda c: c["inicio"])
    if out and not any(c.get("experimental") for c in out):  # sempre pelo menos um corte ousado
        mk_experimental(out[len(out) // 2])
    plan["cortes"] = out
    return plan


def mk_experimental(c):
    c["experimental"] = True
    c["estrutura"] = "cold_open"
    c["lut"] = "noir" if c.get("lut") != "noir" else "sonho"
    c["efeitos"] = list(dict.fromkeys(["glitch_entrada", "letterbox", "granulado", "zoom_lento"]))
    c["transicao_entrada"] = "glitch"
    c["justificativa"] = (c.get("justificativa", "") + " Corte criativo: começa pelo clímax (cold open), visual noir e letterbox, fora do padrão do cliente.").strip()
    for i, v in enumerate(c.get("variacoes", [])):
        v["estrutura"] = "cold_open"
        if i == 0:
            v["lut"], v["efeitos"] = c["lut"], list(c["efeitos"])
            v["nome"] = "Criativo (noir)" if c["lut"] == "noir" else "Criativo"
        else:
            v["efeitos"] = list(dict.fromkeys((v.get("efeitos") or []) + ["glitch_entrada", "letterbox"]))


def _other_lut(cur, tema=""):
    t = (tema or "").lower()
    pref = "intimista" if any(w in t for w in ("família", "amor", "história", "emoção", "fé")) else \
        "saturado" if any(w in t for w in ("humor", "engraçad", "energia", "dica")) else "noir" if "polêm" in t else "dessaturado"
    return pref if pref != cur else next(x for x in LUT_IDS if x != cur)


def _alt_variation(c, i):
    moods = ["épico", "intimista", "tenso", "inspirador", "alegre"]
    effs = [["zoom_punch", "flash_entrada", "fade_saida"], ["zoom_lento", "vinheta", "fade_saida"], ["glitch_entrada", "granulado", "letterbox"]]
    return {"nome": f"Variação {i + 1}", "gancho": c.get("gancho", "").upper() if i == 1 else c.get("titulo", c.get("gancho", "")),
            "lut": _other_lut(c["lut"], c.get("tema", "")) if i == 1 else c["lut"], "clima_trilha": moods[(i + 1) % len(moods)],
            "efeitos": effs[i % len(effs)], "estrutura": "linear", "justificativa": "Variação gerada para comparar gancho, trilha e efeitos."}


# ------------------------------------------------------------------ plano local (sem nuvem)
HOOK_WORDS = re.compile(r"\b(nunca|ninguém|segredo|verdade|erro|maior|melhor|pior|dinheiro|como|por que|porque|ninguem|sempre|primeira|deus|medo)\b", re.I)


def heuristic_plan(tr, preset, meta, preset_id=None):
    segs = tr.get("segments") or []
    dur = meta["duration"]
    dmin, dmax = preset.get("duracao_min", 20), preset.get("duracao_max", 75)
    target = style.preferred_duration(preset_id, dmin, dmax)
    if not segs:  # sem fala: fatias uniformes
        n = max(1, min(preset.get("cortes_max", 15), int(dur // max(target, 5))))
        step = dur / n
        segs = [{"start": i * step, "end": min(dur, i * step + min(target, step)), "text": ""} for i in range(n)]
    cands = []
    for i, s in enumerate(segs):
        a, words, text, j = s["start"], 0, [], i
        while j < len(segs) and segs[j]["end"] - a < target:
            text.append(segs[j]["text"])
            words += len(segs[j]["text"].split())
            j += 1
        if j < len(segs) and segs[j]["end"] - a <= dmax:
            text.append(segs[j]["text"])
            j += 1
        b = segs[j - 1]["end"] if j > i else s["end"]
        if b - a < min(dmin, dur * 0.8):
            continue
        t = " ".join(text)
        score = 4 + min(3, words / max(1, b - a)) + min(2, len(HOOK_WORDS.findall(t)) * 0.5) + (0.5 if "?" in t else 0) + (0.5 if "!" in t else 0)
        cands.append({"inicio": a, "fim": b, "texto": t, "nota": round(min(10, score), 1)})
    cands.sort(key=lambda c: -c["nota"])
    chosen = []
    for c in cands:
        if not any(c["inicio"] < x["fim"] and c["fim"] > x["inicio"] for x in chosen):
            chosen.append(c)
        if len(chosen) >= preset.get("cortes_max", 15):
            break
    cortes = []
    rnd = random.Random(int(dur))
    for k, c in enumerate(sorted(chosen, key=lambda c: c["inicio"])):
        first = re.split(r"(?<=[.!?])\s", c["texto"].strip())[0] if c["texto"] else f"Corte {k + 1}"
        hook = " ".join(first.split()[:8]).rstrip(",.;") or f"Corte {k + 1}"
        L = c["fim"] - c["inicio"]
        lut = rnd.choice(["epico", "intimista", "saturado", "dessaturado"])
        zooms = [{"inicio": round(L * f, 1), "fim": round(min(L, L * f + 2.5), 1), "intensidade": 1.12} for f in (0.25, 0.6)]
        cortes.append({
            "inicio": c["inicio"], "fim": c["fim"], "titulo": hook, "gancho": hook, "tema": "geral", "nota": c["nota"],
            "lut": lut, "clima_trilha": "inspirador", "musica_entra": 0.0, "musica_sai": L, "zooms": zooms,
            "textos": [], "efeitos": list(preset.get("efeitos") or ["zoom_punch", "flash_entrada", "fade_saida"]),
            "transicao_entrada": "flash", "transicao_saida": "fade_preto", "estrutura": "linear", "experimental": False,
            "justificativa": f"Escolhido pelo plano local: densidade de fala e palavras de impacto (nota {c['nota']}).",
            "variacoes": [],
        })
    return {"resumo_episodio": "Plano gerado localmente (IA na nuvem indisponível).", "analise_cenas": [], "cortes": cortes}


# ------------------------------------------------------------------ ajuste rápido de um corte
def replan_cut(cut_plan, note, tr, preset, meta, episode_id):
    schema = CORTE
    seg_text = "\n".join(f"[{s['start']:.1f}-{s['end']:.1f}] {s['text']}" for s in tr.get("segments", [])
                         if s["end"] > cut_plan["inicio"] - 30 and s["start"] < cut_plan["fim"] + 30)
    content = [{"type": "text", "text": f"Plano atual do corte:\n{json.dumps(cut_plan, ensure_ascii=False)}\n\n"
                f"Transcrição ao redor:\n{seg_text}\n\nPedido de ajuste do usuário: {note}\n\nDevolva o corte ajustado."}]
    for fn in (_claude, _openai):
        try:
            c = fn(_system(preset), content, schema, episode_id)
            return sanitize({"cortes": [c]}, tr, dict(preset, cortes_max=1), meta)["cortes"][0], None
        except Exception as e:  # noqa: BLE001
            log.warning("ajuste via IA falhou: %s", e)
    return _local_adjust(cut_plan, note), "Ajuste aplicado localmente (IA indisponível)."


def _local_adjust(c, note):
    c = json.loads(json.dumps(c))
    n = note.lower()
    L = c["fim"] - c["inicio"]
    if "curt" in n:
        c["fim"] = c["inicio"] + L * 0.75
    if "long" in n:
        c["fim"] = c["fim"] + L * 0.25
    if "sem música" in n or "sem musica" in n or "sem trilha" in n:
        c["clima_trilha"] = "nenhuma"
    if "lut" in n or "cor" in n:
        c["lut"] = _other_lut(c["lut"])
    if "sem efeito" in n:
        c["efeitos"] = []
    if "zoom" in n and "sem" in n:
        c["zooms"] = []
    m = re.search(r"gancho[:\s]+[\"“]?([^\"”]+)", note, re.I)
    if m:
        c["gancho"] = m.group(1).strip()
        for v in c.get("variacoes", []):
            v["gancho"] = c["gancho"]
    return c


# ------------------------------------------------------------------ revisão de legendas
def review_captions(lines, episode_id, context=""):
    """Corrige ortografia/pontuação/nomes mantendo o mesmo número de linhas."""
    if not lines:
        return lines, None
    schema = _obj({"linhas": {"type": "array", "items": _str}})
    content = [{"type": "text", "text": "Revise estas legendas em português do Brasil: corrija ortografia, acentos, pontuação e nomes próprios. "
                "Não reescreva o estilo, não junte nem separe linhas: devolva exatamente o mesmo número de linhas, na mesma ordem.\n"
                f"Contexto do cliente: {context}\n\n" + json.dumps(lines, ensure_ascii=False)}]
    for fn, kw in ((_claude, {"effort": "low", "max_tokens": 16000}), (_openai, {})):
        try:
            out = fn("Você revisa legendas de vídeo.", content, schema, episode_id, **kw)["linhas"]
            if len(out) == len(lines):
                return out, None
        except Exception as e:  # noqa: BLE001
            log.warning("revisão de legendas falhou: %s", e)
    return lines, "Legendas sem revisão da IA (serviço indisponível)."
