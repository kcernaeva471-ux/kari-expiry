"""
Telegram-бот: Учёт сроков годности — Акция Kari.

Принимает заполненные Excel-шаблоны от магазинов,
формирует отчёт по скидкам и отправляет в общий чат.

Команды:
  /start   — приветствие и инструкция
  /help    — справка по скидкам
  Файл .xlsx — загрузка и обработка шаблона
"""

import logging
import os
import tempfile

from telegram import Update, ChatMemberUpdated
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ChatMemberHandler,
    ContextTypes,
    filters,
)

import config
import excel_parser

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

MAX_MESSAGE_LENGTH = 4096


# ── Helpers ────────────────────────────────────────────────────────────────

def split_message(text: str, max_len: int = MAX_MESSAGE_LENGTH) -> list[str]:
    """Разбивает длинное сообщение на части по границам строк."""
    if len(text) <= max_len:
        return [text]

    parts = []
    current = ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > max_len:
            parts.append(current)
            current = line
        else:
            current = current + "\n" + line if current else line
    if current:
        parts.append(current)
    return parts


# ── Command handlers ──────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    name = user.full_name or user.username or str(user.id)

    await update.message.reply_text(
        f"Привет, {name}! 👋\n\n"
        "Я помогаю отслеживать сроки годности товаров по акции Kari.\n\n"
        "📎 *Отправьте мне заполненный Excel-шаблон* магазина, "
        "и я сформирую отчёт по скидкам.\n\n"
        "📊 Логика скидок:\n"
        "  🔴 *70%* — до 90 дней до окончания\n"
        "  🟡 *50%* — от 91 до 180 дней\n"
        "  🟢 *Без скидки* — более 180 дней\n"
        "  ⛔ *ПРОСРОЧЕН* — снять с продажи\n\n"
        "Команды: /help — справка",
        parse_mode="Markdown",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "📖 *Как пользоваться ботом:*\n\n"
        "1️⃣ Откройте шаблон вашего магазина (.xlsx)\n"
        "2️⃣ Заполните *Дату производства* и *Срок годности (месяцев)* "
        "для каждого товара\n"
        "3️⃣ Отправьте файл в этот чат\n"
        "4️⃣ Бот автоматически рассчитает скидки и покажет отчёт\n\n"
        "📊 *Пороги скидок:*\n"
        "  🔴 Скидка 70% — осталось ≤ 90 дней\n"
        "  🟡 Скидка 50% — осталось 91–180 дней\n"
        "  🟢 Без скидки — осталось > 180 дней\n"
        "  ⛔ ПРОСРОЧЕН — срок истёк, убрать с полки\n\n"
        "Если отчёт отправляется в общий чат, бот продублирует "
        "его туда автоматически.",
        parse_mode="Markdown",
    )


# ── Приветствие при добавлении в группу ───────────────────────────────────

WELCOME_GROUP_MESSAGE = (
    "👋 Здравствуйте! Я бот системы контроля сроков годности.\n\n"
    "📱 *Сайт:* kari-realizaciya.ru\n\n"
    "🔑 *Вход для Директора подразделения:*\n"
    "• Логин — ваш номер на 7\n"
    "• Пароль — 1234\n"
    "• Смените пароль при первом входе\n\n"
    "🔑 *Вход для магазинов:*\n"
    "• Логин — номер магазина (например 10065)\n"
    "• Пароль — номер магазина\n\n"
    "🔖 *Добавьте иконку на телефон:*\n"
    "• iPhone: Safari → «Поделиться» → «На экран Домой»\n"
    "• Android: Chrome → ⋮ → «Добавить на главный экран»\n\n"
    "✅ *Что нужно сделать:*\n"
    "1. Зайти в приложение по своему номеру\n"
    "2. Открыть каждый товар → указать дату производства и срок годности\n"
    "3. Система сама рассчитает статус: просрочен / 70% / 50% / в норме\n"
    "4. Загрузить фото акционной зоны 📸\n\n"
    "🤖 *Я буду:*\n"
    "• Напоминать, когда товар просрочен и требует списания\n"
    "• Сообщать, когда товар подошёл к скидке 70% или 50%\n"
    "• Присылать сводку по магазину\n\n"
    "💬 Мы открыты к диалогу — пишите пожелания по доработке!"
)


