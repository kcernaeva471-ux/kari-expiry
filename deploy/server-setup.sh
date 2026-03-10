#!/bin/bash
# ═══════════════════════════════════════════════════════════════════
# Kari — автоматическая настройка VPS (Ubuntu 22/24)
# Запуск: bash server-setup.sh kari-realizaciya.ru
# ═══════════════════════════════════════════════════════════════════
set -e

DOMAIN="${1:-kari-realizaciya.ru}"
APP_DIR="/opt/kari-expiry"
APP_USER="kari"
PYTHON="python3"
PORT=8081

echo "══════════════════════════════════════════"
echo " Настройка сервера для $DOMAIN"
echo "══════════════════════════════════════════"

# ── 1. Системные пакеты ──────────────────────────────────────────
echo "[1/8] Установка пакетов..."
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv nginx certbot python3-certbot-nginx ufw curl > /dev/null 2>&1
echo "  -> Пакеты установлены"

# ── 2. Пользователь приложения ───────────────────────────────────
echo "[2/8] Пользователь kari..."
if ! id "$APP_USER" &>/dev/null; then
    useradd -r -m -s /bin/bash "$APP_USER"
    echo "  -> Создан пользователь $APP_USER"
else
    echo "  -> Пользователь $APP_USER уже существует"
fi

# ── 3. Директория приложения ─────────────────────────────────────
echo "[3/8] Директория $APP_DIR..."
mkdir -p "$APP_DIR"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

# ── 4. Python venv + зависимости ─────────────────────────────────
echo "[4/8] Python venv..."
if [ ! -d "$APP_DIR/venv" ]; then
    $PYTHON -m venv "$APP_DIR/venv"
fi
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"
echo "  -> Зависимости установлены"

# ── 5. Systemd сервис ────────────────────────────────────────────
echo "[5/8] Systemd сервис..."
cat > /etc/systemd/system/kari.service << 'UNIT'
[Unit]
Description=Kari Expiry Tracker
After=network.target

[Service]
Type=simple
User=kari
Group=kari
WorkingDirectory=/opt/kari-expiry
ExecStart=/opt/kari-expiry/venv/bin/gunicorn app:flask_app \
    --bind 127.0.0.1:8081 \
    --workers 1 \
    --timeout 120 \
    --access-logfile /opt/kari-expiry/access.log \
    --error-logfile /opt/kari-expiry/error.log
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
echo "  -> Сервис создан"

# ── 6. Nginx ─────────────────────────────────────────────────────
echo "[6/8] Nginx..."
cat > /etc/nginx/sites-available/kari << NGINX
server {
    listen 80;
    server_name $DOMAIN www.$DOMAIN;

    # Логи
    access_log /var/log/nginx/kari_access.log;
    error_log  /var/log/nginx/kari_error.log;

    # Лимит загрузки файлов (для Excel)
    client_max_body_size 20M;

    # Статика напрямую
    location /static/ {
        alias $APP_DIR/static/;
        expires 7d;
        add_header Cache-Control "public, immutable";
    }

    # Приложение
    location / {
        proxy_pass http://127.0.0.1:$PORT;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_connect_timeout 10;
        proxy_read_timeout 120;
        proxy_send_timeout 120;
    }
}
NGINX

# Активируем
ln -sf /etc/nginx/sites-available/kari /etc/nginx/sites-enabled/kari
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx
echo "  -> Nginx настроен"

# ── 7. Firewall ──────────────────────────────────────────────────
echo "[7/8] Firewall..."
ufw --force enable > /dev/null 2>&1 || true
ufw allow ssh > /dev/null 2>&1
ufw allow 'Nginx Full' > /dev/null 2>&1
echo "  -> Firewall: SSH + HTTP + HTTPS"

# ── 8. SSL (Let's Encrypt) ───────────────────────────────────────
echo "[8/8] SSL сертификат..."
certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos --email admin@"$DOMAIN" --redirect 2>/dev/null || {
    echo "  -> SSL: домен ещё не указывает на сервер. Запустите позже:"
    echo "     certbot --nginx -d $DOMAIN --redirect"
}

# ── Запуск ───────────────────────────────────────────────────────
echo ""
echo "══════════════════════════════════════════"
echo " Запуск приложения..."
echo "══════════════════════════════════════════"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
systemctl enable kari
systemctl restart kari

sleep 2
if systemctl is-active --quiet kari; then
    echo "  Kari работает на порту $PORT"
    echo ""
    echo "  Проверка: curl http://localhost:$PORT"
    echo "  Логи:     journalctl -u kari -f"
    echo "  Домен:    https://$DOMAIN"
else
    echo "  ОШИБКА: сервис не запустился"
    journalctl -u kari --no-pager -n 20
fi
echo "══════════════════════════════════════════"
