"""
Обработчики Telegram-бота.
Отчёты по срокам, inline-кнопки, ссылка на дашборд.
Автоматические триггеры и воронки напоминаний.
"""

import logging
from datetime import datetime, time as dtime

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
DASHBOARD_URL = config.DASHBOARD_URL or "https://kari-realizaciya.ru"


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


# ── Триггеры и воронки ────────────────────────────────────────────────────

async def trigger_morning_report(context: ContextTypes.DEFAULT_TYPE):
    """Утренний отчёт в 9:00 — общая картина по всем магазинам."""
    stores = database.get_all_stores_summary()
    activity = database.get_activity_summary()

    active_stores = {s["store_number"] for s in activity if s.get("last_activity")}
    total_products = sum(s["total"] for s in stores)
    total_not_filled = sum(s["not_filled"] for s in stores)
    total_expired = sum(s.get("ПРОСРОЧЕН", 0) for s in stores)
    total_70 = sum(s.get("Скидка 70%", 0) for s in stores)

    filled_pct = int((1 - total_not_filled / total_products) * 100) if total_products else 0

    lines = [
        "📊 Утренний отчёт\n",
        f"Заполненность: {filled_pct}% ({total_products - total_not_filled}/{total_products})",
    ]

    if total_expired > 0:
        lines.append(f"🔴 Просрочено: {total_expired} товаров")
    if total_70 > 0:
        lines.append(f"🟠 Скидка 70%: {total_70} товаров")

    # Магазины-лидеры (100% заполнено)
    leaders = [s for s in stores if s["total"] > 0 and s["not_filled"] == 0]
    if leaders:
        names = ", ".join(s["store_number"] for s in leaders)
        lines.append(f"\n✅ Всё заполнили: {names}")

    # Магазины с пробелами
    lagging = sorted(
        [s for s in stores if s["total"] > 0 and s["not_filled"] > 0],
        key=lambda s: s["not_filled"],
        reverse=True,
    )
    if lagging:
        lines.append(f"\n⚠️ Не заполнено:")
        for s in lagging[:10]:
            pct = int((1 - s["not_filled"] / s["total"]) * 100) if s["total"] else 0
            bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
            lines.append(f"  {s['store_number']}  {bar} {pct}%  ({s['not_filled']} тов.)")

    # Кто не заходил вообще
    try:
        all_valid = database.get_valid_stores()
    except Exception:
        all_valid = config.VALID_STORES
    never_logged = [st for st in all_valid if st not in active_stores]
    if never_logged:
        lines.append(f"\n🚫 Не заходили: {', '.join(sorted(never_logged))}")

    lines.append(f"\n🔗 {DASHBOARD_URL}")

    text = "\n".join(lines)
    try:
        for part in split_message(text):
            await context.bot.send_message(chat_id=config.CHAT_ID, text=part)
        logger.info("Утренний отчёт отправлен")
    except Exception as e:
        logger.error(f"Ошибка отправки утреннего отчёта: {e}")


async def trigger_evening_nudge(context: ContextTypes.DEFAULT_TYPE):
    """Вечернее напоминание в 16:00 — кто ещё не заполнил."""
    stores = database.get_all_stores_summary()
    activity = database.get_activity_summary()

    # Считаем кто сегодня работал (по московскому времени)
    active_today = database.get_today_active_stores(tz_offset_hours=3)

    # Магазины с незаполненными товарами
    lagging = [s for s in stores if s["total"] > 0 and s["not_filled"] > 0]

    if not lagging:
        await context.bot.send_message(
            chat_id=config.CHAT_ID,
            text="🎉 Все магазины заполнили сроки годности! Отличная работа!"
        )
        return

    lines = ["⏰ Напоминание\n"]

    # Кто не заходил сегодня и имеет пробелы
    not_active_today = [s for s in lagging if s["store_number"] not in active_today]
    active_but_incomplete = [s for s in lagging if s["store_number"] in active_today]

    if not_active_today:
        lines.append("❌ Не заходили сегодня:")
        for s in sorted(not_active_today, key=lambda x: x["not_filled"], reverse=True):
            lines.append(f"  🏪 {s['store_number']} — {s['not_filled']} товаров без данных")

    if active_but_incomplete:
        lines.append("\n📝 Заходили, но не всё заполнили:")
        for s in sorted(active_but_incomplete, key=lambda x: x["not_filled"], reverse=True):
            pct = int((1 - s["not_filled"] / s["total"]) * 100) if s["total"] else 0
            lines.append(f"  🏪 {s['store_number']} — осталось {s['not_filled']} тов. ({pct}% готово)")

    total_left = sum(s["not_filled"] for s in lagging)
    lines.append(f"\nВсего не заполнено: {total_left} товаров")
    lines.append(f"🔗 {DASHBOARD_URL}")

    text = "\n".join(lines)
    try:
        for part in split_message(text):
            await context.bot.send_message(chat_id=config.CHAT_ID, text=part)
        logger.info("Вечернее напоминание отправлено")
    except Exception as e:
        logger.error(f"Ошибка отправки вечернего напоминания: {e}")


