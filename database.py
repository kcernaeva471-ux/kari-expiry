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
        _local.db.execute("PRAGMA cache_size=-8000")      # 8 MB кэш
        _local.db.execute("PRAGMA mmap_size=67108864")     # 64 MB mmap
        _local.db.execute("PRAGMA synchronous=NORMAL")     # быстрее записи
        _local.db.execute("PRAGMA temp_store=MEMORY")      # temp в памяти
    return _local.db


def init_db():
    db = get_db()
    db.executescript("""
        -- Центры (группы магазинов)
        CREATE TABLE IF NOT EXISTS centers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE
        );

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
            role TEXT DEFAULT 'director',
            center_id INTEGER REFERENCES centers(id)
        );

        -- Журнал активности
        CREATE TABLE IF NOT EXISTS activity_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            store_number TEXT NOT NULL,
            action TEXT NOT NULL,
            details TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- Кэш цен с kari.com
        CREATE TABLE IF NOT EXISTS product_prices (
            article TEXT PRIMARY KEY,
            price INTEGER DEFAULT 0,
            fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_sp_store ON store_products(store_number);
        CREATE INDEX IF NOT EXISTS idx_sp_article ON store_products(article);
        CREATE INDEX IF NOT EXISTS idx_batches_product ON batches(product_id);
        CREATE INDEX IF NOT EXISTS idx_batches_expiry ON batches(expiry_date);
        CREATE INDEX IF NOT EXISTS idx_activity_store ON activity_log(store_number);
        CREATE INDEX IF NOT EXISTS idx_activity_time ON activity_log(created_at);
        CREATE INDEX IF NOT EXISTS idx_prices_article ON product_prices(article);

        -- Снимки импортов (история загрузок)
        CREATE TABLE IF NOT EXISTS stock_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            imported_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            filename TEXT DEFAULT '',
            total_rows INTEGER DEFAULT 0,
            products_updated INTEGER DEFAULT 0,
            products_added INTEGER DEFAULT 0,
            products_zeroed INTEGER DEFAULT 0
        );

        -- Изменения количеств при импорте
        CREATE TABLE IF NOT EXISTS stock_changes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_id INTEGER NOT NULL REFERENCES stock_snapshots(id),
            store_number TEXT NOT NULL,
            article TEXT NOT NULL,
            product_name TEXT DEFAULT '',
            previous_qty INTEGER DEFAULT 0,
            new_qty INTEGER DEFAULT 0,
            delta INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_sc_snapshot ON stock_changes(snapshot_id);
        CREATE INDEX IF NOT EXISTS idx_sc_store ON stock_changes(store_number);
        CREATE INDEX IF NOT EXISTS idx_sc_created ON stock_changes(created_at);
        CREATE INDEX IF NOT EXISTS idx_snapshots_time ON stock_snapshots(imported_at);
    """)

    # Миграции для существующих БД
    cols = [r[1] for r in db.execute("PRAGMA table_info(store_access)").fetchall()]
    if "center_id" not in cols:
        db.execute("ALTER TABLE store_access ADD COLUMN center_id INTEGER REFERENCES centers(id)")
    if "address" not in cols:
        db.execute("ALTER TABLE store_access ADD COLUMN address TEXT DEFAULT ''")
    if "manager_name" not in cols:
        db.execute("ALTER TABLE store_access ADD COLUMN manager_name TEXT DEFAULT ''")

    # Миграция store_products
    sp_cols = [r[1] for r in db.execute("PRAGMA table_info(store_products)").fetchall()]
    if "tester" not in sp_cols:
        db.execute("ALTER TABLE store_products ADD COLUMN tester INTEGER DEFAULT 0")

    db.commit()


