#!/usr/bin/env bash
# meshtastic-zoo — установка/обновление на Linux-сервере как systemd-сервис.
#
# Однострочником (репо должен быть публичным ИЛИ с токеном в git-доступе):
#   curl -fsSL https://raw.githubusercontent.com/anton-vinogradov/meshtastic-zoo/main/install.sh | bash
#   # или от root:  … | sudo bash
#
# Либо из клона (приватный репо, честно и прозрачно):
#   git clone <repo> meshtastic-zoo && cd meshtastic-zoo && ./install.sh
#
# Обновление: повторить ту же команду (идемпотентно; git pull делается сам).
set -euo pipefail

REPO_URL="${MZ_REPO:-https://github.com/anton-vinogradov/meshtastic-zoo.git}"
SVC=meshtastic-zoo
PORT=8814

# --- где код: запущены из клона репо — берём его; иначе клонируем ---
SELF="${BASH_SOURCE[0]:-}"
if [ -n "$SELF" ] && [ -f "$(dirname -- "$SELF")/collector/hub.py" ]; then
  DIR="$(cd "$(dirname -- "$SELF")" && pwd)"
else
  command -v git >/dev/null || { echo "✗ нужен git"; exit 1; }
  DIR="${MZ_DIR:-/opt/meshtastic-zoo}"
  echo "→ код в $DIR"
  if [ -d "$DIR/.git" ]; then git -C "$DIR" pull --ff-only
  else sudo -n true 2>/dev/null && sudo mkdir -p "$DIR" && sudo chown "$(id -un)": "$DIR" || mkdir -p "$DIR"
       git clone --depth 1 "$REPO_URL" "$DIR"; fi
fi

# --- от кого будет работать сервис (root через "| sudo bash" — берём SUDO_USER) ---
if [ "$(id -u)" -eq 0 ]; then RUN_USER="${SUDO_USER:-root}"; SUDO=""; else RUN_USER="$(id -un)"; SUDO="sudo"; fi
asuser() { if [ "$(id -un)" = "$RUN_USER" ]; then "$@"; else sudo -u "$RUN_USER" -- "$@"; fi; }
[ "$(id -u)" -eq 0 ] && chown -R "$RUN_USER": "$DIR"

command -v python3 >/dev/null || { echo "✗ нужен python3"; exit 1; }

echo "→ venv и зависимости…"
asuser python3 -m venv "$DIR/.venv"
asuser "$DIR/.venv/bin/pip" install -q --upgrade pip
asuser "$DIR/.venv/bin/pip" install -q -r "$DIR/requirements.txt"

echo "→ конфиг и папка данных…"
[ -f "$DIR/collector/config.json" ] || asuser cp "$DIR/collector/config.example.json" "$DIR/collector/config.json"
asuser mkdir -p "$DIR/data"

echo "→ systemd-сервис $SVC…"
$SUDO tee "/etc/systemd/system/$SVC.service" >/dev/null <<UNIT
[Unit]
Description=meshtastic-zoo hub
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$DIR
ExecStart=$DIR/.venv/bin/python $DIR/collector/hub.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

$SUDO systemctl daemon-reload
$SUDO systemctl enable "$SVC" >/dev/null 2>&1 || true
$SUDO systemctl restart "$SVC"

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
echo "✓ готово. Карта: http://${IP:-<этот-сервер>}:$PORT"
echo "  первый запуск: задай свои подсети в ⚙ (config.example — обобщённый)"
echo "  логи:     journalctl -u $SVC -f"
echo "  обновить: повтори ту же команду установки"
