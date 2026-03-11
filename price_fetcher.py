"""
Загрузка цен и наличия с kari.com по артикулам.
URL: https://kari.com/product/{article}/
Данные в __NEXT_DATA__ JSON: props.initialState.productCard.itemCard
"""

import re
import json
import time
import logging
import threading
import urllib.request
import urllib.error
from typing import Optional, Tuple

import database

log = logging.getLogger(__name__)

_fetch_lock = threading.Lock()
_fetch_status = {"running": False, "done": 0, "total": 0, "errors": 0}


def get_status() -> dict:
    return dict(_fetch_status)


def fetch_price_and_availability(article: str) -> Tuple[Optional[int], Optional[bool]]:
    """Получить цену и наличие товара с kari.com.
    Возвращает (price, is_available). None = данные не получены."""
    url = f"https://kari.com/product/{article}/"
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode("utf-8", errors="ignore")

        m = re.search(r'__NEXT_DATA__.*?>(.*?)</script>', html)
        if not m:
            return None, False

        data = json.loads(m.group(1))
        item_card = data["props"]["initialState"]["productCard"]["itemCard"]

        # Цена
        price_data = item_card.get("price", {})
        price = int(price_data.get("current") or price_data.get("first") or 0)

        # Наличие онлайн — проверяем разные поля
        is_available = False
        if item_card.get("isAvailable") or item_card.get("inStock"):
            is_available = True
        elif price > 0:
            # Проверяем наличие по размерам
            sizes = item_card.get("sizes", [])
            if sizes:
                is_available = any(
                    s.get("isAvailable") or s.get("quantity", 0) > 0
                    for s in sizes
                )
            else:
                is_available = True  # Есть цена — скорее всего доступен

        return price, is_available
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None, False  # Товара нет на сайте
        return None, None
    except Exception:
        return None, None


def fetch_price(article: str) -> Optional[int]:
    """Получить цену одного товара с kari.com (совместимость)."""
    price, _ = fetch_price_and_availability(article)
    return price


def fetch_all_prices():
    """Загрузить цены и наличие для всех артикулов без цены."""
    if _fetch_status["running"]:
        return

    try:
        articles = database.get_unfetched_articles()
    except Exception as e:
        log.error(f"Error getting articles: {e}")
        articles = []

    log.info(f"Found {len(articles)} unfetched articles")
    if not articles:
        return

    _fetch_status["running"] = True
    _fetch_status["done"] = 0
    _fetch_status["total"] = len(articles)
    _fetch_status["errors"] = 0

    def _worker():
        try:
            for art in articles:
                price, is_available = fetch_price_and_availability(art)
                if price and price > 0:
                    database.save_price(art, price)
                else:
                    # Сохраняем 0 чтобы не загружать повторно
                    database.save_price(art, 0)
                    _fetch_status["errors"] += 1
                # Сохраняем наличие онлайн
                if is_available is not None:
                    database.save_online_availability(art, is_available)
                _fetch_status["done"] += 1
                time.sleep(0.3)
        finally:
            _fetch_status["running"] = False

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
