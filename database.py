"""
SQLite база данных.
Таблицы: store_products (товары по магазинам), batches (партии с датами),
store_access (коды доступа).
"""

import sqlite3
import threading
import random
import string
from datetime import date, timedelta
from dateutil.relativedelta import relativedelta

import config

_local = threading.local()


def get_db() -> sqlite3.Connection:
    if not hasattr(_local, "db") or _local.db is None:
        _local.db = sqlite3.connect(config.DATABASE_PATH, check_same_thread=False)
        _local.db.row_factory = sqlite3.Row
        _local.db.execute("PRAGMA journal_mode=WAL")
        _local.db.execute("PRAGMA foreign_keys=ON")
    return _local.db


def init_db():
    db = get_db()
    db.executescript("""
        -- Товары магазинов (из остатков)
        CREATE TABLE IF NOT EXISTS store_products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            store_number TEXT NOT NULL,
            article TEXT NOT NULL,
            name TEXT DEFAULT '',
            brand TEXT DEFAULT '',
            product_group TEXT DEFAULT '',
            available INTEGER DEFAULT 0,
            no_expiry BOOLEAN DEFAULT 0,
            UNIQUE(store_number, article)
        );

        -- Партии (даты производства + срок годности)
        CREATE TABLE IF NOT EXISTS batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL REFERENCES store_products(id) ON DELETE CASCADE,
            production_date TEXT NOT NULL,
            shelf_life_months INTEGER NOT NULL,
            expiry_date TEXT NOT NULL,
            quantity INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- Коды доступа магазинов
        CREATE TABLE IF NOT EXISTS store_access (
            store_number TEXT PRIMARY KEY,
            access_code TEXT NOT NULL,
            role TEXT DEFAULT 'director'
        );

        CREATE INDEX IF NOT EXISTS idx_sp_store ON store_products(store_number);
        CREATE INDEX IF NOT EXISTS idx_sp_article ON store_products(article);
        CREATE INDEX IF NOT EXISTS idx_batches_product ON batches(product_id);
    """)
    db.commit()


def setup_store_access():
    db = get_db()
    for store in config.VALID_STORES:
        existing = db.execute(
            "SELECT 1 FROM store_access WHERE store_number = ?", (store,)
        ).fetchone()
        if not existing:
            code = "".join(random.choices(string.digits, k=4))
            db.execute(
                "INSERT INTO store_access (store_number, access_code, role) VALUES (?, ?, 'director')",
                (store, code),
            )
    db.commit()


# ── Импорт данных ─────────────────────────────────────────────────────────

