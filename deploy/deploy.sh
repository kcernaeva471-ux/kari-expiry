#!/bin/bash
# ═══════════════════════════════════════════════════════════════════
# Kari — деплой с Mac на VPS
# Запуск: bash deploy/deploy.sh root@IP
# ═══════════════════════════════════════════════════════════════════
set -e

SSH_TARGET="${1:-}"
DOMAIN="${2:-kari-realizaciya.ru}"
APP_DIR="/opt/kari-expiry"
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

if [ -z "$SSH_TARGET" ]; then
    echo "Использование: bash deploy/deploy.sh root@IP [домен]"
    echo "Пример:        bash deploy/deploy.sh root@123.45.67.89 kari-realizaciya.ru"
    exit 1
fi

echo "══════════════════════════════════════════"
echo " Деплой Kari на $SSH_TARGET"
echo " Домен: $DOMAIN"
echo "══════════════════════════════════════════"

# ── 1. Загрузка файлов ───────────────────────────────────────────
echo "[1/3] Загрузка файлов..."
rsync -avz --exclude '.git' \
    --exclude '*.pyc' \
    --exclude '__pycache__' \
    --exclude '.DS_Store' \
    --exclude 'venv' \
    --exclude 'deploy' \
    --exclude '.claude' \
    --exclude 'docs' \
    --exclude 'scripts' \
    --exclude 'data' \
    --exclude 'kari.db-shm' \
    --exclude 'kari.db-wal' \
    "$PROJECT_DIR/" "$SSH_TARGET:$APP_DIR/"
echo "  -> Файлы загружены"

# ── 2. Настройка .env на сервере ─────────────────────────────────
echo "[2/3] Настройка .env..."
ssh "$SSH_TARGET" "cat > $APP_DIR/.env << 'EOF'
TELEGRAM_BOT_TOKEN=8669528097:AAEqZ9nUjTczM0bejmbLzNBf-JFrSheXvk8
TELEGRAM_CHAT_ID=-1001625248856
FLASK_SECRET_KEY=kari-expiry-secret-2024
ADMIN_CODE=0000
DASHBOARD_URL=https://$DOMAIN
PORT=8081
EOF"
echo "  -> .env настроен"

# ── 3. Перезапуск ────────────────────────────────────────────────
echo "[3/3] Перезапуск..."
ssh "$SSH_TARGET" "
    chown -R kari:kari $APP_DIR 2>/dev/null || true
    systemctl restart kari 2>/dev/null || echo 'Сервис не настроен. Запустите server-setup.sh'
"

sleep 2
ssh "$SSH_TARGET" "systemctl is-active kari && echo 'Kari работает!' || echo 'Ошибка запуска'"

echo "══════════════════════════════════════════"
echo " Деплой завершён"
echo "══════════════════════════════════════════"
