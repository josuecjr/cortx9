"""Notificação do macOS (+ celular via ntfy, pela fila de reenvio)."""
import logging
import subprocess
import sys

from . import secrets, services

log = logging.getLogger("cortex9.notify")


def send(title, message):
    log.info("NOTIFICAÇÃO: %s — %s", title, message)
    if sys.platform == "darwin":
        t = title.replace('"', "'")
        m = message.replace('"', "'")
        subprocess.run(["osascript", "-e", f'display notification "{m}" with title "{t}" sound name "Glass"'], check=False)
    if secrets.has("ntfy_topico"):
        services.enqueue("celular", "notificar", {"titulo": title, "mensagem": message})
