"""
Точка входа: Flask + Telegram-бот в одном процессе.
Flask — главный поток, бот — фоновый.
"""

import os
import threading
import asyncio
import logging

import config
import database

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def run_bot():
    """Запускает Telegram-бота в отдельном потоке с собственным event loop."""
    from bot.handlers import create_bot_application

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    app = create_bot_application()

    async def start():
        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        logger.info("Telegram-бот запущен (polling)")
        # Держим бота работающим
        stop_event = asyncio.Event()
        await stop_event.wait()

    try:
        loop.run_until_complete(start())
    except Exception as e:
        logger.error(f"Ошибка бота: {e}")


_bot_started = False


def start_bot():
    """Запускает бота один раз (защита от повторного запуска gunicorn)."""
    global _bot_started
    if _bot_started:
        return
    _bot_started = True

    if config.BOT_TOKEN:
        bot_thread = threading.Thread(target=run_bot, daemon=True)
        bot_thread.start()
        logger.info("Бот-поток запущен")
    else:
        logger.warning("TELEGRAM_BOT_TOKEN не задан — бот не запущен")


def create_flask_app():
    """Создаёт Flask-приложение (для gunicorn)."""
    database.init_db()
    database.setup_store_access()
    os.makedirs(config.PHOTOS_DIR, exist_ok=True)
    logger.info("База данных инициализирована")

    start_bot()

    from web.routes import create_app
    return create_app()


# Для gunicorn: gunicorn app:flask_app
flask_app = create_flask_app()


def main():
    logger.info(f"Flask запущен на порту {config.FLASK_PORT}")
    flask_app.run(
        host="0.0.0.0",
        port=config.FLASK_PORT,
        debug=False,
        use_reloader=False,
    )


if __name__ == "__main__":
    main()
