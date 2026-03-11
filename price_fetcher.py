"""
Загрузка цен с kari.com по артикулам.
URL: https://kari.com/product/{article}/
Цена в __NEXT_DATA__ JSON: props.initialState.productCard.itemCard.price.current
"""

import re
import json
import time
import logging
import threading
import urllib.request
from typing import Optional

import database

log = logging.getLogger(__name__)

_fetch_lock = threading.Lock()
_fetch_status = {"running": False, "done": 0, "total": 0, "errors": 0}


def get_status() -> dict:
    return dict(_fetch_status)


def fetch_price(article: str) -> Optional[int]:
    """Получить цену одного товара с kari.com."""
    url = f"https://kari.com/product/{article}/"
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode("utf-8", errors="ignore")

        m = re.search(r'__NEXT_DATA__.*?>(.*?)</script>', html)
        if not m:
            return None

        data = json.loads(m.group(1))
        price_data = data["props"]["initialState"]["productCard"]["itemCard"]["price"]
        return int(price_data.get("current") or price_data.get("first") or 0)
    except Exception:
        return None


def fetch_all_prices():
    """Загрузить цены для всех артикулов без цены."""
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
                price = fetch_price(art)
                if price and price > 0:
                    database.save_price(art, price)
                else:
                    # Сохраняем 0 чтобы не загружать повторно
                    database.save_price(art, 0)
                    _fetch_status["errors"] += 1
                _fetch_status["done"] += 1
                time.sleep(0.3)
        finally:
            _fetch_status["running"] = False

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