def setup_store_access():
    """Инициализирует магазины и центр по умолчанию (Центр 5)."""
    db = get_db()

    # Создаём Центр 5, если ещё нет
    center_row = db.execute(
        "SELECT id FROM centers WHERE name = ?", (config.DEFAULT_CENTER,)
    ).fetchone()
    if center_row:
        center_id = center_row["id"]
    else:
        cursor = db.execute(
            "INSERT INTO centers (name) VALUES (?)", (config.DEFAULT_CENTER,)
        )
        center_id = cursor.lastrowid

    # Добавляем магазины из VALID_STORES → Центр 5
    for store in config.VALID_STORES:
        db.execute(
            """INSERT INTO store_access (store_number, access_code, role, center_id)
               VALUES (?, ?, 'director', ?)
               ON CONFLICT(store_number) DO UPDATE SET
                   access_code = CASE WHEN access_code = store_number THEN ? ELSE access_code END,
                   center_id = ?""",
            (store, store, center_id, store, center_id),
        )
    db.commit()


# ── Импорт данных ─────────────────────────────────────────────────────────

def import_stock(stock_rows: list, catalog: dict, filename: str = "",
                  additive: bool = False) -> dict:
    """
    Безопасный импорт остатков — обновляет количества, не удаляет партии.
    Отслеживает изменения для расчёта продаж.

    additive=True: только добавляет/обновляет товары из файла, НЕ обнуляет остальные.
    additive=False (по умолчанию): товары, отсутствующие в файле, обнуляются.

    Возвращает dict: {updated, added, zeroed, total_changes, snapshot_id}
    """
    db = get_db()

    # 1. Создаём запись импорта
    cursor = db.execute(
        "INSERT INTO stock_snapshots (filename) VALUES (?)", (filename,)
    )
    snapshot_id = cursor.lastrowid

    # 2. Агрегируем новые данные по (магазин, артикул)
    new_data = {}
    for row in stock_rows:
        article = row["article"]
        store = row["store"]
        available = row["available"]
        if not available or available <= 0:
            continue

        cat_info = catalog.get(article, {})
        name = cat_info.get("name", row.get("name", ""))
        brand = cat_info.get("brand", "")
        group = cat_info.get("group", "") or row.get("group", "")
        no_expiry = is_no_expiry_product(name, group)

        key = (store, article)
        if key in new_data:
            new_data[key]["available"] += available
        else:
            new_data[key] = {
                "available": available, "name": name,
                "brand": brand, "group": group, "no_expiry": no_expiry,
            }

    # 3. Загружаем текущие товары для сравнения
    existing = {}
    for row in db.execute(
        "SELECT id, store_number, article, available, name FROM store_products"
    ).fetchall():
        existing[(row["store_number"], row["article"])] = {
            "id": row["id"], "available": row["available"], "name": row["name"],
        }

    updated = 0
    added = 0
    zeroed = 0
    changes = []

    # 4. Обновляем существующие / добавляем новые
    for (store, article), info in new_data.items():
        key = (store, article)
        if key in existing:
            old_qty = existing[key]["available"]
            new_qty = info["available"]
            db.execute(
                """UPDATE store_products
                   SET available = ?,
                       name = COALESCE(NULLIF(?, ''), name),
                       brand = COALESCE(NULLIF(?, ''), brand),
                       product_group = COALESCE(NULLIF(?, ''), product_group)
                   WHERE id = ?""",
                (new_qty, info["name"], info["brand"], info["group"],
                 existing[key]["id"]),
            )
            if old_qty != new_qty:
                changes.append((snapshot_id, store, article,
                               existing[key]["name"] or info["name"],
                               old_qty, new_qty, new_qty - old_qty))
            updated += 1
            existing[key]["_done"] = True
        else:
            db.execute(
                """INSERT INTO store_products
                   (store_number, article, name, brand, product_group, available, no_expiry)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (store, article, info["name"], info["brand"], info["group"],
                 info["available"], 1 if info["no_expiry"] else 0),
            )
            changes.append((snapshot_id, store, article, info["name"],
                           0, info["available"], info["available"]))
            added += 1

    # 5. Товары не в файле → available = 0 (распродано)
    #    Пропускается при additive=True (дополнительный импорт)
    if not additive:
        for (store, article), ex in existing.items():
            if ex.get("_done"):
                continue
            if ex["available"] > 0:
                db.execute(
                    "UPDATE store_products SET available = 0 WHERE id = ?",
                    (ex["id"],),
                )
                changes.append((snapshot_id, store, article, ex["name"],
                               ex["available"], 0, -ex["available"]))
                zeroed += 1

    # 6. Сохраняем изменения
    if changes:
        db.executemany(
            """INSERT INTO stock_changes
               (snapshot_id, store_number, article, product_name,
                previous_qty, new_qty, delta)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            changes,
        )

    # 7. Обновляем статистику снимка
    db.execute(
        """UPDATE stock_snapshots
           SET total_rows = ?, products_updated = ?,
               products_added = ?, products_zeroed = ?
           WHERE id = ?""",
        (len(stock_rows), updated, added, zeroed, snapshot_id),
    )
    db.commit()

    return {
        "updated": updated, "added": added, "zeroed": zeroed,
        "total_changes": len(changes), "snapshot_id": snapshot_id,
    }


