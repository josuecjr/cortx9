"""Gera um episódio de teste (fala sintetizada + duas "câmeras" com cores diferentes) e solta na pasta vigiada."""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import db, paths
from . import ffmpeg as ff

TEXTO = [
    "Olá, sejam bem-vindos a mais um episódio do nosso podcast.",
    "Hoje eu quero contar um segredo que ninguém te conta sobre empreender.",
    "O maior erro que eu cometi foi achar que dinheiro resolvia tudo. Não resolve!",
    "Por que a maioria das pessoas desiste no primeiro ano? Porque não tem clareza do próprio objetivo.",
    "Eu lembro da primeira vez que eu perdi um cliente grande. Doeu demais, mas me ensinou muito.",
    "A verdade é que consistência vale mais do que talento. Sempre.",
    "Se você posta todo dia durante um ano, você já está na frente de noventa por cento das pessoas.",
    "E aqui vai uma dica prática: escreva três ideias por dia, mesmo que sejam ruins.",
    "Nunca subestime o poder de uma boa história. As pessoas compram histórias, não produtos.",
    "O medo de errar paralisa mais gente do que a falta de dinheiro.",
    "Então, para fechar: comece pequeno, mas comece hoje. Esse é o recado do episódio.",
    "Obrigado por assistir, e até a próxima!",
]


def make_demo_episode():
    w = db.one("SELECT path FROM watch_folders WHERE enabled=1 ORDER BY id LIMIT 1")
    inbox = Path(w["path"]).expanduser() if w else paths.DEFAULT_INBOX
    inbox.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="cortex9_demo_"))
    audio = tmp / "fala.aiff"
    if sys.platform == "darwin":
        voice = "Luciana"
        vs = subprocess.run(["say", "-v", "?"], capture_output=True, text=True).stdout
        if "Luciana" not in vs:
            voice = next((l.split()[0] for l in vs.splitlines() if "pt_BR" in l), None)
        text = " [[slnc 900]] ".join(TEXTO * 2)
        subprocess.run(["say", *(["-v", voice] if voice else []), "-r", "175", "-o", str(audio), text], check=True)
    else:  # sem TTS: tom + silêncios (só para testar o fluxo)
        ff.run(["-f", "lavfi", "-i", "sine=f=220:d=150", "-af", "volume='if(lt(mod(t,7),5),0.5,0)':eval=frame", str(audio)], step="demo")
    out_tmp = tmp / "episodio_teste.mp4"
    ff.run(["-f", "lavfi", "-i", "testsrc2=s=1920x1080:r=30", "-i", str(audio), "-shortest",
            "-vf", "colorbalance=rs=0.18:gs=0.05:bs=-0.15:enable='lt(mod(t,24),12)',eq=brightness=-0.06:enable='gte(mod(t,24),12)'",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", str(out_tmp)], step="demo")
    dest = inbox / "Episodio de teste CorteX9.mp4"
    n = 2
    while dest.exists():
        dest = inbox / f"Episodio de teste CorteX9 ({n}).mp4"
        n += 1
    shutil.move(str(out_tmp), dest)  # simula a cópia da câmera para a pasta vigiada
    shutil.rmtree(tmp, ignore_errors=True)
    return str(dest)
