"""Ponto de entrada: sobe a fila, a pasta vigiada e abre a janela nativa do CorteX9."""
import argparse
import logging
import os
import subprocess
import sys
import threading
from logging.handlers import RotatingFileHandler

from . import APP_NAME, VERSION, api, db, eventlog, jobs, luts, paths, secrets, server, services, watcher


def setup_logging(verbose=False):
    paths.ensure()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fh = RotatingFileHandler(paths.LOGS / "cortex9.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)


def _remove_magnific():
    """Magnific saiu do CorteX9 (2.3.0): apaga a chave guardada e a opção antiga dos kits."""
    try:
        if secrets.has("magnific_api_key"):
            secrets.delete("magnific_api_key")
            eventlog.info("Chave da Magnific removida do Keychain (integração retirada do app).")
        for p in db.q("SELECT id, data FROM presets"):
            d = p["data"] or {}
            if "magnific_capa" in d:
                d.pop("magnific_capa")
                db.update("presets", p["id"], data=d)
    except Exception:  # noqa: BLE001
        logging.getLogger("cortex9").exception("limpeza da Magnific")


def start_background():
    stop = threading.Event()
    db.seed()
    eventlog.install()
    _remove_magnific()
    try:
        diag = eventlog.diagnostics()
        from . import ffmpeg as ff
        caps = ff.capabilities()
        missing = [k for k, v in caps.items() if not v and k != "VideoToolbox"]
        eventlog.info(f"{APP_NAME} {VERSION} aberto · FFmpeg: {diag.get('FFmpeg versão', diag.get('FFmpeg'))}", step="início",
                      detail="\n".join(f"{k}: {v}" for k, v in diag.items()))
        if missing:
            eventlog.warn("O FFmpeg deste Mac não tem: " + ", ".join(missing) + ". Rode o instalador novo do CorteX9.", step="início")
    except Exception:  # noqa: BLE001
        logging.getLogger("cortex9").exception("diagnóstico inicial")
    luts.ensure_all()
    jobs.start(stop)
    threading.Thread(target=watcher.loop, args=(stop,), name="pasta-vigiada", daemon=True).start()
    threading.Thread(target=services.outbox_loop, args=(stop,), name="reenvios", daemon=True).start()
    if sys.platform == "darwin":  # não deixa o Mac dormir enquanto o app está aberto processando
        subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
    return stop


def main():
    ap = argparse.ArgumentParser(prog="cortex9")
    ap.add_argument("--headless", action="store_true", help="sem janela (servidor + fila apenas)")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()
    setup_logging(a.verbose)
    log = logging.getLogger("cortex9")
    log.info("%s %s iniciando — dados em %s", APP_NAME, VERSION, paths.BASE)
    stop = start_background()
    _srv, url = server.start(a.port)
    if a.headless:
        print(f"CorteX9 rodando em {url}", flush=True)
        try:
            stop.wait()
        except KeyboardInterrupt:
            pass
        return
    import webview
    win = webview.create_window(f"{APP_NAME} {VERSION}", url, width=1440, height=920, min_size=(1100, 720), background_color="#0E0F12")
    api._window = win
    webview.start(debug=a.verbose)
    stop.set()
