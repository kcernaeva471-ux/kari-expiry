import os
from dotenv import load_dotenv

# Загружаем .env
env_path = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(env_path):
    load_dotenv(env_path)

# ── Telegram ───────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = int(os.environ.get("TELEGRAM_CHAT_ID", "0"))

# ── Flask ──────────────────────────────────────────────────────────────────
FLASK_SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "kari-expiry-secret-2024")
FLASK_PORT = int(os.environ.get("PORT", 5000))
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", f"http://localhost:{FLASK_PORT}")

# ── База данных ────────────────────────────────────────────────────────────
DATABASE_PATH = os.environ.get("DATABASE_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "kari.db"))

# ── Авторизация ────────────────────────────────────────────────────────────
ADMIN_CODE = os.environ.get("ADMIN_CODE", "0000")

# ── Общее ──────────────────────────────────────────────────────────────────
TIMEZONE = "Europe/Moscow"

# ── Центры и магазины ─────────────────────────────────────────────────────
DEFAULT_CENTER = "Центр 1"  # Название центра по умолчанию при миграции

# Список магазинов (fallback, если БД пуста)
VALID_STORES = [
    '10065', '10245', '10259', '10706', '10858',
    '10958', '10960', '10983', '11006', '11049',
    '11066', '11405', '11409', '11637', '11661',
    '11701', '11862', '11863', '11961', '13101', '13108',
]

# ── Логика скидок ──────────────────────────────────────────────────────────
DISCOUNT_THRESHOLDS = {
    "expired": 0,
    "discount_70": 90,
    "discount_50": 180,
}

STATUS_MAP = {
    "просрочен": "ПРОСРОЧЕН",
    "скидка 70": "Скидка 70%",
    "70%": "Скидка 70%",
    "скидка 50": "Скидка 50%",
    "50%": "Скидка 50%",
    "в норме": "В норме",
    "не заполнен": "Не заполнен",
}

# ── Цвета категорий (для веба) ────────────────────────────────────────────
CATEGORY_STYLES = {
    "ПРОСРОЧЕН": {"bg": "#f8d7da", "text": "#721c24", "badge": "danger"},
    "Скидка 70%": {"bg": "#fbe9d7", "text": "#6D4C30", "badge": "brown"},
    "Скидка 50%": {"bg": "#fff9c4", "text": "#f57f17", "badge": "warning"},
    "В норме": {"bg": "#c8e6c9", "text": "#1b5e20", "badge": "success"},
}
