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
_default_db = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kari.db")
if os.path.isdir("/data"):
    _default_db = "/data/kari.db"
DATABASE_PATH = os.environ.get("DATABASE_PATH", _default_db)

# ── Авторизация ────────────────────────────────────────────────────────────
ADMIN_CODE = os.environ.get("ADMIN_CODE", "0000")

# ── Общее ──────────────────────────────────────────────────────────────────
TIMEZONE = "Europe/Moscow"

# ── Центры и магазины ─────────────────────────────────────────────────────
DEFAULT_CENTER = "Центр 5"  # Все наши магазины — Центр 5

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

# ── Фото акций ────────────────────────────────────────────────────────────
PHOTOS_DIR = "/data/photos" if os.path.isdir("/data") else os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "photos"
)
ALLOWED_PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".webp"}
MAX_PHOTO_SIZE_MB = 10

# ── Цвета категорий (для веба) ────────────────────────────────────────────
CATEGORY_STYLES = {
    "ПРОСРОЧЕН": {"bg": "#f5d5e5", "text": "#991E66", "badge": "danger"},
    "Скидка 70%": {"bg": "#f0dde8", "text": "#8b3a6a", "badge": "brown"},
    "Скидка 50%": {"bg": "#ebe0f0", "text": "#6b457a", "badge": "warning"},
    "В норме": {"bg": "#e0ece4", "text": "#3d6b50", "badge": "success"},
}
