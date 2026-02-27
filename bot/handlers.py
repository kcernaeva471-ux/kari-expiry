"""
Обработчики Telegram-бота.
Отчёты по срокам, inline-кнопки, ссылка на дашборд.
"""

import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

import config
import database

logger = logging.getLogger(__name__)

MAX_MSG = 4096


def split_message(text, max_len=MAX_MSG):
    if len(text) <= max_len:
        return [text]
    parts, current = [], ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > max_len:
            parts.append(current)
            current = line
        else:
            current = current + "\n" + line if current else line
    if current:
        parts.append(current)
    return parts


# ── Команды ────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.effective_user.full_name or ""
    await update.message.reply_text(
        f"Привет, {name}!\n\n"
        "Я отслеживаю сроки годности товаров по акции Kari.\n\n"
        "Команды:\n"
        "/report <магазин> — отчёт по магазину\n"
        "/urgent <магазин> — срочные товары\n"
        "/dashboard — веб-дашборд\n"
        "/help — помощь",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Инструкция:\n\n"
        "1. Откройте дашборд: /dashboard\n"
        "2. Заполните даты производства и сроки годности\n"
        "3. Бот покажет отчёт по команде /report\n\n"
        "Скидки:\n"
        "  70% — до 90 дней\n"
        "  50% — 91–180 дней\n"
        "  Просрочен — снять с продажи\n",
    )


async def cmd_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = config.DASHBOARD_URL
    await update.message.reply_text(
        f"Дашборд: {url}\n\n"
        "Войдите с кодом доступа вашего магазина.",
    )


async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отчёт по магазину: /report 11701"""
    args = context.args
    if not args:
        await update.message.reply_text(
            "Укажите номер магазина:\n/report 11701"
        )
        return

    store = args[0].strip()
    if store not in config.VALID_STORES:
        await update.message.reply_text(f"Магазин {store} не найден")
        return

    stats = database.get_store_stats(store)
    products = database.get_store_products(store, "urgent")

    lines = [
        f"Магазин {store}\n",
        f"Всего товаров: {stats['total']}",
        f"Не заполнено: {stats['not_filled']}",
        f"Без срока: {stats['no_expiry']}",
        f"Заполнено: {stats['filled']}",
        "",
        f"ПРОСРОЧЕН: {stats.get('ПРОСРОЧЕН', 0)}",
        f"Скидка 70%: {stats.get('Скидка 70%', 0)}",
        f"Скидка 50%: {stats.get('Скидка 50%', 0)}",
        f"В норме: {stats.get('В норме', 0)}",
    ]

    if products:
        lines.append(f"\nСрочные товары ({len(products)}):\n")
        for p in products[:15]:
            status_emoji = ""
            if p["status"] == "ПРОСРОЧЕН":
                status_emoji = "🔴"
            elif p["status"] == "Скидка 70%":
                status_emoji = "🟠"
            elif p["status"] == "Скидка 50%":
                status_emoji = "🟡"

            days_str = ""
            if p["worst_days"] is not None and p["worst_days"] < 99999:
                days_str = f" ({p['worst_days']} дн.)"

            lines.append(f"{status_emoji} {p['article']} {p['name']}{days_str}")

        if len(products) > 15:
            lines.append(f"\n...и ещё {len(products) - 15}")

    text = "\n".join(lines)

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Дашборд", url=config.DASHBOARD_URL)],
    ])

    for part in split_message(text):
        await update.message.reply_text(part, reply_markup=keyboard)


async def cmd_urgent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Срочные товары: /urgent 11701"""
    args = context.args
    if not args:
        # Показываем сводку по всем
        stores = database.get_all_stores_summary()
        lines = ["Срочные товары по магазинам:\n"]
        for s in stores:
            expired = s.get("ПРОСРОЧЕН", 0)
            d70 = s.get("Скидка 70%", 0)
            urgent_total = expired + d70
            if urgent_total > 0:
                lines.append(f"🏪 {s['store_number']}: 🔴{expired} 🟠{d70}")
        if len(lines) == 1:
            lines.append("Нет срочных товаров")
        await update.message.reply_text("\n".join(lines))
        return

    store = args[0].strip()
    # Перенаправляем на report
    context.args = [store]
    await cmd_report(update, context)


# ── Создание Application ──────────────────────────────────────────────────

def create_bot_application():
    app = Application.builder().token(config.BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("dashboard", cmd_dashboard))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CommandHandler("urgent", cmd_urgent))

    return app
