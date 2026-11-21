"""Biblioteca de estilo: aprendizado com aprovações e reprovações."""
from collections import Counter

from . import db


def record(cut, variation_spec, decision, note=""):
    plan = cut["plan"] or {}
    spec = variation_spec or {}
    db.insert("style_library", preset_id=cut.get("preset_id"), cut_id=cut["id"], decision=decision,
              hook=spec.get("gancho") or plan.get("gancho"), duration=float(plan.get("fim", 0)) - float(plan.get("inicio", 0)),
              music_mood=spec.get("clima_trilha") or plan.get("clima_trilha"), lut=spec.get("lut") or plan.get("lut"),
              effects=spec.get("efeitos") or plan.get("efeitos") or [], ai_score=plan.get("nota"),
              experimental=1 if cut.get("experimental") else 0, note=note, created_at=db.now())


def summary(preset_id=None, limit=200):
    rows = db.q("SELECT * FROM style_library WHERE (? IS NULL OR preset_id=? OR preset_id IS NULL) ORDER BY id DESC LIMIT ?",
                preset_id, preset_id, limit)
    rows = [r for r in rows if r["decision"] != "variacao_preterida"]
    ok = [r for r in rows if r["decision"] == "aprovado"]
    bad = [r for r in rows if r["decision"] == "reprovado"]

    def avg(xs):
        xs = [x for x in xs if x]
        return round(sum(xs) / len(xs), 1) if xs else None

    s = {
        "total": len(rows), "aprovados": len(ok), "reprovados": len(bad),
        "duracao_media_aprovada": avg([r["duration"] for r in ok]),
        "duracao_media_reprovada": avg([r["duration"] for r in bad]),
        "luts_aprovadas": Counter(r["lut"] for r in ok if r["lut"]).most_common(3),
        "luts_reprovadas": Counter(r["lut"] for r in bad if r["lut"]).most_common(3),
        "trilhas_aprovadas": Counter(r["music_mood"] for r in ok if r["music_mood"]).most_common(3),
        "efeitos_aprovados": Counter(e for r in ok for e in (r["effects"] or [])).most_common(5),
        "efeitos_reprovados": Counter(e for r in bad for e in (r["effects"] or [])).most_common(5),
        "ganchos_aprovados": [r["hook"] for r in ok if r["hook"]][:8],
        "ganchos_reprovados": [r["hook"] for r in bad if r["hook"]][:5],
        "notas_de_ajuste": [r["note"] for r in rows if r["note"]][:8],
        "experimentais_aprovados": sum(1 for r in ok if r["experimental"]),
        "experimentais_total": sum(1 for r in rows if r["experimental"] and r["decision"] in ("aprovado", "reprovado")),
    }
    return s


def as_prompt(preset_id=None):
    s = summary(preset_id)
    if not s["total"]:
        return "Ainda não há histórico de aprovações para este cliente."
    lines = [f"Histórico: {s['aprovados']} cortes aprovados, {s['reprovados']} reprovados."]
    if s["duracao_media_aprovada"]:
        extra = f" (reprovada: {s['duracao_media_reprovada']}s)" if s["duracao_media_reprovada"] else ""
        lines.append(f"Duração média aprovada: {s['duracao_media_aprovada']}s{extra}.")
    for k, lbl in (("luts_aprovadas", "LUTs aprovadas"), ("luts_reprovadas", "LUTs reprovadas"), ("trilhas_aprovadas", "Climas de trilha aprovados"),
                   ("efeitos_aprovados", "Efeitos aprovados"), ("efeitos_reprovados", "Efeitos reprovados")):
        if s[k]:
            lines.append(f"{lbl}: " + ", ".join(f"{a} ({b})" for a, b in s[k]))
    if s["ganchos_aprovados"]:
        lines.append("Ganchos que o cliente aprovou: " + " | ".join(s["ganchos_aprovados"]))
    if s["ganchos_reprovados"]:
        lines.append("Ganchos reprovados: " + " | ".join(s["ganchos_reprovados"]))
    if s["notas_de_ajuste"]:
        lines.append("Pedidos de ajuste anteriores: " + " | ".join(s["notas_de_ajuste"]))
    lines.append(f"Cortes experimentais aprovados: {s['experimentais_aprovados']} de {s['experimentais_total']}.")
    return "\n".join(lines)


def preferred_duration(preset_id, lo, hi):
    s = summary(preset_id)
    d = s["duracao_media_aprovada"]
    return max(lo, min(hi, d)) if d else (lo + hi) / 2
