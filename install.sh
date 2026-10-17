#!/usr/bin/env bash
# Установка mail2tg как systemd-сервиса.
# Запуск из папки проекта:  ./install.sh
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="mail2tg"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"

if [[ $EUID -ne 0 ]]; then
  echo "Запустите от root: sudo ./install.sh" >&2
  exit 1
fi

echo "==> Папка проекта: $APP_DIR"

# Python и venv
if ! command -v python3 >/dev/null 2>&1; then
  echo "==> Устанавливаю python3"
  apt-get update -qq && apt-get install -y -qq python3 python3-venv
fi
if [[ ! -x "$APP_DIR/.venv/bin/python" ]]; then
  echo "==> Создаю виртуальное окружение"
  if ! python3 -m venv "$APP_DIR/.venv" 2>/dev/null; then
    PYVER="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
    apt-get update -qq
    apt-get install -y -qq python3-venv "python${PYVER}-venv" || apt-get install -y -qq python3-venv
    rm -rf "$APP_DIR/.venv"
    python3 -m venv "$APP_DIR/.venv"
  fi
fi

echo "==> Устанавливаю зависимости"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

# .env
if [[ ! -f "$APP_DIR/.env" ]]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  echo "==> Создан $APP_DIR/.env из шаблона"
fi
chmod 600 "$APP_DIR/.env"

# systemd
echo "==> Устанавливаю сервис $UNIT_PATH"
sed "s|__APP_DIR__|$APP_DIR|g" "$APP_DIR/systemd/mail2tg.service" > "$UNIT_PATH"
systemctl daemon-reload
systemctl enable "$SERVICE_NAME" >/dev/null

env_value() { grep -E "^$1=" "$APP_DIR/.env" | tail -n1 | cut -d= -f2- | tr -d '"'"'"' '; }

if [[ -n "$(env_value TELEGRAM_BOT_TOKEN)" && -n "$(env_value TELEGRAM_CHAT_ID)" \
      && -n "$(env_value IMAP_USER)" && -n "$(env_value IMAP_PASSWORD)" ]]; then
  echo "==> Проверяю настройки"
  if "$APP_DIR/.venv/bin/python" "$APP_DIR/mail2tg.py" --check; then
    systemctl restart "$SERVICE_NAME"
    echo
    echo "Готово! Сервис запущен. Логи:  journalctl -u $SERVICE_NAME -f"
  else
    echo
    echo "Сервис установил, но не запустил: исправьте .env и выполните"
    echo "  systemctl restart $SERVICE_NAME"
  fi
else
  cat <<EOF

Сервис установлен, но ещё не запущен — заполните настройки:
  nano $APP_DIR/.env

Узнать свой chat_id (сначала нажмите «Старт» у бота):
  $APP_DIR/.venv/bin/python $APP_DIR/mail2tg.py --get-chat-id

Проверить настройки:
  $APP_DIR/.venv/bin/python $APP_DIR/mail2tg.py --check

Запустить:
  systemctl restart $SERVICE_NAME
  journalctl -u $SERVICE_NAME -f
EOF
fi
