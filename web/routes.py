"""Flask-маршруты для веб-дашборда."""

import os
import tempfile
from functools import wraps
from datetime import datetime

from flask import (
    Flask, render_template, request, redirect, url_for,
    session, flash, jsonify,
)

import config
import database


def create_app() -> Flask:
    app = Flask(
        __name__,
        template_folder=os.path.join(os.path.dirname(__file__), "..", "templates"),
        static_folder=os.path.join(os.path.dirname(__file__), "..", "static"),
    )
    app.secret_key = config.FLASK_SECRET_KEY
    app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16 MB

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
            store = request.form.get("store_number", "").strip()
            code = request.form.get("access_code", "").strip()

            result = database.check_access(store, code)
            if result:
                session["store_number"] = result["store_number"]
                session["role"] = result["role"]
                if result["role"] == "admin":
                    return redirect(url_for("dashboard"))
                return redirect(url_for("store_detail", store_number=result["store_number"]))

            flash("Неверный код доступа", "danger")

        return render_template("login.html", stores=config.VALID_STORES)

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.route("/")
    @login_required
    def dashboard():
        if session.get("role") == "admin":
            stores = database.get_all_stores_summary()
            return render_template("dashboard.html", stores=stores)
        return redirect(url_for("store_detail", store_number=session["store_number"]))

    @app.route("/store/<store_number>")
    @login_required
    def store_detail(store_number):
        if session.get("role") != "admin" and session.get("store_number") != store_number:
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

        batch_id = database.add_batch(product_id, production_date, shelf_life_months)
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
        return jsonify({"ok": True})

    # ── API ───────────────────────────────────────────────────────────────

    @app.route("/api/stores")
    def api_stores():
        return jsonify(database.get_all_stores_summary())

    @app.route("/api/store/<store_number>")
    def api_store(store_number):
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

                count = database.import_stock(stock_rows, catalog)
                flash(f"Импортировано {count} товаров", "success")
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

    return app