def import_stock(stock_rows: list, catalog: dict):
    """
    Импорт остатков + каталог.
    stock_rows: [{"article", "name", "store", "available"}, ...]
    catalog: {article: {"name", "brand", "group"}, ...}
    """
    db = get_db()

    # Очищаем старые данные
    db.execute("DELETE FROM batches")
    db.execute("DELETE FROM store_products")

    count = 0
    for row in stock_rows:
        article = row["article"]
        store = row["store"]
        available = row["available"]

        if not available or available <= 0:
            continue

        # Берём инфо из каталога (если есть)
        cat_info = catalog.get(article, {})
        name = cat_info.get("name", row.get("name", ""))
        brand = cat_info.get("brand", "")
        group = cat_info.get("group", "")

        # Определяем, есть ли срок годности
        no_expiry = is_no_expiry_product(name, group)

        db.execute(
            """INSERT OR REPLACE INTO store_products
               (store_number, article, name, brand, product_group, available, no_expiry)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (store, article, name, brand, group, available, 1 if no_expiry else 0),
        )
        count += 1

    db.commit()
    return count


def is_no_expiry_product(name: str, group: str) -> bool:
    """Определяет, нет ли у товара срока годности (логически)."""
    name_lower = name.lower()
    no_expiry_words = [
        "спонж", "повязка", "стакан", "футляр", "кисть", "кисточка",
        "щётка", "щетка", "расчёска", "расческа", "пилка", "ножницы",
        "пинцет", "зеркало", "органайзер", "мочалка", "массажёр",
        "массажер", "бигуди", "набор для окрашивания",
        "маска для лица us01",  # одноразовые маски
        "защитных масок",
    ]
    for word in no_expiry_words:
        if word in name_lower:
            return True
    return False


# ── Партии ─────────────────────────────────────────────────────────────────

def add_batch(product_id: int, production_date: str, shelf_life_months: int,
              quantity: int = 0) -> int:
    """Добавляет партию к товару. Возвращает batch_id."""
    db = get_db()

    # Рассчитываем дату окончания
    from datetime import datetime
    prod_date = datetime.strptime(production_date, "%Y-%m-%d").date()
    expiry = prod_date + relativedelta(months=shelf_life_months)

    cursor = db.execute(
        """INSERT INTO batches (product_id, production_date, shelf_life_months,
           expiry_date, quantity)
           VALUES (?, ?, ?, ?, ?)""",
        (product_id, production_date, shelf_life_months, expiry.isoformat(), quantity),
    )
    db.commit()
    return cursor.lastrowid


def delete_batch(batch_id: int):
    db = get_db()
    db.execute("DELETE FROM batches WHERE id = ?", (batch_id,))
    db.commit()


def mark_no_expiry(product_id: int, value: bool = True):
    """Отмечает товар как «без срока годности»."""
    db = get_db()
    db.execute(
        "UPDATE store_products SET no_expiry = ? WHERE id = ?",
        (1 if value else 0, product_id),
    )
    # Удаляем партии если ставим «без срока»
    if value:
        db.execute("DELETE FROM batches WHERE product_id = ?", (product_id,))
    db.commit()


# ── Запросы ────────────────────────────────────────────────────────────────

def classify_days(days_left: int) -> str:
    if days_left <= 0:
        return "ПРОСРОЧЕН"
    if days_left <= config.DISCOUNT_THRESHOLDS["discount_70"]:
        return "Скидка 70%"
    if days_left <= config.DISCOUNT_THRESHOLDS["discount_50"]:
        return "Скидка 50%"
    return "В норме"


def get_store_products(store_number: str, filter_status: str = None) -> list[dict]:
    """
    Товары магазина с партиями и рассчитанными статусами.
    filter_status: None (все), 'urgent' (просроч+70%+50%), 'ПРОСРОЧЕН', 'Скидка 70%' и т.п.
    """
    db = get_db()
    today = date.today()

    rows = db.execute(
        """SELECT sp.*, GROUP_CONCAT(b.id) as batch_ids
           FROM store_products sp
           LEFT JOIN batches b ON b.product_id = sp.id
           WHERE sp.store_number = ?
           GROUP BY sp.id
           ORDER BY sp.name""",
        (store_number,),
    ).fetchall()

    products = []
    for row in rows:
        p = dict(row)
        p["batches"] = []

        # Загружаем партии
        if row["batch_ids"]:
            batches = db.execute(
                "SELECT * FROM batches WHERE product_id = ? ORDER BY expiry_date",
                (row["id"],),
            ).fetchall()
            for b in batches:
                bd = dict(b)
                exp = date.fromisoformat(b["expiry_date"])
                bd["days_left"] = (exp - today).days
                bd["status"] = classify_days(bd["days_left"])
                bd["expiry_formatted"] = exp.strftime("%d.%m.%Y")
                bd["prod_formatted"] = date.fromisoformat(b["production_date"]).strftime("%d.%m.%Y")
                p["batches"].append(bd)

        # Определяем общий статус товара
        if p["no_expiry"]:
            p["status"] = "Без срока"
            p["worst_days"] = 99999
        elif not p["batches"]:
            p["status"] = "Не заполнен"
            p["worst_days"] = None
        else:
            worst = min(b["days_left"] for b in p["batches"])
            p["status"] = classify_days(worst)
            p["worst_days"] = worst

        # Фильтр
        if filter_status == "urgent":
            if p["status"] in ("ПРОСРОЧЕН", "Скидка 70%", "Скидка 50%"):
                products.append(p)
        elif filter_status == "Не заполнен":
            if p["status"] == "Не заполнен":
                products.append(p)
        elif filter_status and filter_status != "all":
            if p["status"] == filter_status:
                products.append(p)
        else:
            products.append(p)

    # Сортируем: просроченные сверху
    def sort_key(p):
        if p["worst_days"] is None:
            return 99998
        return p["worst_days"]

    products.sort(key=sort_key)
    return products


def get_all_stores_summary() -> list[dict]:
    """Сводка по всем магазинам."""
    db = get_db()
    today = date.today()
    result = []

    for store in config.VALID_STORES:
        total = db.execute(
            "SELECT COUNT(*) as c FROM store_products WHERE store_number = ?",
            (store,),
        ).fetchone()["c"]

        no_expiry = db.execute(
            "SELECT COUNT(*) as c FROM store_products WHERE store_number = ? AND no_expiry = 1",
            (store,),
        ).fetchone()["c"]

        # Товары с партиями
        with_batches = db.execute(
            """SELECT COUNT(DISTINCT sp.id) as c FROM store_products sp
               JOIN batches b ON b.product_id = sp.id
               WHERE sp.store_number = ?""",
            (store,),
        ).fetchone()["c"]

        not_filled = total - no_expiry - with_batches

        # Считаем по категориям
        counts = {"ПРОСРОЧЕН": 0, "Скидка 70%": 0, "Скидка 50%": 0, "В норме": 0}
        batch_rows = db.execute(
            """SELECT sp.id, MIN(b.expiry_date) as min_expiry
               FROM store_products sp
               JOIN batches b ON b.product_id = sp.id
               WHERE sp.store_number = ?
               GROUP BY sp.id""",
            (store,),
        ).fetchall()

        for br in batch_rows:
            exp = date.fromisoformat(br["min_expiry"])
            days = (exp - today).days
            cat = classify_days(days)
            counts[cat] += 1

        result.append({
            "store_number": store,
            "total": total,
            "not_filled": not_filled,
            "no_expiry": no_expiry,
            **counts,
        })

    return result


def get_store_stats(store_number: str) -> dict:
    """Счётчики для одного магазина."""
    db = get_db()
    today = date.today()

    total = db.execute(
        "SELECT COUNT(*) as c FROM store_products WHERE store_number = ?",
        (store_number,),
    ).fetchone()["c"]

    no_expiry = db.execute(
        "SELECT COUNT(*) as c FROM store_products WHERE store_number = ? AND no_expiry = 1",
        (store_number,),
    ).fetchone()["c"]

    with_batches = db.execute(
        """SELECT COUNT(DISTINCT sp.id) as c FROM store_products sp
           JOIN batches b ON b.product_id = sp.id WHERE sp.store_number = ?""",
        (store_number,),
    ).fetchone()["c"]

    counts = {"ПРОСРОЧЕН": 0, "Скидка 70%": 0, "Скидка 50%": 0, "В норме": 0}
    batch_rows = db.execute(
        """SELECT sp.id, MIN(b.expiry_date) as min_expiry
           FROM store_products sp JOIN batches b ON b.product_id = sp.id
           WHERE sp.store_number = ? GROUP BY sp.id""",
        (store_number,),
    ).fetchall()

    for br in batch_rows:
        exp = date.fromisoformat(br["min_expiry"])
        days = (exp - today).days
        counts[classify_days(days)] += 1

    return {
        "store_number": store_number,
        "total": total,
        "not_filled": total - no_expiry - with_batches,
        "no_expiry": no_expiry,
        "filled": with_batches,
        **counts,
    }


def get_product_by_id(product_id: int):
    db = get_db()
    return db.execute("SELECT * FROM store_products WHERE id = ?", (product_id,)).fetchone()


def check_access(store_number: str, code: str):
    if code == config.ADMIN_CODE:
        return {"store_number": "admin", "role": "admin"}
    db = get_db()
    row = db.execute(
        "SELECT * FROM store_access WHERE store_number = ? AND access_code = ?",
        (store_number, code),
    ).fetchone()
    if row:
        return {"store_number": row["store_number"], "role": row["role"]}
    return None


def get_access_codes() -> list[dict]:
    db = get_db()
    return [dict(r) for r in db.execute("SELECT * FROM store_access ORDER BY store_number").fetchall()]
