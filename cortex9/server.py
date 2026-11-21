"""Servidor local (127.0.0.1) que alimenta a janela nativa: interface, API e vídeos com suporte a Range."""
import json
import logging
import mimetypes
import os
import secrets as pysecrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import api, db, paths
from .errors import explain

log = logging.getLogger("cortex9.server")
UI = Path(__file__).resolve().parent / "ui"
TOKEN = pysecrets.token_urlsafe(24)


def _allowed(p: Path):
    p = p.resolve()
    roots = [paths.BASE, paths.MOVIES]
    for pr in db.q("SELECT data FROM presets"):
        if (pr["data"] or {}).get("pasta_saida"):
            roots.append(Path(pr["data"]["pasta_saida"]).expanduser())
    for pr in db.q("SELECT options FROM photo_projects"):  # destino escolhido no projeto
        if (pr["options"] or {}).get("pasta_saida"):
            roots.append(Path(pr["options"]["pasta_saida"]).expanduser())
    return any(str(p).startswith(str(r.resolve())) for r in roots)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body=b"", ctype="application/json", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _cookie_ok(self):
        return f"c9={TOKEN}" in (self.headers.get("Cookie") or "")

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            qs = parse_qs(u.query)
            if qs.get("t", [""])[0] != TOKEN and not self._cookie_ok():
                return self._send(403, b"forbidden", "text/plain")
            body = (UI / "index.html").read_bytes().replace(b"__TOKEN__", TOKEN.encode())
            return self._send(200, body, "text/html; charset=utf-8", {"Set-Cookie": f"c9={TOKEN}; Path=/; SameSite=Strict; HttpOnly"})
        if u.path.startswith("/ui/"):
            f = (UI / u.path[4:]).resolve()
            if not str(f).startswith(str(UI)) or not f.is_file():
                return self._send(404, b"", "text/plain")
            return self._send(200, f.read_bytes(), (mimetypes.guess_type(f.name)[0] or "application/octet-stream") + "; charset=utf-8")
        if u.path == "/media":
            if not self._cookie_ok():
                return self._send(403, b"", "text/plain")
            p = Path(unquote(parse_qs(u.query).get("p", [""])[0]))
            if not p.is_file() or not _allowed(p):
                return self._send(404, b"", "text/plain")
            return self._file(p)
        self._send(404, b"", "text/plain")

    do_HEAD = do_GET

    def _file(self, p: Path):
        size = p.stat().st_size
        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        code = 200
        if rng and rng.startswith("bytes="):
            a, _, b = rng[6:].partition("-")
            start = int(a) if a else max(0, size - int(b))
            end = int(b) if a and b else size - 1
            end = min(end, size - 1)
            code = 206
        length = end - start + 1
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(p, "rb") as f:
            f.seek(start)
            left = length
            try:
                while left > 0:
                    chunk = f.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def do_POST(self):
        u = urlparse(self.path)
        if not u.path.startswith("/api/") or self.headers.get("X-Token") != TOKEN:
            return self._send(403, b'{"erro":"forbidden"}')
        name = u.path[5:]
        fn = api.METHODS.get(name)
        if not fn:
            return self._send(404, b'{"erro":"metodo"}')
        n = int(self.headers.get("Content-Length") or 0)
        args = json.loads(self.rfile.read(n) or b"{}") if n else {}
        try:
            res = fn(**args)
            body = json.dumps({"ok": True, "data": res}, ensure_ascii=False, default=str).encode()
        except Exception as e:  # noqa: BLE001
            log.exception("api %s", name)
            err = explain(name, e).as_dict()
            if isinstance(e, ValueError):
                err["motivo"], err["como_resolver"] = str(e), ""
            body = json.dumps({"ok": False, "erro": err}, ensure_ascii=False).encode()
        self._send(200, body)


def start(port=0):
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, name="servidor", daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/?t={TOKEN}"