def undo_import(snapshot_id: int) -> dict:
    """
    Отменяет импорт: восстанавливает предыдущие количества товаров.
    Возвращает dict: {restored, deleted, snapshot_id}
    """
    db = get_db()

    # Получаем все изменения для этого снимка
    changes = db.execute(
        """SELECT store_number, article, previous_qty, new_qty
           FROM stock_changes WHERE snapshot_id = ?""",
        (snapshot_id,),
    ).fetchall()

    restored = 0
    deleted = 0

    for ch in changes:
        store = ch["store_number"]
        article = ch["article"]
        prev = ch["previous_qty"]
        new = ch["new_qty"]

        if prev == 0 and new > 0:
            # Товар был добавлен — удаляем его (или обнуляем)
            db.execute(
                """UPDATE store_products SET available = 0
                   WHERE store_number = ? AND article = ?""",
                (store, article),
            )
            deleted += 1
        elif prev > 0 and new == 0:
            # Товар был обнулён — восстанавливаем
            db.execute(
                """UPDATE store_products SET available = ?
                   WHERE store_number = ? AND article = ?""",
                (prev, store, article),
            )
            restored += 1
        elif prev != new:
            # Количество изменилось — откатываем
            db.execute(
                """UPDATE store_products SET available = ?
                   WHERE store_number = ? AND article = ?""",
                (prev, store, article),
            )
            restored += 1

    # Удаляем записи изменений и снимок
    db.execute("DELETE FROM stock_changes WHERE snapshot_id = ?", (snapshot_id,))
    db.execute("DELETE FROM stock_snapshots WHERE id = ?", (snapshot_id,))
    db.commit()

    return {"restored": restored, "deleted": deleted, "snapshot_id": snapshot_id}


