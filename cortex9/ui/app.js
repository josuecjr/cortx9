"use strict";
const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtT = (s) => { s = Math.max(0, Math.round(s || 0)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; };
const fmtDate = (t) => t ? new Date(t * 1000).toLocaleString("pt-BR", { dateStyle: "short", timeStyle: "short" }) : "";
const usd = (v) => `US$ ${(v || 0).toFixed(v < 1 ? 4 : 2)}`;

async function call(name, args = {}) {
  const r = await fetch(`/api/${name}`, { method: "POST", headers: { "X-Token": window.C9_TOKEN, "Content-Type": "application/json" }, body: JSON.stringify(args) });
  const j = await r.json();
  if (!j.ok) { toast(`${j.erro.motivo} ${j.erro.como_resolver || ""}`, true); throw j.erro; }
  return j.data;
}
function toast(msg, bad = false) {
  const d = document.createElement("div"); d.className = bad ? "bad" : ""; d.textContent = msg;
  $("#toast").appendChild(d); setTimeout(() => d.remove(), bad ? 9000 : 4000);
}
function errBox(e) {
  if (!e) return "";
  return `<div class="err"><b>${esc(e.etapa ? e.etapa + ": " : "")}${esc(e.motivo)}</b><div>${esc(e.como_resolver || "")}</div>${e.detalhe ? `<details><summary>detalhe técnico</summary><pre style="white-space:pre-wrap">${esc(e.detalhe)}</pre></details>` : ""}</div>`;
}

const STATUS = {
  na_fila: ["Na fila", ""], processando: ["Processando", "info"], gerando_previas: ["Gerando prévias", "info"],
  aguardando_aprovacao: ["Aguardando aprovação", "acc"], renderizando_finais: ["Renderizando finais", "info"],
  concluido: ["Concluído", "ok"], erro: ["Erro", "bad"],
  gerando_previa: ["Gerando prévia", "info"], previa_pronta: ["Prévia pronta", "acc"], aprovado: ["Aprovado · na fila do final", "ok"],
  renderizando_final: ["Renderizando final", "info"], finalizado: ["Final pronto", "ok"], reprovado: ["Reprovado", "bad"], ajustando: ["Ajustando", "info"],
};
const pill = (s) => { const [t, c] = STATUS[s] || [s, ""]; return `<span class="pill ${c}">${esc(t)}</span>`; };

let view = "fila", currentEp = null, timer = null;
const ui = { varSel: {}, cutSig: {} };

document.querySelectorAll("nav button").forEach((b) => b.onclick = () => { if (b.dataset.view === "fotos") { ui.foto = null; ui.fsig = null; } go(b.dataset.view); });
function go(v, ep) {
  view = v; if (ep !== undefined) currentEp = ep;
  document.querySelectorAll("nav button").forEach((b) => b.classList.toggle("on", b.dataset.view === v));
  ui.cutSig = {}; $("#main").innerHTML = ""; render();
}
async function render() {
  clearTimeout(timer);
  try { await VIEWS[view](); } catch (e) { console.error(e); }
  if (["fila", "aprovacao", "fotos"].includes(view)) timer = setTimeout(render, 2500);
}
async function sysInfo() {
  try {
    const s = await call("state");
    $("#ver").textContent = `v${s.versao}`;
    const q = {}; s.fila.forEach((r) => { q[r.lane] = q[r.lane] || {}; q[r.lane][r.status] = r.n; });
    const n = (l, st) => (q[l] || {})[st] || 0;
    $("#sys").innerHTML = `Fila: <b>${n("analise", "running") + n("previa", "running") + n("final", "running")}</b> rodando · <b>${n("analise", "queued") + n("previa", "queued") + n("final", "queued")}</b> esperando<br>
      Envios pendentes: <b>${s.envios_pendentes}</b><br>Whisper: <b>${esc(s.whisper || "não instalado")}</b><br>Render: <b>${s.hw ? "VideoToolbox (GPU)" : "software"}</b>`;
    return s;
  } catch { return null; }
}

const VIEWS = {
  // ------------------------------------------------------------ FILA
  async fila() {
    const s = await sysInfo(); if (!s) return;
    const eps = s.episodios.map((e) => {
      const c = e.cortes || {}; const tot = Object.values(c).reduce((a, b) => a + b, 0);
      return `<div class="card ep" data-ep="${e.id}">
        <div><div class="t">${esc(e.titulo)}</div><div class="m">#${e.id} · kit ${esc(e.preset || "")} · entrou ${fmtDate(e.criado)}${e.previas_em ? ` · prévias em ${Math.round((e.previas_em - e.criado) / 60)} min` : ""}</div>
          ${e.erro ? errBox(e.erro) : ""}${(e.avisos || []).slice(0, 2).map((a) => `<div class="warn">${esc(a)}</div>`).join("")}</div>
        <div><div class="m" style="margin-bottom:6px">${esc(e.etapa || "")}</div><div class="bar"><i style="width:${Math.round((e.progresso || 0) * 100)}%"></i></div>
          <div class="m" style="margin-top:6px">${tot ? `${tot} cortes · ${c.finalizado || 0} finais · ${c.reprovado || 0} reprovados` : ""}</div></div>
        <div class="row">${pill(e.status)}${e.status === "erro" ? `<button class="btn sm" data-retry="${e.id}">Tentar de novo</button>` : ""}</div></div>`;
    }).join("");
    $("#main").innerHTML = `<div class="row"><div><h1>Fila de episódios</h1><p class="sub">Solte o arquivo da câmera em ${s.pastas.map((p) => `<code>${esc(p)}</code>`).join(", ") || "uma pasta vigiada"} — o resto é automático.</p></div><div class="spacer"></div>
      <button class="btn" id="addf">Adicionar arquivo…</button><button class="btn" id="demo">Gerar episódio de teste</button></div>
      <div class="list">${eps || `<div class="empty">Nenhum episódio ainda.<br>Copie um vídeo para a pasta vigiada ou clique em “Gerar episódio de teste”.</div>`}</div>`;
    document.querySelectorAll("[data-ep]").forEach((el) => el.onclick = (ev) => { if (ev.target.closest("button")) return; go("aprovacao", +el.dataset.ep); });
    document.querySelectorAll("[data-retry]").forEach((b) => b.onclick = async () => { await call("retry", { episode_id: +b.dataset.retry }); toast("Reenviado para a fila."); });
    $("#demo").onclick = async () => { $("#demo").disabled = true; toast("Gerando episódio de teste… (leva ~1 min)"); try { const p = await call("demo"); toast(`Episódio de teste solto em ${p}`); } finally { $("#demo").disabled = false; } };
    $("#addf").onclick = async () => { let p = await call("pick", { kind: "file" }); if (!p) p = prompt("Caminho do arquivo de vídeo:"); if (p) { await call("add_file", { path: p }); toast("Episódio adicionado à fila."); } };
  },

  // ------------------------------------------------------------ APROVAÇÃO
  async aprovacao() {
    if (!currentEp) {
      const s = await sysInfo(); const e = s && s.episodios.find((x) => ["aguardando_aprovacao", "gerando_previas", "renderizando_finais"].includes(x.status)) || (s && s.episodios[0]);
      if (!e) { $("#main").innerHTML = `<div class="empty">Nenhum episódio para aprovar.</div>`; return; }
      currentEp = e.id;
    } else sysInfo();
    const ep = await call("episode", { id: currentEp });
    if (!ep) { currentEp = null; return; }
    ui.fmt = ep.formato;
    const pend = ep.cortes.filter((c) => c.status === "previa_pronta").length;
    const head = `<div class="ephead"><div class="row"><div><h1>${esc(ep.titulo)}</h1>
      <div class="sub" style="margin:0">${pill(ep.status)} &nbsp;${esc(ep.etapa || "")} · plano: ${esc(ep.motor || "—")} · custo ${usd(ep.custo.total_usd)}${ep.previas_em ? ` · prévias em ${Math.round((ep.previas_em - ep.criado) / 60)} min` : ""}${ep.link_cliente ? ` · <a href="${esc(ep.link_cliente)}" target="_blank" style="color:var(--acc)">link do cliente</a>` : ""}</div></div>
      <div class="spacer"></div><button class="btn" id="retryall">Tentar de novo os erros</button>
      <button class="btn pri" id="apall" ${pend ? "" : "disabled"}>Aprovar tudo e renderizar (${pend})</button></div>
      ${ep.erro ? errBox(ep.erro) : ""}${(ep.avisos || []).map((a) => `<div class="warn">${esc(a)}</div>`).join("")}
      ${ep.resumo ? `<details class="why" style="margin-top:8px"><summary>Resumo e análise de cenas da IA diretora</summary><p>${esc(ep.resumo)}</p>${(ep.cenas || []).map((c) => `<div>• ${fmtT(c.tempo)} — ${esc(c.quem_fala)}, ${esc(c.enquadramento)}, ${esc(c.expressao)}, energia ${c.energia}</div>`).join("")}</details>` : ""}
      <div class="bar" style="margin-top:10px"><i style="width:${Math.round((ep.progresso || 0) * 100)}%"></i></div></div>`;
    let root = $("#aprov");
    if (!root || root.dataset.ep != ep.id) {
      $("#main").innerHTML = `<div id="aprov" data-ep="${ep.id}"><div id="eph"></div><div class="grid" id="grid"></div></div>`;
      root = $("#aprov"); ui.cutSig = {};
    }
    $("#eph").innerHTML = head;
    $("#apall").onclick = async () => { if (!confirm(`Aprovar ${pend} cortes (variação selecionada em cada um) e renderizar em alta?`)) return; const n = await call("approve_all", { episode_id: ep.id }); toast(`${n} cortes aprovados — finais na fila.`); render(); };
    $("#retryall").onclick = async () => { const n = await call("retry", { episode_id: ep.id }); toast(n ? `${n} etapas voltaram para a fila.` : "Nenhum erro para repetir."); };
    if (!ep.cortes.length) { $("#grid").innerHTML = `<div class="empty" style="grid-column:1/-1">Os cortes aparecem aqui assim que a IA diretora terminar o plano.</div>`; return; }
    const grid = $("#grid");
    ep.cortes.forEach((c) => {
      const sig = JSON.stringify(c);
      if (ui.cutSig[c.id] === sig) return;
      ui.cutSig[c.id] = sig;
      let el = grid.querySelector(`[data-cut="${c.id}"]`);
      if (!el) { el = document.createElement("div"); el.dataset.cut = c.id; grid.appendChild(el); }
      cutCard(el, c);
    });
  },

  // ------------------------------------------------------------ VÍDEO DE FOTOS
  async fotos() {
    sysInfo();
    if (ui.foto) return fotoDetail(ui.foto);
    if ($("#fform")) { await fotoList(); return; }
    const [d, pr] = await Promise.all([call("photo_projects"), call("presets")]);
    $("#main").innerHTML = `<div class="row"><div><h1>Vídeo de fotos</h1><p class="sub">Escolha a pasta e a trilha. A IA enquadra sem distorcer, organiza, anima cada foto no ritmo da música e cria títulos e motion graphics.<br>
      Atalho “largar e esquecer”: solte uma <b>subpasta de fotos</b> (com um MP3 dentro, se quiser) numa pasta vigiada.</p></div></div>
      <div class="card" id="fform"><div class="form">
        <div class="full"><label>Pasta de fotos</label><div class="row" style="flex-wrap:nowrap"><input id="f_pasta" placeholder="/Users/voce/Pictures/Casamento Ana e Léo"><button class="btn sm" data-fp="pasta" data-kind="folder">…</button></div></div>
        <div class="full"><label>Trilha (MP3/WAV/M4A) — vazio = MP3 dentro da pasta ou trilha do kit</label><div class="row" style="flex-wrap:nowrap"><input id="f_trilha" placeholder="opcional"><button class="btn sm" data-fp="trilha" data-kind="file">…</button></div></div>
        <div><label>Título (vazio = a IA cria)</label><input id="f_titulo"></div>
        <div><label>Subtítulo / data / local</label><input id="f_subtitulo"></div>
        <div><label>Estilo</label><select id="f_estilo">${d.estilos.map((e) => `<option>${e}</option>`).join("")}</select></div>
        <div><label>Duração (s) — 0 = automático pela trilha</label><input id="f_duracao" type="number" value="0" min="0"></div>
        <div><label>Kit do cliente (logo, legenda, destino)</label><select id="f_kit">${pr.presets.map((p) => `<option value="${p.id}">${esc(p.nome)}</option>`).join("")}</select></div>
        <div><label>Formatos</label><div class="tags">${d.formatos.map((f) => `<label><input type="checkbox" class="f_fmt" value="${f}" ${f === "16:9" ? "checked" : ""}>${f}${f === "16:9" ? " (1920×1080)" : ""}</label>`).join("")}</div></div>
        <div class="full"><label>Contexto para a IA (evento, pessoas, clima)</label><input id="f_contexto" placeholder="ex.: viagem em família para a serra, tom alegre e nostálgico"></div>
        <div class="full row"><div class="spacer"></div><button class="btn pri" id="fgo">Criar vídeo</button></div></div></div>
      <h2>Projetos</h2><div class="list" id="flist"></div>`;
    document.querySelectorAll("[data-fp]").forEach((b) => b.onclick = async () => { const r = await call("pick", { kind: b.dataset.kind }); if (r) $(`#f_${b.dataset.fp}`).value = r; else toast("Cole o caminho no campo (o seletor só abre no app do Mac)."); });
    $("#fgo").onclick = async () => {
      const formatos = [...document.querySelectorAll(".f_fmt:checked")].map((x) => x.value);
      if (!$("#f_pasta").value) return toast("Escolha a pasta de fotos.", true);
      if (!formatos.length) return toast("Escolha pelo menos um formato.", true);
      const id = await call("create_photo_video", { pasta: $("#f_pasta").value, trilha: $("#f_trilha").value, titulo: $("#f_titulo").value, subtitulo: $("#f_subtitulo").value,
        estilo: $("#f_estilo").value, duracao: +$("#f_duracao").value, contexto: $("#f_contexto").value, formatos, preset_id: +$("#f_kit").value });
      toast("Vídeo de fotos na fila."); ui.foto = id; ui.fsig = null; render();
    };
    await fotoList();
  },

  // ------------------------------------------------------------ KITS
  async kits() {
    const d = await call("presets");
    const cur = d.presets.find((p) => p.id === ui.kit) || d.presets[0];
    ui.kit = cur && cur.id;
    const p = cur ? cur.dados : d.padrao;
    const L = p.legenda || {};
    const effs = ["zoom_punch", "zoom_lento", "flash_entrada", "fade_saida", "vinheta", "granulado", "letterbox", "glitch_entrada"];
    const fileInput = (k, lbl, kind = "file") => `<div><label>${lbl}</label><div class="row" style="flex-wrap:nowrap"><input id="k_${k}" value="${esc(p[k] || "")}" placeholder="opcional"><button class="btn sm" data-pick="${k}" data-kind="${kind}">…</button></div></div>`;
    $("#main").innerHTML = `<h1>Kits por cliente/projeto</h1><p class="sub">Tudo o que o app precisa saber para não fazer perguntas. Cada pasta vigiada usa um kit.</p>
      <div class="row" style="margin-bottom:16px"><select id="ksel" style="width:auto">${d.presets.map((x) => `<option value="${x.id}" ${x.id === ui.kit ? "selected" : ""}>${esc(x.nome)}</option>`).join("")}</select>
      <button class="btn" id="knew">Novo kit</button><div class="spacer"></div><button class="btn bad" id="kdel">Apagar</button><button class="btn pri" id="ksave">Salvar kit</button></div>
      <div class="card"><div class="form">
        <div><label>Nome do kit</label><input id="k_nome" value="${esc(cur ? cur.nome : "Novo kit")}"></div>
        <div><label>Cliente</label><input id="k_cliente" value="${esc(p.cliente)}"></div>
        <div><label>Projeto</label><input id="k_projeto" value="${esc(p.projeto)}"></div>
        <div class="full"><label>Formatos de saída (o primeiro é usado nas prévias)</label><div class="tags">${["9:16", "1:1", "4:5", "16:9"].map((f) => `<label><input type="checkbox" class="k_fmt" value="${f}" ${p.formatos.includes(f) ? "checked" : ""}>${f}</label>`).join("")}</div></div>
        <div><label>Rotação do arquivo do YoloBox</label><select id="k_rotacao_yolobox">${[0, 90, 180, 270].map((r) => `<option ${+p.rotacao_yolobox === r ? "selected" : ""} value="${r}">${r}°</option>`).join("")}</select></div>
        ${fileInput("pasta_kit", "Pasta do kit (Drive) — logo, abertura, encerramento, trilhas", "folder")}
        ${fileInput("trilha", "Trilha padrão (arquivo ou pasta)")}
        ${fileInput("logo", "Logo (PNG)")}
        <div><label>Posição do logo</label><select id="k_posicao_logo">${["superior-direita", "superior-esquerda", "inferior-direita", "inferior-esquerda"].map((x) => `<option ${p.posicao_logo === x ? "selected" : ""}>${x}</option>`).join("")}</select></div>
        ${fileInput("abertura", "Abertura (vídeo)")}
        ${fileInput("encerramento", "Encerramento (vídeo)")}
        ${fileInput("pasta_saida", "Pasta dos finais", "folder")}
        <div><label>Fonte da legenda</label><input id="kl_fonte" value="${esc(L.fonte)}"></div>
        <div><label>Tamanho</label><input id="kl_tamanho" type="number" value="${L.tamanho}"></div>
        <div><label>Cor / destaque</label><div class="row" style="flex-wrap:nowrap"><input id="kl_cor" type="color" value="${L.cor}" style="height:38px"><input id="kl_destaque" type="color" value="${L.destaque}" style="height:38px"></div></div>
        <div><label>Posição da legenda</label><select id="kl_posicao">${["inferior", "meio", "superior"].map((x) => `<option ${L.posicao === x ? "selected" : ""}>${x}</option>`).join("")}</select></div>
        <div><label>Palavras por linha</label><input id="kl_palavras_por_linha" type="number" min="1" max="8" value="${L.palavras_por_linha}"></div>
        <div><label>&nbsp;</label><span class="chk"><input id="kl_maiusculas" type="checkbox" ${L.maiusculas ? "checked" : ""}> Legenda em maiúsculas</span></div>
        <div class="full"><label>Efeitos preferidos</label><div class="tags">${effs.map((e) => `<label><input type="checkbox" class="k_eff" value="${e}" ${p.efeitos.includes(e) ? "checked" : ""}>${e}</label>`).join("")}</div></div>
        <div><label>Cortes por episódio (mín–máx)</label><div class="row" style="flex-wrap:nowrap"><input id="k_cortes_min" type="number" value="${p.cortes_min}"><input id="k_cortes_max" type="number" value="${p.cortes_max}"></div></div>
        <div><label>Duração do corte em s (mín–máx)</label><div class="row" style="flex-wrap:nowrap"><input id="k_duracao_min" type="number" value="${p.duracao_min}"><input id="k_duracao_max" type="number" value="${p.duracao_max}"></div></div>
        <div><label>Variações por corte</label><select id="k_variacoes">${[2, 3].map((n) => `<option ${+p.variacoes === n ? "selected" : ""}>${n}</option>`).join("")}</select></div>
        <div><label>Tratamento de voz</label><select id="k_voz"><option value="local" ${p.voz === "local" ? "selected" : ""}>Local (FFmpeg)</option><option value="elevenlabs" ${p.voz === "elevenlabs" ? "selected" : ""}>ElevenLabs + local</option></select></div>
        <div><label>Fonte da trilha</label><select id="k_fonte_trilha"><option value="kit" ${p.fonte_trilha === "kit" ? "selected" : ""}>Kit do cliente</option><option value="epidemic" ${p.fonte_trilha === "epidemic" ? "selected" : ""}>Epidemic Sound (pelo clima)</option></select></div>
        <div><label>&nbsp;</label><span class="chk"><input id="k_magnific_capa" type="checkbox" ${p.magnific_capa ? "checked" : ""}> Capa com upscale Magnific</span></div>
        <div><label>&nbsp;</label><span class="chk"><input id="k_arquivar_drive" type="checkbox" ${p.arquivar_drive ? "checked" : ""}> Arquivar finais no Google Drive</span></div>
        <div class="full"><label>Contexto para a IA diretora (tom, público, assuntos, nomes próprios)</label><textarea id="k_contexto" rows="3">${esc(p.contexto)}</textarea></div>
      </div></div>`;
    $("#ksel").onchange = () => { ui.kit = +$("#ksel").value; render(); };
    $("#knew").onclick = async () => { ui.kit = await call("save_preset", { nome: "Novo kit", dados: d.padrao }); render(); };
    $("#kdel").onclick = async () => { if (cur && confirm("Apagar este kit?")) { await call("delete_preset", { id: cur.id }); ui.kit = null; render(); } };
    document.querySelectorAll("[data-pick]").forEach((b) => b.onclick = async () => { const r = await call("pick", { kind: b.dataset.kind }); if (r) $(`#k_${b.dataset.pick}`).value = r; else toast("Cole o caminho no campo (seletor só funciona no app do Mac).") });
    $("#ksave").onclick = async () => {
      const v = (id) => $(`#k_${id}`).value;
      const dados = { ...p, cliente: v("cliente"), projeto: v("projeto"), formatos: [...document.querySelectorAll(".k_fmt:checked")].map((x) => x.value),
        rotacao_yolobox: +v("rotacao_yolobox"), pasta_kit: v("pasta_kit"), trilha: v("trilha"), logo: v("logo"), posicao_logo: v("posicao_logo"),
        abertura: v("abertura"), encerramento: v("encerramento"), pasta_saida: v("pasta_saida"),
        legenda: { fonte: $("#kl_fonte").value, tamanho: +$("#kl_tamanho").value, cor: $("#kl_cor").value, destaque: $("#kl_destaque").value, contorno: L.contorno ?? 5,
          posicao: $("#kl_posicao").value, maiusculas: $("#kl_maiusculas").checked, palavras_por_linha: +$("#kl_palavras_por_linha").value },
        efeitos: [...document.querySelectorAll(".k_eff:checked")].map((x) => x.value), cortes_min: +v("cortes_min"), cortes_max: +v("cortes_max"),
        duracao_min: +v("duracao_min"), duracao_max: +v("duracao_max"), variacoes: +v("variacoes"), voz: v("voz"), fonte_trilha: v("fonte_trilha"),
        magnific_capa: $("#k_magnific_capa").checked, arquivar_drive: $("#k_arquivar_drive").checked, contexto: v("contexto") };
      if (!dados.formatos.length) return toast("Escolha pelo menos um formato.", true);
      ui.kit = await call("save_preset", { id: cur && cur.id, nome: $("#k_nome").value, dados }); toast("Kit salvo."); render();
    };
  },

  // ------------------------------------------------------------ PASTAS
  async pastas() {
    const [fs, d] = await Promise.all([call("folders"), call("presets")]);
    const opts = (sel) => d.presets.map((p) => `<option value="${p.id}" ${p.id === sel ? "selected" : ""}>${esc(p.nome)}</option>`).join("");
    $("#main").innerHTML = `<h1>Pastas vigiadas</h1><p class="sub">Quando um vídeo termina de ser copiado (o tamanho para de crescer e o arquivo abre), o episódio entra na fila com o kit da pasta.</p>
      <div class="list">${fs.map((f) => `<div class="card row" data-f="${f.id}"><input class="fp" value="${esc(f.pasta)}" style="flex:2"><button class="btn sm" data-pk>…</button>
        <select class="fk" style="flex:1">${opts(f.preset_id)}</select><span class="chk"><input type="checkbox" class="fa" ${f.ativa ? "checked" : ""}>ativa</span>
        ${f.existe ? "" : `<span class="pill bad">pasta não existe</span>`}<button class="btn sm fs">Salvar</button><button class="btn sm bad fd">Remover</button></div>`).join("")}</div>
      <h2>Nova pasta</h2><div class="card row"><input id="np" placeholder="/Users/voce/Movies/CorteX9/Cliente X" style="flex:2"><button class="btn sm" id="npk">…</button><select id="nk" style="flex:1">${opts()}</select><button class="btn pri" id="nadd">Adicionar</button></div>`;
    document.querySelectorAll("[data-f]").forEach((row) => {
      const id = +row.dataset.f;
      row.querySelector("[data-pk]").onclick = async () => { const r = await call("pick", { kind: "folder" }); if (r) row.querySelector(".fp").value = r; };
      row.querySelector(".fs").onclick = async () => { await call("save_folder", { id, pasta: row.querySelector(".fp").value, preset_id: +row.querySelector(".fk").value, ativa: row.querySelector(".fa").checked }); toast("Pasta salva."); render(); };
      row.querySelector(".fd").onclick = async () => { await call("delete_folder", { id }); render(); };
    });
    $("#npk").onclick = async () => { const r = await call("pick", { kind: "folder" }); if (r) $("#np").value = r; };
    $("#nadd").onclick = async () => { if (!$("#np").value) return; await call("save_folder", { pasta: $("#np").value, preset_id: +$("#nk").value }); toast("Pasta adicionada."); render(); };
  },

  // ------------------------------------------------------------ CONEXÕES
  async conexoes() {
    const d = await call("connections");
    const ob = await call("outbox");
    const lbl = { conectado: "Conectado", erro: "Com erro", faltando_chave: "Faltando chave", nao_testado: "Não testado" };
    $("#main").innerHTML = `<h1>Conexões</h1><p class="sub">O processamento pesado roda no Mac. A nuvem só entra para inteligência e recursos que não rodam localmente. Chaves guardadas em: <b>${esc(d.cofre)}</b>.</p>
      <div class="list">${d.servicos.map((s) => `<div class="card conn" data-s="${s.id}">
        <div><div><span class="dot ${s.estado}"></span><b>${esc(s.nome)}</b> &nbsp;<span class="pill">${lbl[s.estado] || s.estado}</span></div>
          <div class="why" style="margin-top:6px">${esc(s.uso)}</div><div style="margin-top:8px;font-size:12.5px">${esc(s.mensagem || "")}</div></div>
        <div class="form" style="grid-template-columns:1fr">${s.campos.map((c) => `<div><label>${esc(c.label)}</label>${c.key === "gdrive_service_account" ? `<textarea rows="2" data-k="${c.key}" placeholder="${c.value ? "•••••• (guardado)" : "cole o JSON"}"></textarea>` : `<input data-k="${c.key}" ${c.secret ? 'type="password" autocomplete="off"' : ""} placeholder="${c.secret && c.value ? "•••••• (guardada)" : ""}" value="${c.secret ? "" : esc(c.value)}">`}</div>`).join("")}
          <div class="row"><button class="btn sm sv">Salvar</button><button class="btn sm pri ts">Testar</button></div></div></div>`).join("")}</div>
      <h2>Envios na fila de reenvio</h2>${ob.length ? `<table><tr><th>#</th><th>Serviço</th><th>Tentativas</th><th>Situação</th></tr>${ob.map((o) => { const e = o.last_error ? JSON.parse(o.last_error) : null; return `<tr><td>${o.id}</td><td>${esc(o.service)} · ${esc(o.action)}</td><td>${o.attempts}</td><td>${o.done ? '<span class="pill ok">enviado</span>' : e ? `<span class="pill bad">${esc(e.motivo)}</span> ${esc(e.como_resolver)}` : '<span class="pill">aguardando</span>'}</td></tr>`; }).join("")}</table><div class="row" style="margin-top:10px"><button class="btn" id="obr">Tentar enviar agora</button></div>` : `<div class="empty">Nada pendente.</div>`}`;
    document.querySelectorAll("[data-s]").forEach((card) => {
      const values = () => { const v = {}; card.querySelectorAll("[data-k]").forEach((i) => { if (i.value.trim() || !(i.type === "password" || i.tagName === "TEXTAREA")) v[i.dataset.k] = i.value; }); return v; };
      card.querySelector(".sv").onclick = async () => { await call("save_connection", { values: values() }); toast("Salvo."); render(); };
      card.querySelector(".ts").onclick = async () => { await call("save_connection", { values: values() }); toast("Testando…"); const r = await call("test_connection", { id: card.dataset.s }); toast(r.mensagem, r.estado !== "conectado"); render(); };
    });
    if ($("#obr")) $("#obr").onclick = async () => { await call("outbox_retry_now"); render(); };
  },

  // ------------------------------------------------------------ ESTILO
  async estilo() {
    const d = await call("style_library"); const r = d.resumo;
    $("#main").innerHTML = `<h1>Biblioteca de estilo</h1><p class="sub">Tudo o que foi aprovado e reprovado entra como referência nos próximos planos da IA diretora.</p>
      <div class="kpis"><div class="kpi"><b>${r.aprovados}</b><span>aprovados</span></div><div class="kpi"><b>${r.reprovados}</b><span>reprovados</span></div>
      <div class="kpi"><b>${r.duracao_media_aprovada ?? "—"}s</b><span>duração média aprovada</span></div><div class="kpi"><b>${r.experimentais_aprovados}/${r.experimentais_total}</b><span>experimentais aprovados</span></div></div>
      <div class="card"><label>O que a IA diretora recebe como referência</label><pre style="white-space:pre-wrap;margin:0">${esc(d.texto)}</pre></div>
      <h2>Últimas decisões</h2><table><tr><th>Decisão</th><th>Gancho</th><th>Duração</th><th>LUT</th><th>Trilha</th><th>Efeitos</th><th>Nota IA</th><th>Ajuste pedido</th></tr>
      ${d.itens.map((i) => `<tr><td>${i.decision === "aprovado" ? '<span class="pill ok">aprovado</span>' : i.decision === "reprovado" ? '<span class="pill bad">reprovado</span>' : '<span class="pill info">ajuste</span>'}${i.experimental ? ' <span class="pill exp">💡</span>' : ""}</td>
        <td>${esc(i.hook)}</td><td>${Math.round(i.duration || 0)}s</td><td>${esc(i.lut)}</td><td>${esc(i.music_mood)}</td><td>${esc((i.effects || []).join(", "))}</td><td>${i.ai_score ?? ""}</td><td>${esc(i.note || "")}</td></tr>`).join("")}</table>`;
  },

  // ------------------------------------------------------------ CUSTOS
  async custos() {
    const d = await call("costs");
    $("#main").innerHTML = `<h1>Custo por episódio</h1><p class="sub">Tokens e créditos de cada serviço. A média é enviada ao Base44 ao concluir cada episódio.</p>
      <div class="kpis"><div class="kpi"><b>${usd(d.media_por_episodio)}</b><span>média por episódio concluído</span></div>
      ${d.por_servico.map((s) => `<div class="kpi"><b>${usd(s.usd)}</b><span>${esc(s.service)} · ${Math.round(s.units).toLocaleString("pt-BR")} ${esc(s.unit_type)}</span></div>`).join("")}</div>
      <table><tr><th>#</th><th>Episódio</th><th>Status</th><th>Custo</th></tr>${d.episodios.map((e) => `<tr><td>${e.id}</td><td>${esc(e.title)}</td><td>${pill(e.status)}</td><td>${usd(e.usd)}</td></tr>`).join("")}</table>
      <h2>Preços (US$ por unidade)</h2><div class="card form">${Object.entries(d.precos).map(([k, v]) => `<div><label>${esc(k)}</label><input data-p="${k}" type="number" step="any" value="${v}"></div>`).join("")}
      <div class="full"><button class="btn pri" id="ps">Salvar preços</button></div></div>`;
    $("#ps").onclick = async () => { const p = {}; document.querySelectorAll("[data-p]").forEach((i) => p[i.dataset.p] = i.value); await call("save_prices", { precos: p }); toast("Preços salvos."); };
  },

  // ------------------------------------------------------------ HISTÓRICO
  async historico() {
    const md = await call("changelog");
    const html = esc(md).replace(/^### (.*)$/gm, "<h3>$1</h3>").replace(/^## (.*)$/gm, "<h2>$1</h2>").replace(/^# (.*)$/gm, "<h1>$1</h1>")
      .replace(/^- (.*)$/gm, "<li>$1</li>").replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/`(.+?)`/g, "<code>$1</code>").replace(/\n\n/g, "<br>");
    $("#main").innerHTML = `<div class="md card">${html}</div>`;
  },
};

async function fotoList() {
  const d = await call("photo_projects");
  $("#flist").innerHTML = d.projetos.map((p) => `<div class="card ep" data-fid="${p.id}"><div><div class="t">${esc(p.title)}</div><div class="m">#${p.id} · ${esc(p.folder)} · ${fmtDate(p.created_at)}</div>${p.erro ? errBox(p.erro) : ""}</div>
    <div><div class="m" style="margin-bottom:6px">${esc(p.stage || "")}</div><div class="bar"><i style="width:${Math.round((p.progress || 0) * 100)}%"></i></div></div><div>${pill(p.status)}</div></div>`).join("") || `<div class="empty">Nenhum vídeo de fotos ainda.</div>`;
  document.querySelectorAll("[data-fid]").forEach((el) => el.onclick = () => { ui.foto = +el.dataset.fid; ui.fsig = null; $("#main").innerHTML = ""; render(); });
}

async function fotoDetail(id) {
  const p = await call("photo_project", { id });
  if (!p) { ui.foto = null; return render(); }
  const sig = JSON.stringify(p);
  if (ui.fsig === sig && $("#fdet")) return;
  ui.fsig = sig;
  const ratio = { "9:16": "r916", "1:1": "r11", "4:5": "r45" }[(p.opcoes.formatos || ["16:9"])[0]] || "";
  const sel = ui.fvar ?? p.escolhida ?? 0;
  const locked = ["renderizando_final", "concluido", "reprovado"].includes(p.status);
  const playing = {}; document.querySelectorAll("#fdet video").forEach((v) => playing[v.dataset.i] = v.currentTime);
  $("#main").innerHTML = `<div id="fdet"><div class="row"><button class="btn sm" id="fback">← Vídeos de fotos</button></div>
    <div class="row" style="margin-top:12px"><div><h1>${esc(p.titulo_video || p.titulo)}</h1><div class="sub" style="margin:0">${pill(p.status)} &nbsp;${esc(p.etapa || "")} · ${p.fotos_total} fotos · plano: ${esc(p.motor || "—")}${p.musica ? ` · trilha ${p.musica.bpm} BPM` : ""} · custo ${usd(p.custo.total_usd)}</div></div>
      <div class="spacer"></div>${p.status === "erro" ? `<button class="btn" id="fretry">Tentar de novo</button>` : ""}</div>
    ${p.erro ? errBox(p.erro) : ""}${(p.avisos || []).map((a) => `<div class="warn">${esc(a)}</div>`).join("")}
    <div class="bar" style="margin:12px 0 18px"><i style="width:${Math.round((p.progresso || 0) * 100)}%"></i></div>
    ${p.justificativa ? `<div class="card why" style="margin-bottom:16px"><b style="color:var(--text)">Por que a IA montou assim:</b> ${esc(p.justificativa)}${p.ajuste ? `<br><b style="color:var(--text)">Último ajuste:</b> ${esc(p.ajuste)}` : ""}</div>` : ""}
    <div class="fvars">${p.variacoes.map((v, i) => `<div class="cut ${i === sel ? "aprovado" : ""}">
      <div class="vid wide ${ratio}">${v.video ? `<video data-i="${i}" src="${v.video}" poster="${v.thumb || ""}" controls playsinline preload="metadata"></video>` : `<div class="ph">${v.status === "erro" ? "Esta versão falhou" : "Renderizando prévia…"}</div>`}</div>
      <div class="body"><div class="h"><b>V${v.idx} · ${esc(v.spec.nome)}</b>${pill(v.status === "pronta" ? "previa_pronta" : v.status === "erro" ? "erro" : "gerando_previa")}<span class="spacer"></span>${v.duracao ? `<span class="pill">${fmtT(v.duracao)}</span>` : ""}</div>
        <div class="meta"><span>LUT ${esc(v.spec.lut)}</span><span>movimento ×${v.spec.intensidade_movimento}</span><span>transições: ${esc(v.spec.transicao_padrao === "do_plano" ? "do plano" : v.spec.transicao_padrao)}</span>${(v.spec.efeitos || []).map((e) => `<span>${esc(e)}</span>`).join("")}</div>
        <div class="why">${esc(v.spec.justificativa || "")}</div>${v.erro ? errBox(v.erro) : ""}
        ${locked ? "" : `<div class="row"><button class="btn ok sm" data-fap="${i}" ${v.status === "pronta" ? "" : "disabled"}>Aprovar V${v.idx} e renderizar</button></div>`}</div></div>`).join("")}</div>
    ${locked ? "" : `<div class="card row" style="margin-top:16px"><input id="fadj" placeholder="Ajuste rápido: mais rápido, mais calmo, sem texto, outra LUT, título: …, duração: 60"><button class="btn" id="fadjb">Ajustar</button><button class="btn bad" id="frej">Reprovar</button></div>`}
    ${p.finais.length ? `<h2>Finais</h2><div class="meta">${p.finais.map((f) => `<span><a href="#" data-rev="${esc(f.arquivo)}" style="color:var(--ok)">${esc(f.formato)} · ${fmtT(f.duracao)} — abrir no Finder</a></span>`).join("")}</div>` : ""}
    <h2>Roteiro (${p.cenas.length} cenas)</h2><div class="scenes">${p.cenas.map((c) => `<div class="scene"><img src="${c.thumb}" loading="lazy"><div><b>${c.n}</b> · ${fmtT(c.inicio)}–${fmtT(c.fim)}<br>${esc(c.movimento)} · ${esc(c.transicao)}${c.texto ? `<br><i>“${esc(c.texto)}”</i>` : ""}${(c.ajustes || []).length ? `<br><span style="color:var(--ok)">✓ ${esc(c.ajustes.join(" · "))}</span>` : ""}</div></div>`).join("")}</div>
    ${(p.duplicadas || []).length ? `<h2>Fotos repetidas removidas</h2><div class="card why">${p.duplicadas.map((d) => `<div>Mantida <b style="color:var(--text)">${esc(d.mantida)}</b> — removidas: ${d.removidas.map((r) => `${esc(r.nome)} (${esc(r.motivo)})`).join(", ")}</div>`).join("")}</div>` : ""}</div>`;
  document.querySelectorAll("#fdet video").forEach((v) => { if (playing[v.dataset.i]) v.currentTime = playing[v.dataset.i]; });
  $("#fback").onclick = () => { ui.foto = null; ui.fvar = null; $("#main").innerHTML = ""; render(); };
  if ($("#fretry")) $("#fretry").onclick = async () => { const n = await call("photo_retry", { id }); toast(n ? "De volta à fila." : "Nada para repetir."); };
  document.querySelectorAll("[data-fap]").forEach((b) => b.onclick = async () => { await call("photo_approve", { id, var: +b.dataset.fap }); toast("Aprovado — render final na fila."); ui.fsig = null; render(); });
  document.querySelectorAll("[data-rev]").forEach((a) => a.onclick = (e) => { e.preventDefault(); call("reveal", { path: a.dataset.rev }); });
  if ($("#fadjb")) {
    const go = async () => { const note = $("#fadj").value; if (!note) return toast("Escreva o ajuste.", true); await call("photo_adjust", { id, note }); toast("Ajuste na fila — novas prévias em instantes."); ui.fsig = null; render(); };
    $("#fadjb").onclick = go; $("#fadj").onkeydown = (e) => { if (e.key === "Enter") go(); };
    $("#frej").onclick = async () => { const n = prompt("Motivo (opcional):", ""); if (n === null) return; await call("photo_reject", { id, note: n }); ui.fsig = null; render(); };
  }
}

function cutCard(el, c) {
  const ratio = { "9:16": "", "1:1": "r11", "16:9": "r169", "4:5": "r45" };
  const ready = c.variacoes.filter((v) => v.status === "pronta");
  let sel = ui.varSel[c.id] ?? c.escolhida ?? (ready[0] && ready[0].id) ?? (c.variacoes[0] && c.variacoes[0].id);
  if (!c.variacoes.find((v) => v.id === sel)) sel = c.variacoes[0] && c.variacoes[0].id;
  const v = c.variacoes.find((x) => x.id === sel);
  const locked = ["aprovado", "renderizando_final", "finalizado", "reprovado"].includes(c.status);
  const keepTime = el.querySelector("video") && el.querySelector("video").dataset.v == sel ? el.querySelector("video").currentTime : 0;
  el.className = `cut ${c.experimental ? "exp" : ""} ${c.status}`;
  el.innerHTML = `
    <div class="vid ${ratio[ui.fmt] || ""}">${v && v.video ? `<video data-v="${v.id}" src="${v.video}" poster="${v.thumb || ""}" controls playsinline preload="metadata"></video>`
      : `<div class="ph">${v && v.status === "erro" ? "Esta variação falhou" : "Renderizando prévia…"}</div>`}</div>
    <div class="body">
      <div class="h"><b>Corte ${String(c.idx).padStart(2, "0")}</b>${c.experimental ? '<span class="pill exp">💡 corte criativo · experimental</span>' : ""}${pill(c.status)}<span class="spacer"></span><span class="pill">nota ${c.nota ?? "—"}</span></div>
      <div class="vars">${c.variacoes.map((x) => `<button data-sel="${x.id}" class="${x.id === sel ? "on" : ""} ${x.id === c.escolhida ? "chosen" : ""}" title="${esc(x.spec.justificativa || "")}">V${x.idx} · ${esc(x.spec.nome || "")} · ${esc(x.spec.lut)}${x.status === "erro" ? " ⚠" : x.status !== "pronta" ? " ⏳" : ""}</button>`).join("")}</div>
      ${v ? `<div class="hook">“${esc(v.spec.gancho || c.gancho)}”</div><div class="meta"><span>${fmtT(c.inicio)}–${fmtT(c.fim)}</span><span>LUT ${esc(v.spec.lut)}</span><span>trilha ${esc(v.spec.clima_trilha || "—")}</span><span>${esc(v.spec.estrutura || "linear")}</span>${(v.spec.efeitos || []).map((e) => `<span>${esc(e)}</span>`).join("")}</div>` : ""}
      ${c.limpeza ? `<div class="why">Limpeza: −${c.limpeza.segundos_removidos}s de respiros${c.limpeza.vicios_removidos.length ? `, vícios: ${esc(c.limpeza.vicios_removidos.slice(0, 6).join(", "))}` : ""} · ${c.zooms} zooms${c.textos.length ? ` · textos: ${esc(c.textos.join(" / "))}` : ""}</div>` : ""}
      <details class="why"><summary>Por que a IA escolheu isso</summary><p>${esc(c.justificativa)}</p>${v && v.spec.justificativa ? `<p><b>Variação:</b> ${esc(v.spec.justificativa)}</p>` : ""}<p>Tema: ${esc(c.tema || "")}</p>${c.ajuste ? `<p><b>Último ajuste pedido:</b> ${esc(c.ajuste)}</p>` : ""}</details>
      ${c.erro ? errBox(c.erro) : ""}${v && v.erro ? errBox(v.erro) : ""}
      ${c.finais.length ? `<div class="meta">${c.finais.map((f) => `<span><a href="#" data-rev="${esc(f.arquivo)}" style="color:var(--ok)">${esc(f.formato)} · ${f.duracao}s</a></span>`).join("")}</div>` : ""}
    </div>
    ${locked ? (c.status === "reprovado" ? `<div class="acts"><button class="btn sm" data-act="readj">Pedir nova versão</button></div>` : "")
      : `<div class="acts"><button class="btn ok sm" data-act="ok" ${v && v.status === "pronta" ? "" : "disabled"}>Aprovar V${v ? v.idx : ""}</button><button class="btn bad sm" data-act="no">Reprovar todas</button>${c.status === "erro" ? '<button class="btn sm" data-act="retry">Tentar de novo</button>' : ""}</div>
         <div class="adj"><input placeholder="Ajuste rápido: mais curto, outra LUT, gancho: …" data-adj><button class="btn sm" data-act="adj">Ajustar</button></div>`}`;
  const vid = el.querySelector("video");
  if (vid && keepTime) vid.currentTime = keepTime;
  el.querySelectorAll("[data-sel]").forEach((b) => b.onclick = () => { ui.varSel[c.id] = +b.dataset.sel; call("choose", { cut_id: c.id, variation_id: +b.dataset.sel }); ui.cutSig[c.id] = null; cutCard(el, { ...c, escolhida: +b.dataset.sel }); });
  el.querySelectorAll("[data-rev]").forEach((a) => a.onclick = (e) => { e.preventDefault(); call("reveal", { path: a.dataset.rev }); });
  const act = async (k) => {
    if (k === "ok") { await call("approve", { cut_id: c.id, variation_id: sel }); toast(`Corte ${c.idx} aprovado — final na fila.`); }
    if (k === "no") { const n = prompt("Motivo da reprovação (opcional, ajuda a IA a aprender):", "") ; if (n === null) return; await call("reject", { cut_id: c.id, note: n }); }
    if (k === "adj" || k === "readj") { const note = k === "adj" ? el.querySelector("[data-adj]").value : prompt("O que mudar neste corte?"); if (!note) return toast("Escreva o ajuste.", true); await call("adjust", { cut_id: c.id, note }); toast("Ajuste na fila — novas prévias em instantes."); }
    if (k === "retry") { await call("retry", { cut_id: c.id }); }
    ui.cutSig[c.id] = null; render();
  };
  el.querySelectorAll("[data-act]").forEach((b) => b.onclick = () => act(b.dataset.act));
  const adj = el.querySelector("[data-adj]"); if (adj) adj.onkeydown = (e) => { if (e.key === "Enter") act("adj"); };
}

render();
