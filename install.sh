#!/usr/bin/env bash
# meshtastic-zoo — установка/обновление на Linux-сервере как systemd-сервис.
#
# Запускать ОТ ОБЫЧНОГО пользователя (не root): venv/конфиг/данные будут его,
# а для systemd скрипт сам вызовет sudo. Нужен python3 (venv) и sudo.
#
#   git clone <repo> meshtastic-zoo && cd meshtastic-zoo && ./install.sh
#   # обновление позже:
#   git pull && ./install.sh
#
# Идемпотентно: повторный запуск обновляет зависимости и перезапускает сервис.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SVC=meshtastic-zoo
RUN_USER="$(id -un)"
PORT=8814

[ "$RUN_USER" = root ] && { echo "✗ запусти от обычного пользователя, не root (sudo вызовется сам)"; exit 1; }
command -v python3 >/dev/null || { echo "✗ нужен python3"; exit 1; }
command -v sudo   >/dev/null || { echo "✗ нужен sudo (для systemd)"; exit 1; }

echo "→ venv и зависимости…"
python3 -m venv "$DIR/.venv"
"$DIR/.venv/bin/pip" install -q --upgrade pip
"$DIR/.venv/bin/pip" install -q -r "$DIR/requirements.txt"

echo "→ конфиг и папка данных…"
[ -f "$DIR/collector/config.json" ] || cp "$DIR/collector/config.example.json" "$DIR/collector/config.json"
mkdir -p "$DIR/data"

echo "→ systemd-сервис $SVC (нужен sudo)…"
sudo tee "/etc/systemd/system/$SVC.service" >/dev/null <<UNIT
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

sudo systemctl daemon-reload
sudo systemctl enable --now "$SVC"
sudo systemctl restart "$SVC"   # на случай обновления кода

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
echo "✓ готово. Карта: http://${IP:-<этот-сервер>}:$PORT"
echo "  статус:   sudo systemctl status $SVC"
echo "  логи:     journalctl -u $SVC -f"
echo "  обновить: git pull && ./install.sh"