async def trigger_weekly_rating(context: ContextTypes.DEFAULT_TYPE):
    """Еженедельный рейтинг в понедельник в 10:00."""
    stores = database.get_all_stores_summary()
    activity = database.get_activity_summary()

    # Рейтинг по заполненности
    rated = []
    for s in stores:
        if s["total"] > 0:
            pct = int((1 - s["not_filled"] / s["total"]) * 100)
            act = next((a for a in activity if a["store_number"] == s["store_number"]), {})
            rated.append({
                "store": s["store_number"],
                "pct": pct,
                "not_filled": s["not_filled"],
                "batches": act.get("batches_added", 0),
            })

    rated.sort(key=lambda x: x["pct"], reverse=True)

    lines = ["🏆 Еженедельный рейтинг магазинов\n"]

    # Топ-3
    medals = ["🥇", "🥈", "🥉"]
    for i, s in enumerate(rated[:3]):
        lines.append(f"{medals[i]} {s['store']} — {s['pct']}% заполнено")

    # Остальные
    if len(rated) > 3:
        lines.append("")
        for i, s in enumerate(rated[3:], start=4):
            marker = "✅" if s["pct"] == 100 else ("⚠️" if s["pct"] >= 50 else "❌")
            lines.append(f"{i}. {marker} {s['store']} — {s['pct']}%  ({s['not_filled']} не заполн.)")

    # Антирейтинг — кто вообще не начал
    zeros = [s for s in rated if s["pct"] == 0]
    if zeros:
        lines.append(f"\n🚨 Не начали заполнять:")
        for s in zeros:
            lines.append(f"  ❌ {s['store']}")

    lines.append(f"\n🔗 {DASHBOARD_URL}")

    text = "\n".join(lines)
    try:
        for part in split_message(text):
            await context.bot.send_message(chat_id=config.CHAT_ID, text=part)
        logger.info("Еженедельный рейтинг отправлен")
    except Exception as e:
        logger.error(f"Ошибка отправки рейтинга: {e}")


async def trigger_expired_alert(context: ContextTypes.DEFAULT_TYPE):
    """Алерт о просроченных товарах — каждый день в 11:00."""
    stores = database.get_all_stores_summary()

    urgent = [s for s in stores if s.get("ПРОСРОЧЕН", 0) > 0 or s.get("Скидка 70%", 0) > 0]
    if not urgent:
        return  # Нет срочных — молчим

    lines = ["🚨 ВНИМАНИЕ — срочные товары!\n"]

    for s in sorted(urgent, key=lambda x: x.get("ПРОСРОЧЕН", 0), reverse=True):
        expired = s.get("ПРОСРОЧЕН", 0)
        d70 = s.get("Скидка 70%", 0)
        parts = []
        if expired:
            parts.append(f"🔴 {expired} просрочено")
        if d70:
            parts.append(f"🟠 {d70} скидка 70%")
        lines.append(f"🏪 {s['store_number']}: {', '.join(parts)}")

    lines.append(f"\nТребуется: снять просроченное с продажи, выставить скидки!")
    lines.append(f"🔗 {DASHBOARD_URL}")

    text = "\n".join(lines)
    try:
        for part in split_message(text):
            await context.bot.send_message(chat_id=config.CHAT_ID, text=part)
        logger.info("Алерт о просрочке отправлен")
    except Exception as e:
        logger.error(f"Ошибка отправки алерта: {e}")


# ── Ручная команда для тестирования триггеров ────────────────────────────

async def cmd_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Тест триггера: /trigger morning|evening|weekly|expired"""
    args = context.args
    if not args:
        await update.message.reply_text(
            "Тест триггеров:\n"
            "/trigger morning — утренний отчёт\n"
            "/trigger evening — вечернее напоминание\n"
            "/trigger weekly — еженедельный рейтинг\n"
            "/trigger expired — алерт о просрочке"
        )
        return

    trigger = args[0].lower()
    triggers = {
        "morning": trigger_morning_report,
        "evening": trigger_evening_nudge,
        "weekly": trigger_weekly_rating,
        "expired": trigger_expired_alert,
    }

    func = triggers.get(trigger)
    if func:
        await func(context)
        await update.message.reply_text(f"Триггер '{trigger}' выполнен")
    else:
        await update.message.reply_text(f"Неизвестный триггер: {trigger}")


# ── Создание Application ──────────────────────────────────────────────────

def create_bot_application():
    import pytz
    tz = pytz.timezone(config.TIMEZONE)

    app = Application.builder().token(config.BOT_TOKEN).build()

    # Команды
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("dashboard", cmd_dashboard))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CommandHandler("urgent", cmd_urgent))
    app.add_handler(CommandHandler("trigger", cmd_trigger))

    # Расписание триггеров (Moscow time)
    jq = app.job_queue

    # Утренний отчёт — 9:00 каждый день
    jq.run_daily(trigger_morning_report, time=dtime(9, 0, tzinfo=tz), name="morning_report")

    # Алерт о просрочке — 11:00 каждый день
    jq.run_daily(trigger_expired_alert, time=dtime(11, 0, tzinfo=tz), name="expired_alert")

    # Вечернее напоминание — 16:00 каждый день
    jq.run_daily(trigger_evening_nudge, time=dtime(16, 0, tzinfo=tz), name="evening_nudge")

    # Еженедельный рейтинг — понедельник 10:00
    jq.run_daily(
        trigger_weekly_rating,
        time=dtime(10, 0, tzinfo=tz),
        days=(0,),  # 0 = Monday
        name="weekly_rating",
    )

    logger.info("Триггеры настроены: 9:00 отчёт, 11:00 алерт, 16:00 напоминание, ПН 10:00 рейтинг")

    return app
