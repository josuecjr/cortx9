"""Ponto de entrada: sobe a fila, a pasta vigiada e abre a janela nativa do CorteX9."""
import argparse
import logging
import os
import subprocess
import sys
import threading
from logging.handlers import RotatingFileHandler

from . import APP_NAME, VERSION, api, db, jobs, luts, paths, server, services, watcher


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


def start_background():
    stop = threading.Event()
    db.seed()
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
