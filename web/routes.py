"""Flask-маршруты для веб-дашборда."""

import os
import time
import tempfile
from functools import wraps
from datetime import datetime
from collections import defaultdict

from flask import (
    Flask, render_template, request, redirect, url_for,
    session, flash, jsonify,
)

import config
import database

try:
    import price_fetcher
except Exception:
    price_fetcher = None

# ── Защита от перебора кодов ──────────────────────────────────────────────
_login_attempts = defaultdict(list)  # IP → [timestamps]
MAX_LOGIN_ATTEMPTS = 5
LOGIN_BLOCK_SECONDS = 300  # 5 минут блокировки


def _is_blocked(ip: str) -> bool:
    """Проверяет, заблокирован ли IP после множества неудачных попыток."""
    now = time.time()
    _login_attempts[ip] = [t for t in _login_attempts[ip] if now - t < LOGIN_BLOCK_SECONDS]
    return len(_login_attempts[ip]) >= MAX_LOGIN_ATTEMPTS


def _record_failed(ip: str):
    _login_attempts[ip].append(time.time())


def _clear_attempts(ip: str):
    _login_attempts.pop(ip, None)


def create_app() -> Flask:
    app = Flask(
        __name__,
        template_folder=os.path.join(os.path.dirname(__file__), "..", "templates"),
        static_folder=os.path.join(os.path.dirname(__file__), "..", "static"),
    )
    app.secret_key = config.FLASK_SECRET_KEY
    app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16 MB

    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

    # ── Gzip-сжатие ответов ──────────────────────────────────────────────
    import gzip as _gzip
    import io as _io

    @app.after_request
    def compress_response(response):
        if (response.status_code < 200 or response.status_code >= 300
                or response.direct_passthrough
                or "Content-Encoding" in response.headers
                or "gzip" not in request.headers.get("Accept-Encoding", "")):
            return response
        ct = response.content_type or ""
        if not (ct.startswith("text/") or "json" in ct or "javascript" in ct):
            return response
        data = response.get_data()
        if len(data) < 512:
            return response
        buf = _io.BytesIO()
        with _gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=6) as gz:
            gz.write(data)
        response.set_data(buf.getvalue())
        response.headers["Content-Encoding"] = "gzip"
        response.headers["Content-Length"] = len(response.get_data())
        response.headers["Vary"] = "Accept-Encoding"
        return response

    # ── Заголовки безопасности ─────────────────────────────────────────────

    @app.after_request
    def add_security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        return response

    # ── Авторизация ────────────────────────────────────────────────────────

    def login_required(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if "role" not in session:
                return redirect(url_for("login"))
            return f(*args, **kwargs)
        return decorated

    def admin_required(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if session.get("role") != "admin":
                flash("Доступ только для администратора", "danger")
                return redirect(url_for("dashboard"))
            return f(*args, **kwargs)
        return decorated

    # ── Маршруты ───────────────────────────────────────────────────────────

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            ip = request.remote_addr or "unknown"

            if _is_blocked(ip):
                remaining = int(LOGIN_BLOCK_SECONDS - (time.time() - min(_login_attempts[ip])))
                flash(f"Слишком много попыток. Подождите {remaining // 60 + 1} мин.", "danger")
                return render_template("login.html")

            store = request.form.get("store_number", "").strip()
            code = request.form.get("access_code", "").strip()

            result = database.check_access(store, code)
            if result:
                _clear_attempts(ip)
                session["store_number"] = result["store_number"]
                session["role"] = result["role"]
                # Сохраняем center_id в сессию для center_manager
                if result["role"] not in ("admin",):
                    cid = database.get_center_for_store(result["store_number"])
                    if cid:
                        session["center_id"] = cid
                # Проверяем дефолтный пароль (код == номер магазина)
                if result["role"] != "admin" and code == store:
                    flash("Смените стандартный код доступа!", "warning")
                database.log_activity(result["store_number"], "login", f"Вход в систему")
                if result["role"] in ("admin", "center_manager"):
                    return redirect(url_for("dashboard"))
                return redirect(url_for("store_detail", store_number=result["store_number"]))

            _record_failed(ip)
            attempts_left = MAX_LOGIN_ATTEMPTS - len(_login_attempts[ip])
            if attempts_left > 0:
                flash(f"Неверный код доступа (осталось {attempts_left} попыток)", "danger")
            else:
                flash(f"Доступ заблокирован на {LOGIN_BLOCK_SECONDS // 60} минут", "danger")

        return render_template("login.html")

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.route("/")
    @login_required
    def dashboard():
        role = session.get("role")
        if role == "admin":
            stores = database.get_all_stores_summary()
            return render_template("dashboard.html", stores=stores)
        if role == "center_manager":
            stores = database.get_all_stores_summary()
            cid = session.get("center_id")
            stores = [s for s in stores if s.get("center_id") == cid]
            return render_template("dashboard.html", stores=stores)
        return redirect(url_for("store_detail", store_number=session["store_number"]))

    @app.route("/store/<store_number>")
    @login_required
    def store_detail(store_number):
        role = session.get("role")
        if role == "admin":
            pass  # доступ ко всем
        elif role == "center_manager":
            cid = session.get("center_id")
            store_cid = database.get_center_for_store(store_number)
            if store_cid != cid:
                flash("Доступ только к магазинам вашего центра", "danger")
                return redirect(url_for("dashboard"))
        elif session.get("store_number") != store_number:
            flash("Доступ только к своему магазину", "danger")
            return redirect(url_for("dashboard"))

        filter_status = request.args.get("filter")
        stats = database.get_store_stats(store_number)
        products = database.get_store_products(store_number, filter_status)

        return render_template(
            "store_detail.html",
            store_number=store_number,
            stats=stats,
            products=products,
            current_filter=filter_status,
            styles=config.CATEGORY_STYLES,
        )

    # ── API: Добавить партию ──────────────────────────────────────────────

    @app.route("/api/batch/add", methods=["POST"])
    @login_required
    def api_add_batch():
        data = request.get_json() or request.form
        product_id = data.get("product_id")
        production_date = data.get("production_date", "").strip()
        shelf_life_months = data.get("shelf_life_months")

        if not product_id or not production_date or not shelf_life_months:
            return jsonify({"error": "Заполните все поля"}), 400

        try:
            product_id = int(product_id)
            shelf_life_months = int(shelf_life_months)
        except (ValueError, TypeError):
            return jsonify({"error": "Неверные данные"}), 400

        if shelf_life_months < 1 or shelf_life_months > 120:
            return jsonify({"error": "Срок: от 1 до 120 месяцев"}), 400

        try:
            datetime.strptime(production_date, "%Y-%m-%d")
        except ValueError:
            return jsonify({"error": "Неверный формат даты"}), 400

        product = database.get_product_by_id(product_id)
        if not product:
            return jsonify({"error": "Товар не найден"}), 404

        if session.get("role") != "admin":
            if product["store_number"] != session.get("store_number"):
                return jsonify({"error": "Нет доступа"}), 403

        quantity = 0
        try:
            quantity = int(data.get("quantity", 0))
        except (ValueError, TypeError):
            quantity = 0

        batch_id = database.add_batch(product_id, production_date, shelf_life_months, quantity)
        store = session.get("store_number", "?")
        database.log_activity(store, "add_batch",
                              f"Товар #{product_id}: {production_date}, {shelf_life_months} мес., {quantity} шт.")
        return jsonify({"ok": True, "batch_id": batch_id})

    @app.route("/api/batch/delete", methods=["POST"])
    @login_required
    def api_delete_batch():
        data = request.get_json() or request.form
        batch_id = data.get("batch_id")

        if not batch_id:
            return jsonify({"error": "Не указана партия"}), 400

        try:
            batch_id = int(batch_id)
        except (ValueError, TypeError):
            return jsonify({"error": "Неверные данные"}), 400

        database.delete_batch(batch_id)
        store = session.get("store_number", "?")
        database.log_activity(store, "delete_batch", f"Удалена партия #{batch_id}")
        return jsonify({"ok": True})

    @app.route("/api/product/no-expiry", methods=["POST"])
    @login_required
    def api_no_expiry():
        data = request.get_json() or request.form
        product_id = data.get("product_id")
        value = data.get("value", True)

        if not product_id:
            return jsonify({"error": "Не указан товар"}), 400

        try:
            product_id = int(product_id)
        except (ValueError, TypeError):
            return jsonify({"error": "Неверные данные"}), 400

        if isinstance(value, str):
            value = value.lower() in ("true", "1", "yes")

        product = database.get_product_by_id(product_id)
        if not product:
            return jsonify({"error": "Товар не найден"}), 404

        if session.get("role") != "admin":
            if product["store_number"] != session.get("store_number"):
                return jsonify({"error": "Нет доступа"}), 403

        database.mark_no_expiry(product_id, value)
        store = session.get("store_number", "?")
        action = "no_expiry" if value else "undo_no_expiry"
        database.log_activity(store, action, f"Товар #{product_id}")
        return jsonify({"ok": True})

    # ── API ───────────────────────────────────────────────────────────────

    @app.route("/api/stores")
    @login_required
    def api_stores():
        return jsonify(database.get_all_stores_summary())

    @app.route("/api/store/<store_number>")
    @login_required
    def api_store(store_number):
        role = session.get("role")
        if role == "admin":
            pass
        elif role == "center_manager":
            cid = session.get("center_id")
            if database.get_center_for_store(store_number) != cid:
                return jsonify({"error": "Нет доступа"}), 403
        elif session.get("store_number") != store_number:
            return jsonify({"error": "Нет доступа"}), 403
        filter_status = request.args.get("filter")
        return jsonify(database.get_store_products(store_number, filter_status))

    # ── Коды доступа (админ) ─────────────────────────────────────────────

    @app.route("/codes")
    @login_required
    @admin_required
    def access_codes():
        codes = database.get_access_codes()
        stores = database.get_all_stores_summary()
        return render_template("dashboard.html",
                               stores=stores,
                               codes=codes, show_codes=True)

    # ── Журнал активности (админ) ─────────────────────────────────────────

    @app.route("/activity")
    @login_required
    def activity():
        role = session.get("role")
        if role not in ("admin", "center_manager"):
            flash("Доступ только для администратора", "danger")
            return redirect(url_for("dashboard"))

        store_filter = request.args.get("store")
        summary = database.get_activity_summary()
        log = database.get_activity_log(200, store_filter)
        stores_completion = database.get_all_stores_summary()
        try:
            losses = database.get_losses_report()
            prices_count = database.get_prices_count()
        except Exception:
            losses = {"stores": [], "totals": {"expired": 0, "d70": 0, "d50": 0, "total": 0}}
            prices_count = {"total": 0, "fetched": 0}
        fetch_status = price_fetcher.get_status() if price_fetcher else {"running": False, "done": 0, "total": 0, "errors": 0}
        try:
            sales_summary = database.get_sales_with_expiry_status(days=7)
            import_history = database.get_import_history(5)
        except Exception:
            sales_summary = []
            import_history = []

        # Фильтр данных для директора подразделения — только его центр
        if role == "center_manager":
            cid = session.get("center_id")
            center_stores = set(database.get_stores_in_center(cid)) if cid else set()
            stores_completion = [s for s in stores_completion if s["store_number"] in center_stores]
            summary = [s for s in summary if s.get("store_number") in center_stores]
            log = [l for l in log if l.get("store_number") in center_stores]
            if isinstance(losses, dict) and "stores" in losses:
                losses["stores"] = [s for s in losses["stores"] if s.get("store") in center_stores]
                # Пересчитаем итоги
                totals = {"expired": 0, "d70": 0, "d50": 0, "total": 0}
                for s in losses["stores"]:
                    for k in totals:
                        totals[k] += s.get(k, 0)
                losses["totals"] = totals
            sales_summary = [s for s in sales_summary if s.get("store_number") in center_stores]

        return render_template("activity.html",
                               summary=summary, log=log,
                               store_filter=store_filter,
                               stores_completion=stores_completion,
                               losses=losses,
                               prices_count=prices_count,
                               fetch_status=fetch_status,
                               sales_summary=sales_summary,
                               import_history=import_history)

    @app.route("/api/fetch-prices", methods=["POST"])
    @login_required
    @admin_required
    def api_fetch_prices():
        if not price_fetcher:
            return jsonify({"error": "price_fetcher not available"}), 500
        price_fetcher.fetch_all_prices()
        return jsonify({"ok": True, "status": price_fetcher.get_status()})

    @app.route("/api/fetch-prices/status")
    @login_required
    @admin_required
    def api_fetch_prices_status():
        if not price_fetcher:
            return jsonify({"running": False, "done": 0, "total": 0, "errors": 0})
        return jsonify(price_fetcher.get_status())

    # ── Загрузка данных (админ) ──────────────────────────────────────────

    @app.route("/upload", methods=["GET", "POST"])
    @login_required
    @admin_required
    def upload():
        if request.method == "POST":
            stock_file = request.files.get("stock_file")
            catalog_file = request.files.get("catalog_file")

            if not stock_file or not stock_file.filename.endswith((".xlsx", ".xls")):
                flash("Загрузите файл остатков (.xlsx)", "danger")
                return redirect(url_for("upload"))

            stock_tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
            stock_path = stock_tmp.name
            stock_file.save(stock_path)
            stock_tmp.close()

            catalog_path = None
            if catalog_file and catalog_file.filename:
                catalog_tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
                catalog_path = catalog_tmp.name
                catalog_file.save(catalog_path)
                catalog_tmp.close()

            try:
                from import_data import read_stock, read_catalog

                catalog = {}
                if catalog_path:
                    catalog = read_catalog(catalog_path)

                stock_rows = read_stock(stock_path)
                if not stock_rows:
                    flash("Нет данных для импорта", "danger")
                    return redirect(url_for("upload"))

                result = database.import_stock(stock_rows, catalog, filename=stock_file.filename)
                flash(
                    f"Импорт: обновлено {result['updated']}, "
                    f"добавлено {result['added']}, "
                    f"обнулено {result['zeroed']}, "
                    f"изменений {result['total_changes']}",
                    "success",
                )
                database.log_activity(
                    "admin", "import_stock",
                    f"Файл: {stock_file.filename}. "
                    f"Обновлено: {result['updated']}, добавлено: {result['added']}, "
                    f"обнулено: {result['zeroed']}")
                return redirect(url_for("dashboard"))

            except Exception as e:
                flash(f"Ошибка импорта: {e}", "danger")
                return redirect(url_for("upload"))
            finally:
                if os.path.exists(stock_path):
                    os.unlink(stock_path)
                if catalog_path and os.path.exists(catalog_path):
                    os.unlink(catalog_path)

        return render_template("upload.html")

    # ── Центры (админ) ──────────────────────────────────────────────────

    @app.route("/centers")
    @login_required
    @admin_required
    def centers():
        center_list = database.get_centers()
        for c in center_list:
            stores = database.get_center_stores(c["id"])
            c["stores"] = stores
            c["manager"] = next((s for s in stores if s["role"] == "center_manager"), None)
        return render_template("centers.html", centers=center_list)

    @app.route("/api/center/add", methods=["POST"])
    @login_required
    @admin_required
    def api_add_center():
        data = request.get_json() or request.form
        name = (data.get("name") or "").strip()
        if not name:
            return jsonify({"error": "Укажите название центра"}), 400
        try:
            center_id = database.add_center(name)
            database.log_activity("admin", "add_center", f"Центр «{name}»")
            return jsonify({"ok": True, "center_id": center_id})
        except Exception as e:
            return jsonify({"error": str(e)}), 400

    @app.route("/api/center/rename", methods=["POST"])
    @login_required
    @admin_required
    def api_rename_center():
        data = request.get_json() or request.form
        center_id = data.get("center_id")
        name = (data.get("name") or "").strip()
        if not center_id or not name:
            return jsonify({"error": "Укажите id и название"}), 400
        try:
            database.rename_center(int(center_id), name)
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"error": str(e)}), 400

    @app.route("/api/center/delete", methods=["POST"])
    @login_required
    @admin_required
    def api_delete_center():
        data = request.get_json() or request.form
        center_id = data.get("center_id")
        if not center_id:
            return jsonify({"error": "Укажите id центра"}), 400
        database.delete_center(int(center_id))
        database.log_activity("admin", "delete_center", f"Центр #{center_id}")
        return jsonify({"ok": True})

    @app.route("/api/center/add-store", methods=["POST"])
    @login_required
    @admin_required
    def api_add_store_to_center():
        data = request.get_json() or request.form
        store_number = (data.get("store_number") or "").strip()
        center_id = data.get("center_id")
        address = (data.get("address") or "").strip()
        if not store_number or not center_id:
            return jsonify({"error": "Укажите магазин и центр"}), 400
        database.add_store_to_center(store_number, int(center_id), address)
        database.log_activity("admin", "add_store",
                              f"Магазин {store_number} → центр #{center_id}")
        return jsonify({"ok": True})

    @app.route("/api/center/import", methods=["POST"])
    @login_required
    @admin_required
    def api_import_centers():
        """Импорт центров и магазинов из Excel.

        Поддерживает два формата:
        1. Колонки: Центр, Магазин, Адрес
        2. Колонки: Магазин, ТЦ — название центра из имени файла
        """
        file = request.files.get("file")
        if not file or not file.filename.endswith((".xlsx", ".xls")):
            return jsonify({"error": "Загрузите файл .xlsx"}), 400

        tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
        tmp_path = tmp.name
        file.save(tmp_path)
        tmp.close()

        try:
            import openpyxl
            import re
            wb = openpyxl.load_workbook(tmp_path, read_only=True)
            ws = wb.active

            # Ищем строку-заголовок (в первых 5 строках)
            all_rows = list(ws.iter_rows(values_only=True))
            wb.close()

            header_idx = None
            headers = {}
            for ridx, row in enumerate(all_rows[:5]):
                for cidx, cell in enumerate(row):
                    val = str(cell or "").strip().lower()
                    if val in ("центр", "center", "подразделение"):
                        headers["center"] = cidx
                    elif val in ("магазин", "store", "номер", "номер магазина"):
                        headers["store"] = cidx
                    elif val in ("адрес", "address", "тц"):
                        headers["address"] = cidx
                if "store" in headers:
                    header_idx = ridx
                    break

            if header_idx is None or "store" not in headers:
                return jsonify({"error": "Нужна колонка «Магазин»"}), 400

            # Если нет колонки «Центр», извлекаем название из имени файла
            center_from_filename = ""
            if "center" not in headers:
                fname = os.path.splitext(file.filename)[0]
                # Ищем "Центр N" или "Центр Name" в имени файла
                m = re.search(r'[Цц]ентр\s*(.+)', fname)
                if m:
                    center_from_filename = "Центр " + m.group(1).strip().rstrip("_. ")
                else:
                    center_from_filename = fname.strip()

            rows = []
            for row in all_rows[header_idx + 1:]:
                store_idx = headers["store"]
                if store_idx >= len(row) or not row[store_idx]:
                    continue

                store_val = row[store_idx]
                store_str = str(int(store_val) if isinstance(store_val, float) else store_val).strip()
                if not store_str or not any(c.isdigit() for c in store_str):
                    continue

                # Центр: из колонки или из имени файла
                if "center" in headers and headers["center"] < len(row) and row[headers["center"]]:
                    center_name = str(row[headers["center"]]).strip()
                else:
                    center_name = center_from_filename

                # Адрес
                addr_idx = headers.get("address", 999)
                address_val = row[addr_idx] if addr_idx < len(row) and row[addr_idx] else ""

                if center_name:
                    rows.append({
                        "center": center_name,
                        "store": store_str,
                        "address": str(address_val or "").strip(),
                    })

            if not rows:
                return jsonify({"error": "Нет данных для импорта"}), 400

            result = database.import_centers_from_rows(rows)
            database.log_activity("admin", "import_centers",
                                  f"Импорт: {result['centers_created']} центров, {result['stores_added']} магазинов")
            return jsonify({"ok": True, **result})

        except Exception as e:
            return jsonify({"error": f"Ошибка: {e}"}), 400
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    @app.route("/api/center/remove-store", methods=["POST"])
    @login_required
    @admin_required
    def api_remove_store():
        data = request.get_json() or request.form
        store_number = (data.get("store_number") or "").strip()
        if not store_number:
            return jsonify({"error": "Укажите магазин"}), 400
        database.remove_store(store_number)
        database.log_activity("admin", "remove_store", f"Магазин {store_number}")
        return jsonify({"ok": True})

    @app.route("/api/center/set-manager", methods=["POST"])
    @login_required
    @admin_required
    def api_set_center_manager():
        data = request.get_json() or request.form
        center_id = data.get("center_id")
        manager_name = (data.get("manager_name") or "").strip()
        phone = (data.get("phone") or "").strip()
        access_code = (data.get("access_code") or "").strip()
        if not center_id or not manager_name or not phone or not access_code:
            return jsonify({"error": "Заполните ФИО, телефон и код доступа"}), 400
        # ДП входит по номеру телефона — phone используется как store_number (логин)
        database.set_center_manager(int(center_id), phone, manager_name, access_code)
        database.log_activity("admin", "set_manager", f"ДП {manager_name} (тел: {phone}) → центр #{center_id}")
        return jsonify({"ok": True})

    @app.route("/api/center/remove-manager", methods=["POST"])
    @login_required
    @admin_required
    def api_remove_center_manager():
        data = request.get_json() or request.form
        center_id = data.get("center_id")
        if not center_id:
            return jsonify({"error": "Укажите центр"}), 400
        database.remove_center_manager(int(center_id))
        database.log_activity("admin", "remove_manager", f"Убран ДП центра #{center_id}")
        return jsonify({"ok": True})

    @app.route("/api/store/update-code", methods=["POST"])
    @login_required
    @admin_required
    def api_update_store_code():
        data = request.get_json() or request.form
        store_number = (data.get("store_number") or "").strip()
        new_code = (data.get("new_code") or "").strip()
        if not store_number or not new_code:
            return jsonify({"error": "Укажите магазин и новый код"}), 400
        database.update_store_code(store_number, new_code)
        return jsonify({"ok": True})

    return app
