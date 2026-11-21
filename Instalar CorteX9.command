#!/bin/bash
# Instalador do CorteX9 para macOS (Apple Silicon). Dê dois cliques.
set -e
cd "$(dirname "$0")"
SRC="$(pwd)"
APPDATA="$HOME/Library/Application Support/CorteX9"
APPDIR="$APPDATA/app"
VENV="$APPDATA/venv"
BUNDLE="$HOME/Applications/CorteX9.app"

echo "=============================================="
echo "  CorteX9 — instalação"
echo "=============================================="

if [ "$(uname -m)" != "arm64" ]; then
  echo "⚠️  Este Mac não é Apple Silicon. O CorteX9 funciona, mas sem a aceleração do M3."
fi

# 0) Atualização: fecha o CorteX9 se estiver aberto (dados, kits e chaves são mantidos)
if pgrep -f "python.*-m cortex9" >/dev/null 2>&1; then
  echo "→ Fechando o CorteX9 aberto para atualizar…"
  pkill -f "python.*-m cortex9" || true
  sleep 2
fi
if [ -f "$APPDIR/cortex9/__init__.py" ]; then
  echo "→ Versão instalada: $(grep -o '[0-9]\+\.[0-9]\+\.[0-9]\+' "$APPDIR/cortex9/__init__.py" | head -1) — atualizando…"
fi

# 1) Homebrew
if ! command -v brew >/dev/null 2>&1; then
  if [ -x /opt/homebrew/bin/brew ]; then eval "$(/opt/homebrew/bin/brew shellenv)"; else
    echo "→ Instalando o Homebrew (vai pedir a senha do Mac)…"
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    eval "$(/opt/homebrew/bin/brew shellenv)"
  fi
fi

# 2) FFmpeg (com VideoToolbox e libass) e Python
echo "→ Instalando FFmpeg e Python…"
brew list ffmpeg >/dev/null 2>&1 || brew install ffmpeg
brew list python@3.12 >/dev/null 2>&1 || brew install python@3.12
PY="$(brew --prefix)/bin/python3.12"

# 2b) O FFmpeg do Homebrew pode vir sem libass (textos/títulos do vídeo). Sem ela as prévias falham.
FF="$(brew --prefix)/bin/ffmpeg"
if ! "$FF" -hide_banner -filters 2>/dev/null | grep -qE "^ ... subtitles "; then
  echo "→ O FFmpeg instalado não tem suporte a textos (libass). Instalando o ffmpeg-full…"
  brew install ffmpeg-full || echo "   ⚠️  Não consegui instalar o ffmpeg-full. O app funciona, mas sem textos no vídeo de fotos. Veja a aba Log."
fi
for cand in "$(brew --prefix)/opt/ffmpeg-full/bin/ffmpeg" "$FF"; do
  if [ -x "$cand" ] && "$cand" -hide_banner -filters 2>/dev/null | grep -qE "^ ... subtitles "; then
    echo "   FFmpeg com textos: $cand"; break
  fi
done

# 3) Código do app
echo "→ Copiando o app…"
mkdir -p "$APPDATA"
rm -rf "$APPDIR"
mkdir -p "$APPDIR"
cp -R "$SRC/cortex9" "$SRC/CHANGELOG.md" "$SRC/requirements.txt" "$APPDIR/"

# 4) Ambiente Python + dependências (Whisper MLX, pywebview, Anthropic SDK…)
echo "→ Instalando dependências (alguns minutos na primeira vez)…"
[ -d "$VENV" ] || "$PY" -m venv "$VENV"
"$VENV/bin/pip" install --upgrade pip -q
"$VENV/bin/pip" install --upgrade -r "$APPDIR/requirements.txt" -q

# 5) Modelo do Whisper (baixa agora para o primeiro episódio não esperar)
echo "→ Baixando o modelo do Whisper (≈1,6 GB, só na primeira vez)…"
"$VENV/bin/python" - <<'PYEOF' || echo "   (o modelo será baixado no primeiro episódio)"
from huggingface_hub import snapshot_download
snapshot_download("mlx-community/whisper-large-v3-turbo")
PYEOF

# 6) App nativo em ~/Applications
echo "→ Criando o CorteX9.app…"
mkdir -p "$BUNDLE/Contents/MacOS" "$BUNDLE/Contents/Resources"
cat > "$BUNDLE/Contents/Info.plist" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>CorteX9</string>
  <key>CFBundleDisplayName</key><string>CorteX9</string>
  <key>CFBundleIdentifier</key><string>app.cortex9.mac</string>
  <key>CFBundleVersion</key><string>2.5.0</string>
  <key>CFBundleShortVersionString</key><string>2.5.0</string>
  <key>CFBundleExecutable</key><string>CorteX9</string>
  <key>CFBundleIconFile</key><string>CorteX9</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>LSApplicationCategoryType</key><string>public.app-category.video</string>
</dict></plist>
PL
cat > "$BUNDLE/Contents/MacOS/CorteX9" <<SH
#!/bin/bash
export PATH="/opt/homebrew/bin:/usr/local/bin:\$PATH"
cd "$APPDIR"
exec "$VENV/bin/python" -m cortex9 "\$@" >> "$APPDATA/logs/app-stdout.log" 2>&1
SH
chmod +x "$BUNDLE/Contents/MacOS/CorteX9"
mkdir -p "$APPDATA/logs"
# ícone
"$VENV/bin/python" "$APPDIR/cortex9/icon.py" "$BUNDLE/Contents/Resources" 2>/dev/null || true
touch "$BUNDLE"

# 7) Pastas padrão
mkdir -p "$HOME/Movies/CorteX9/Entrada" "$HOME/Movies/CorteX9/Finais"

echo ""
echo "✅ CorteX9 $(grep -o '[0-9]\+\.[0-9]\+\.[0-9]\+' "$APPDIR/cortex9/__init__.py" | head -1) instalado em ~/Applications/CorteX9.app"
echo "   Pasta vigiada padrão: ~/Movies/CorteX9/Entrada"
echo ""
open "$BUNDLE"