def get_last_snapshot() -> dict:
    """Возвращает последний снимок импорта."""
    db = get_db()
    row = db.execute(
        "SELECT * FROM stock_snapshots ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


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


def set_tester(product_id: int, value: bool = True):
    """Помечает товар как тестер (выставлен для покупателей)."""
    db = get_db()
    db.execute(
        "UPDATE store_products SET tester = ? WHERE id = ?",
        (1 if value else 0, product_id),
    )
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

        # Сколько штук внесено в партии
        p["batch_total"] = sum(b.get("quantity", 0) or 0 for b in p["batches"])
        p["remaining"] = max(p["available"] - p["batch_total"], 0) if not p["no_expiry"] else 0

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

        # Собираем набор статусов всех партий товара
        batch_statuses = {b["status"] for b in p["batches"]} if p["batches"] else set()

        # Фильтр: товар попадает во вкладку, если ЛЮБАЯ его партия подходит
        if filter_status == "urgent":
            if batch_statuses & {"ПРОСРОЧЕН", "Скидка 70%", "Скидка 50%"}:
                products.append(p)
        elif filter_status == "Не заполнен":
            # Показываем если нет партий ИЛИ внесено меньше чем available
            if not p["batches"] or p["remaining"] > 0:
                if not p["no_expiry"]:
                    products.append(p)
        elif filter_status and filter_status != "all":
            if filter_status in batch_statuses or (not p["batches"] and p["status"] == filter_status):
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


def get_valid_stores() -> list[str]:
    """Список всех магазинов из БД (замена config.VALID_STORES)."""
    db = get_db()
    rows = db.execute("SELECT store_number FROM store_access ORDER BY store_number").fetchall()
    if rows:
        return [r["store_number"] for r in rows]
    # Fallback на конфиг если БД пуста
    return list(config.VALID_STORES)


def get_all_stores_summary() -> list[dict]:
    """Сводка по всем магазинам (по количеству штук) с информацией о центрах."""
    db = get_db()
    today = date.today()
    result = []

    stores_rows = db.execute("""
        SELECT sa.store_number, sa.center_id, COALESCE(c.name, '') as center_name
        FROM store_access sa
        LEFT JOIN centers c ON c.id = sa.center_id
        WHERE sa.store_number != 'admin' AND sa.role != 'center_manager'
        ORDER BY sa.center_id, sa.store_number
    """).fetchall()

    if not stores_rows:
        stores_rows = [{"store_number": s, "center_id": None, "center_name": ""} for s in config.VALID_STORES]

    for srow in stores_rows:
        store = srow["store_number"] if isinstance(srow, dict) else srow[0]
        center_id = srow["center_id"] if isinstance(srow, dict) else srow[1]
        center_name = srow["center_name"] if isinstance(srow, dict) else srow[2]

        # Общее количество штук
        total = db.execute(
            "SELECT COALESCE(SUM(available), 0) as c FROM store_products WHERE store_number = ?",
            (store,),
        ).fetchone()["c"]

        # Без срока — штуки
        no_expiry = db.execute(
            "SELECT COALESCE(SUM(available), 0) as c FROM store_products WHERE store_number = ? AND no_expiry = 1",
            (store,),
        ).fetchone()["c"]

        # Заполнено = сумма штук во всех партиях (реально внесённые)
        filled = db.execute(
            """SELECT COALESCE(SUM(b.quantity), 0) as c
               FROM store_products sp JOIN batches b ON b.product_id = sp.id
               WHERE sp.store_number = ? AND sp.no_expiry = 0""",
            (store,),
        ).fetchone()["c"]

        not_filled = max(total - no_expiry - filled, 0)

        # Тестеры (количество артикулов-тестеров)
        testers = db.execute(
            "SELECT COUNT(*) as c FROM store_products WHERE store_number = ? AND tester = 1",
            (store,),
        ).fetchone()["c"]

        # Категории по количеству штук в партиях
        counts = {"ПРОСРОЧЕН": 0, "Скидка 70%": 0, "Скидка 50%": 0, "В норме": 0}
        batch_rows = db.execute(
            """SELECT b.expiry_date, b.quantity
               FROM store_products sp
               JOIN batches b ON b.product_id = sp.id
               WHERE sp.store_number = ?""",
            (store,),
        ).fetchall()

        for br in batch_rows:
            exp = date.fromisoformat(br["expiry_date"])
            days = (exp - today).days
            counts[classify_days(days)] += max(br["quantity"] or 1, 1)

        result.append({
            "store_number": store,
            "center_id": center_id,
            "center_name": center_name,
            "total": total,
            "not_filled": not_filled,
            "no_expiry": no_expiry,
            "testers": testers,
            **counts,
        })

    return result


def get_store_stats(store_number: str) -> dict:
    """Счётчики для одного магазина (по количеству штук, не по артикулам)."""
    db = get_db()
    today = date.today()

    # Общее количество штук
    total = db.execute(
        "SELECT COALESCE(SUM(available), 0) as c FROM store_products WHERE store_number = ?",
        (store_number,),
    ).fetchone()["c"]

    # Количество артикулов (для отображения)
    total_articles = db.execute(
        "SELECT COUNT(*) as c FROM store_products WHERE store_number = ?",
        (store_number,),
    ).fetchone()["c"]

    # Без срока годности — штуки
    no_expiry = db.execute(
        "SELECT COALESCE(SUM(available), 0) as c FROM store_products WHERE store_number = ? AND no_expiry = 1",
        (store_number,),
    ).fetchone()["c"]

    testers = db.execute(
        "SELECT COUNT(*) as c FROM store_products WHERE store_number = ? AND tester = 1",
        (store_number,),
    ).fetchone()["c"]

    # Заполнено = сумма штук во всех партиях (реально внесённые)
    filled = db.execute(
        """SELECT COALESCE(SUM(b.quantity), 0) as c
           FROM store_products sp JOIN batches b ON b.product_id = sp.id
           WHERE sp.store_number = ? AND sp.no_expiry = 0""",
        (store_number,),
    ).fetchone()["c"]

    # Категории по количеству штук в партиях
    counts = {"ПРОСРОЧЕН": 0, "Скидка 70%": 0, "Скидка 50%": 0, "В норме": 0}
    batch_rows = db.execute(
        """SELECT b.expiry_date, b.quantity
           FROM store_products sp JOIN batches b ON b.product_id = sp.id
           WHERE sp.store_number = ?""",
        (store_number,),
    ).fetchall()

    for br in batch_rows:
        exp = date.fromisoformat(br["expiry_date"])
        days = (exp - today).days
        cat = classify_days(days)
        counts[cat] += max(br["quantity"] or 1, 1)

    not_filled = max(total - no_expiry - filled, 0)

    return {
        "store_number": store_number,
        "total": total,
        "total_articles": total_articles,
        "not_filled": not_filled,
        "no_expiry": no_expiry,
        "testers": testers,
        "filled": filled,
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


# ── Журнал активности ─────────────────────────────────────────────────────

def log_activity(store_number: str, action: str, details: str = ""):
    """Записывает действие в журнал."""
    db = get_db()
    db.execute(
        "INSERT INTO activity_log (store_number, action, details) VALUES (?, ?, ?)",
        (store_number, action, details),
    )
    db.commit()


def get_activity_log(limit: int = 200, store_filter: str = None) -> list[dict]:
    """Возвращает последние записи журнала."""
    db = get_db()
    if store_filter:
        rows = db.execute(
            """SELECT * FROM activity_log
               WHERE store_number = ?
               ORDER BY created_at DESC LIMIT ?""",
            (store_filter, limit),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT * FROM activity_log ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_activity_summary() -> list[dict]:
    """Сводка активности по магазинам: последний вход, кол-во действий."""
    db = get_db()
    rows = db.execute("""
        SELECT store_number,
               COUNT(*) as total_actions,
               SUM(CASE WHEN action = 'login' THEN 1 ELSE 0 END) as logins,
               SUM(CASE WHEN action = 'add_batch' THEN 1 ELSE 0 END) as batches_added,
               SUM(CASE WHEN action = 'no_expiry' THEN 1 ELSE 0 END) as no_expiry_set,
               MAX(created_at) as last_activity
        FROM activity_log
        WHERE store_number != 'admin'
        GROUP BY store_number
        ORDER BY last_activity DESC
    """).fetchall()
    return [dict(r) for r in rows]


def get_today_active_stores(tz_offset_hours: int = 3) -> set:
    """
    Возвращает set номеров магазинов, которые имели активность сегодня.
    tz_offset_hours: смещение от UTC (3 для Москвы).
    """
    db = get_db()
    # SQLite хранит created_at в UTC, сдвигаем на часовой пояс
    rows = db.execute("""
        SELECT DISTINCT store_number
        FROM activity_log
        WHERE store_number != 'admin'
          AND datetime(created_at, '+' || ? || ' hours') >= date('now', '+' || ? || ' hours')
    """, (tz_offset_hours, tz_offset_hours)).fetchall()
    return {r["store_number"] for r in rows}


# ── Центры ────────────────────────────────────────────────────────────────

def get_centers() -> list[dict]:
    """Список центров с количеством магазинов (без ДП)."""
    db = get_db()
    rows = db.execute("""
        SELECT c.id, c.name,
               COUNT(CASE WHEN sa.role != 'center_manager' THEN 1 END) as store_count
        FROM centers c
        LEFT JOIN store_access sa ON sa.center_id = c.id
        GROUP BY c.id
        ORDER BY c.name
    """).fetchall()
    return [dict(r) for r in rows]


def get_center_stores(center_id: int) -> list[dict]:
    """Магазины одного центра."""
    db = get_db()
    rows = db.execute(
        "SELECT * FROM store_access WHERE center_id = ? ORDER BY store_number",
        (center_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_stores_grouped_by_center() -> list[dict]:
    """Магазины сгруппированные по центрам (для логина)."""
    db = get_db()
    centers = db.execute("SELECT id, name FROM centers ORDER BY name").fetchall()
    result = []
    for c in centers:
        stores = db.execute(
            "SELECT store_number FROM store_access WHERE center_id = ? ORDER BY store_number",
            (c["id"],),
        ).fetchall()
        result.append({
            "id": c["id"],
            "name": c["name"],
            "stores": [s["store_number"] for s in stores],
        })
    # Магазины без центра
    orphans = db.execute(
        "SELECT store_number FROM store_access WHERE center_id IS NULL ORDER BY store_number"
    ).fetchall()
    if orphans:
        result.append({
            "id": None,
            "name": "Без центра",
            "stores": [s["store_number"] for s in orphans],
        })
    return result


def add_center(name: str) -> int:
    """Создаёт центр. Возвращает id."""
    db = get_db()
    cursor = db.execute("INSERT INTO centers (name) VALUES (?)", (name.strip(),))
    db.commit()
    return cursor.lastrowid


def rename_center(center_id: int, name: str):
    """Переименовывает центр."""
    db = get_db()
    db.execute("UPDATE centers SET name = ? WHERE id = ?", (name.strip(), center_id))
    db.commit()


def delete_center(center_id: int):
    """Удаляет центр. Магазины получают center_id = NULL."""
    db = get_db()
    db.execute("UPDATE store_access SET center_id = NULL WHERE center_id = ?", (center_id,))
    db.execute("DELETE FROM centers WHERE id = ?", (center_id,))
    db.commit()


def add_store_to_center(store_number: str, center_id: int, address: str = ""):
    """Добавляет магазин в центр. Создаёт запись store_access если нет."""
    db = get_db()
    existing = db.execute(
        "SELECT store_number FROM store_access WHERE store_number = ?",
        (store_number,),
    ).fetchone()
    if existing:
        db.execute(
            "UPDATE store_access SET center_id = ?, address = ? WHERE store_number = ?",
            (center_id, address, store_number),
        )
    else:
        db.execute(
            """INSERT INTO store_access (store_number, access_code, role, center_id, address)
               VALUES (?, ?, 'director', ?, ?)""",
            (store_number, store_number, center_id, address),
        )
    db.commit()


def import_centers_from_rows(rows: list[dict]) -> dict:
    """
    Массовый импорт центров и магазинов из Excel.
    rows: [{"center": "Центр Восток", "store": "10065", "address": "ул. Ленина 5"}, ...]
    Магазины, которых нет в файле, удаляются из центров (закрытые).
    Возвращает {"centers_created": N, "stores_added": N, "stores_removed": N}.
    """
    db = get_db()
    centers_created = 0
    stores_added = 0

    # Кэш центров name → id
    center_cache = {}
    for row in db.execute("SELECT id, name FROM centers").fetchall():
        center_cache[row["name"]] = row["id"]

    # Собираем все магазины из файла
    file_stores = set()

    for row in rows:
        center_name = (row.get("center") or "").strip()
        store_number = (row.get("store") or "").strip()
        address = (row.get("address") or "").strip()

        if not center_name or not store_number:
            continue

        file_stores.add(store_number)

        # Создаём центр если не существует
        if center_name not in center_cache:
            cursor = db.execute("INSERT INTO centers (name) VALUES (?)", (center_name,))
            center_cache[center_name] = cursor.lastrowid
            centers_created += 1

        center_id = center_cache[center_name]

        # Добавляем/обновляем магазин
        existing = db.execute(
            "SELECT store_number FROM store_access WHERE store_number = ?",
            (store_number,),
        ).fetchone()
        if existing:
            db.execute(
                "UPDATE store_access SET center_id = ?, address = ? WHERE store_number = ?",
                (center_id, address, store_number),
            )
        else:
            db.execute(
                """INSERT INTO store_access (store_number, access_code, role, center_id, address)
                   VALUES (?, ?, 'director', ?, ?)""",
                (store_number, store_number, center_id, address),
            )
        stores_added += 1

    # Удаляем магазины, которых нет в файле (закрытые), но не трогаем admin и center_manager
    all_db_stores = db.execute(
        "SELECT store_number FROM store_access WHERE store_number != 'admin' AND role != 'center_manager'"
    ).fetchall()
    stores_removed = 0
    for s in all_db_stores:
        if s["store_number"] not in file_stores:
            db.execute("DELETE FROM store_access WHERE store_number = ?", (s["store_number"],))
            stores_removed += 1

    db.commit()
    return {"centers_created": centers_created, "stores_added": stores_added, "stores_removed": stores_removed}


def remove_store(store_number: str):
    """Удаляет магазин из системы."""
    db = get_db()
    db.execute("DELETE FROM store_access WHERE store_number = ?", (store_number,))
    db.commit()


def get_center_for_store(store_number: str):
    """Возвращает center_id для магазина или None."""
    db = get_db()
    row = db.execute(
        "SELECT center_id FROM store_access WHERE store_number = ?",
        (store_number,),
    ).fetchone()
    return row["center_id"] if row else None


def get_stores_in_center(center_id: int) -> list[str]:
    """Список номеров магазинов в центре (без ДП)."""
    db = get_db()
    rows = db.execute(
        "SELECT store_number FROM store_access WHERE center_id = ? AND role != 'center_manager' ORDER BY store_number",
        (center_id,),
    ).fetchall()
    return [r["store_number"] for r in rows]


def set_center_manager(center_id: int, login_id: str, manager_name: str, access_code: str):
    """Создаёт/обновляет ДП для центра. Входит по номеру телефона."""
    db = get_db()
    # Удаляем старого ДП этого центра если есть
    db.execute(
        "DELETE FROM store_access WHERE center_id = ? AND role = 'center_manager'",
        (center_id,),
    )
    db.execute(
        """INSERT INTO store_access (store_number, access_code, role, center_id, manager_name)
           VALUES (?, ?, 'center_manager', ?, ?)""",
        (login_id, access_code, center_id, manager_name),
    )
    db.commit()


def remove_center_manager(center_id: int):
    """Убирает ДП из центра."""
    db = get_db()
    db.execute(
        "DELETE FROM store_access WHERE center_id = ? AND role = 'center_manager'",
        (center_id,),
    )
    db.commit()


def update_store_code(store_number: str, new_code: str):
    """Обновляет код доступа магазина."""
    db = get_db()
    db.execute(
        "UPDATE store_access SET access_code = ? WHERE store_number = ?",
        (new_code, store_number),
    )
    db.commit()


def update_store_role(store_number: str, role: str):
    """Обновляет роль магазина."""
    db = get_db()
    db.execute(
        "UPDATE store_access SET role = ? WHERE store_number = ?",
        (role, store_number),
    )
    db.commit()


# ── Продажи (по данным импортов) ──────────────────────────────────────────

def get_sales_with_expiry_status(days: int = 7) -> list[dict]:
    """Продажи по магазинам с разбивкой по статусу срока годности."""
    db = get_db()
    today = date.today()

    rows = db.execute("""
        SELECT sc.store_number, sc.article, ABS(sc.delta) as qty_sold,
               MIN(b.expiry_date) as worst_expiry
        FROM stock_changes sc
        JOIN stock_snapshots ss ON ss.id = sc.snapshot_id
        LEFT JOIN store_products sp
            ON sp.store_number = sc.store_number AND sp.article = sc.article
        LEFT JOIN batches b ON b.product_id = sp.id
        WHERE sc.delta < 0
          AND ss.imported_at >= datetime('now', ?)
        GROUP BY sc.id
    """, (f'-{days} days',)).fetchall()

    stores = {}
    for r in rows:
        store = r["store_number"]
        if store not in stores:
            stores[store] = {
                "store_number": store,
                "sold_normal": 0, "sold_d50": 0,
                "sold_d70": 0, "sold_expired": 0,
                "total_sold": 0,
            }
        qty = r["qty_sold"]
        stores[store]["total_sold"] += qty

        if r["worst_expiry"]:
            days_left = (date.fromisoformat(r["worst_expiry"]) - today).days
            if days_left <= 0:
                stores[store]["sold_expired"] += qty
            elif days_left <= config.DISCOUNT_THRESHOLDS["discount_70"]:
                stores[store]["sold_d70"] += qty
            elif days_left <= config.DISCOUNT_THRESHOLDS["discount_50"]:
                stores[store]["sold_d50"] += qty
            else:
                stores[store]["sold_normal"] += qty
        else:
            stores[store]["sold_normal"] += qty

    return sorted(stores.values(), key=lambda x: x["total_sold"], reverse=True)


def get_import_history(limit: int = 10) -> list[dict]:
    """История последних импортов."""
    db = get_db()
    rows = db.execute(
        "SELECT * FROM stock_snapshots ORDER BY imported_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


# ── Цены и потери ─────────────────────────────────────────────────────────

def save_price(article: str, price: int):
    db = get_db()
    db.execute(
        """INSERT INTO product_prices (article, price, fetched_at)
           VALUES (?, ?, CURRENT_TIMESTAMP)
           ON CONFLICT(article) DO UPDATE SET price = ?, fetched_at = CURRENT_TIMESTAMP""",
        (article, price, price),
    )
    db.commit()


def get_unfetched_articles() -> list[str]:
    """Артикулы без цены или с ценой старше 7 дней."""
    db = get_db()
    rows = db.execute("""
        SELECT DISTINCT sp.article
        FROM store_products sp
        LEFT JOIN product_prices pp ON sp.article = pp.article
        WHERE pp.article IS NULL
           OR pp.fetched_at < datetime('now', '-7 days')
    """).fetchall()
    return [r[0] for r in rows]


def get_prices_count() -> dict:
    db = get_db()
    total = db.execute("SELECT COUNT(DISTINCT article) FROM store_products").fetchone()[0]
    fetched = db.execute("SELECT COUNT(*) FROM product_prices WHERE price > 0").fetchone()[0]
    return {"total": total, "fetched": fetched}


def get_losses_report() -> dict:
    """Расчёт потерь по магазинам с привязкой к центрам."""
    db = get_db()

    # Строим маппинг магазин → центр
    center_map = {}
    sa_rows = db.execute("""
        SELECT sa.store_number, COALESCE(c.name, '') as center_name
        FROM store_access sa LEFT JOIN centers c ON c.id = sa.center_id
    """).fetchall()
    for sa in sa_rows:
        center_map[sa["store_number"]] = sa["center_name"]

    rows = db.execute("""
        SELECT
            sp.store_number,
            sp.article,
            sp.name,
            sp.available,
            pp.price,
            MIN(b.expiry_date) as worst_expiry
        FROM store_products sp
        JOIN batches b ON b.product_id = sp.id
        JOIN product_prices pp ON pp.article = sp.article AND pp.price > 0
        WHERE sp.no_expiry = 0
        GROUP BY sp.id
    """).fetchall()

    stores = {}
    totals = {"expired": 0, "d70": 0, "d50": 0, "total": 0}

    for r in rows:
        store = r[0]
        available = r[3]
        price = r[4]
        worst_expiry = r[5]

        if not worst_expiry:
            continue

        days_left = (date.fromisoformat(worst_expiry) - date.today()).days

        if days_left <= 0:
            loss = price * available
            cat = "expired"
        elif days_left <= config.DISCOUNT_THRESHOLDS["discount_70"]:
            loss = int(price * 0.70 * available)
            cat = "d70"
        elif days_left <= config.DISCOUNT_THRESHOLDS["discount_50"]:
            loss = int(price * 0.50 * available)
            cat = "d50"
        else:
            continue

        if store not in stores:
            stores[store] = {
                "store_number": store,
                "center_name": center_map.get(store, ""),
                "expired": 0, "d70": 0, "d50": 0, "total": 0,
            }
        stores[store][cat] += loss
        stores[store]["total"] += loss
        totals[cat] += loss
        totals["total"] += loss

    store_list = sorted(stores.values(), key=lambda x: x["total"], reverse=True)
    return {"stores": store_list, "totals": totals}