async def on_bot_added_to_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отправляет приветствие когда бота добавляют в группу."""
    result: ChatMemberUpdated = update.my_chat_member
    if result is None:
        return

    old_status = result.old_chat_member.status if result.old_chat_member else None
    new_status = result.new_chat_member.status if result.new_chat_member else None

    # Бота добавили в группу (was not member → became member/admin)
    if old_status in (None, "left", "kicked") and new_status in ("member", "administrator"):
        chat = result.chat
        logger.info(f"Бот добавлен в группу: {chat.title} (ID: {chat.id})")
        try:
            await context.bot.send_message(
                chat_id=chat.id,
                text=WELCOME_GROUP_MESSAGE,
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.error(f"Ошибка отправки приветствия в группу {chat.id}: {e}")


# ── File handler ──────────────────────────────────────────────────────────

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка загруженного Excel-файла."""
    document = update.message.document

    if not document:
        return

    file_name = document.file_name or "file"
    if not file_name.lower().endswith((".xlsx", ".xls")):
        await update.message.reply_text(
            "⚠️ Пожалуйста, отправьте файл в формате *.xlsx*",
            parse_mode="Markdown",
        )
        return

    user = update.effective_user
    name = user.full_name or user.username or str(user.id)
    logger.info(f"Файл от {name} (ID: {user.id}): {file_name}")

    # Скачиваем файл
    await update.message.reply_text("⏳ Обрабатываю файл...")

    try:
        tg_file = await document.get_file()
        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
            tmp_path = tmp.name
            await tg_file.download_to_drive(tmp_path)

        # Парсим
        data = excel_parser.parse_store_template(tmp_path)
        # Подставляем оригинальное имя файла
        if "file_name" in data:
            data["file_name"] = file_name

        report = excel_parser.format_report(data)

    except Exception as e:
        logger.error(f"Ошибка обработки файла {file_name}: {e}", exc_info=True)
        await update.message.reply_text(
            f"❌ Ошибка при обработке файла:\n`{e}`",
            parse_mode="Markdown",
        )
        return
    finally:
        # Удаляем временный файл
        if 'tmp_path' in locals() and os.path.exists(tmp_path):
            os.unlink(tmp_path)

    # Отправляем отчёт в текущий чат
    for part in split_message(report):
        await update.message.reply_text(part, parse_mode="Markdown")

    # Дублируем в общий чат (если настроен и это не он)
    if config.CHAT_ID and update.effective_chat.id != config.CHAT_ID:
        try:
            header = f"📨 *Отчёт от {name}*\n\n"
            full_report = header + report
            for part in split_message(full_report):
                await context.bot.send_message(
                    chat_id=config.CHAT_ID,
                    text=part,
                    parse_mode="Markdown",
                )
            await update.message.reply_text("✅ Отчёт отправлен в общий чат.")
        except Exception as e:
            logger.error(f"Ошибка отправки в общий чат: {e}")
            await update.message.reply_text(
                "⚠️ Не удалось отправить отчёт в общий чат. "
                "Проверьте TELEGRAM_CHAT_ID и права бота."
            )


# ── Main ──────────────────────────────────────────────────────────────────

def main() -> None:
    if not config.BOT_TOKEN:
        raise ValueError(
            "TELEGRAM_BOT_TOKEN не задан!\n"
            "Создайте файл .env по образцу .env.example"
        )

    app = Application.builder().token(config.BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(ChatMemberHandler(on_bot_added_to_group, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    logger.info("Бот запущен. Ожидание файлов...")
    app.run_polling()


if __name__ == "__main__":
    main()
