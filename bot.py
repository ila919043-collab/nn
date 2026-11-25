#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Telegram-бот приёма заявок на вступление в семью.

Схема работы:
  * Основной бот (bot_token) принимает анкету от заявителя.
  * Готовая заявка уходит администратору — во второй бот (admin_bot_token),
    если он задан, иначе в этот же. В сообщении кнопки «Одобрить» / «Отклонить».
  * При нажатии «Одобрить» бот показывает время приёма — только варианты,
    оканчивающиеся на :00 (либо свободный ввод, минуты тоже округляются до :00).
  * Финальный бланк с решением уходит заявителю одним сообщением.
  * Всё сохраняется в applications.json.
"""

import asyncio
import contextlib
import html
import json
import logging
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
APPLICATIONS_PATH = BASE_DIR / "applications.json"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

ASKING = 0
MAX_ANSWER_LEN = 400

ADMIN_IDS: list[int] = []
ADMIN_SENDER: Bot | None = None  # кому отправлять заявки (второй бот или тот же)
MAIN_SENDER: Bot | None = None   # через кого писать заявителям

QUESTIONS = [
    ("age", "🎂 <b>Сколько вам лет?</b>"),
    ("game_level", "⭐ <b>Какой у вас уровень в игре?</b>"),
    (
        "other_families",
        "🏠 <b>Был ли опыт в других семьях?</b>\n"
        "Напишите, в каких и как долго, либо «нет».",
    ),
    ("static_id", "🆔 <b>Ваш статический ID?</b>"),
    ("ooc_name", "👤 <b>Ваше OOC имя?</b>"),
    (
        "daily_time",
        "⏰ <b>Сколько времени играете в день?</b>\nНапример: 3 часа.",
    ),
    ("weekly_times", "🗓 <b>Сколько раз в неделю можете играть?</b>"),
]

FIELD_LABELS = {
    "age": "Возраст",
    "game_level": "Уровень в игре",
    "other_families": "Опыт в других семьях",
    "static_id": "Статический ID",
    "ooc_name": "OOC имя",
    "daily_time": "Время игры в день",
    "weekly_times": "Игр в неделю",
}


# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------
def load_config() -> tuple[str, str, list[int]]:
    """Читает токены и ID админов из переменных окружения или config.json."""
    token = os.getenv("BOT_TOKEN", "").strip()
    admin_token = os.getenv("ADMIN_BOT_TOKEN", "").strip()
    admins_raw = os.getenv("ADMIN_IDS", "").strip()

    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not token:
            token = str(data.get("bot_token", "")).strip()
        if not admin_token:
            admin_token = str(data.get("admin_bot_token", "")).strip()
        if not admins_raw:
            admins_raw = ",".join(str(a) for a in data.get("admin_ids", []) or [])

    admin_ids: list[int] = []
    for chunk in admins_raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if chunk.lstrip("-").isdigit():
            admin_ids.append(int(chunk))

    if ":" not in token:
        raise SystemExit(
            "Не задан bot_token в config.json.\n"
            "Получите токен у @BotFather (команда /newbot) и впишите его в config.json."
        )
    if not admin_ids:
        raise SystemExit(
            "Не задан admin_ids в config.json — кому отправлять заявки.\n"
            "Узнайте свой Telegram ID у @userinfobot или командой /myid."
        )
    return token, admin_token, admin_ids


# ---------------------------------------------------------------------------
# Хранение заявок
# ---------------------------------------------------------------------------
def load_applications() -> list[dict]:
    if not APPLICATIONS_PATH.exists():
        return []
    try:
        with open(APPLICATIONS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def write_applications(data: list[dict]) -> None:
    with open(APPLICATIONS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def next_application_id() -> int:
    return max((a.get("id", 0) for a in load_applications()), default=0) + 1


def save_application(user, answers: dict[str, str]) -> dict:
    record = {
        "id": next_application_id(),
        "date": datetime.now().isoformat(timespec="seconds"),
        "telegram_id": user.id,
        "telegram_username": user.username,
        "telegram_name": user.full_name,
        "answers": answers,
        "status": None,       # заполняется админом: одобрено / отклонено
        "accept_date": None,  # когда могут принять (всегда :00 минут)
    }
    data = load_applications()
    data.append(record)
    write_applications(data)
    return record


def find_application(app_id: int) -> dict | None:
    for app in load_applications():
        if app.get("id") == app_id:
            return app
    return None


def update_application(app_id: int, **fields) -> dict | None:
    apps = load_applications()
    found = None
    for app in apps:
        if app.get("id") == app_id:
            app.update(fields)
            found = app
            break
    if found is not None:
        write_applications(apps)
    return found


def set_application_result(target: str, status: str, accept_date: str) -> dict | None:
    """Находит заявку по Telegram ID или по номеру в списке и записывает решение."""
    apps = load_applications()
    found = None
    for app in apps:
        if str(app.get("telegram_id")) == target:
            found = app
            break
    if found is None and target.isdigit():
        idx = int(target) - 1
        if 0 <= idx < len(apps):
            found = apps[idx]
    if found is None:
        return None
    found["status"] = status
    found["accept_date"] = accept_date
    write_applications(apps)
    return found


# ---------------------------------------------------------------------------
# Форматирование бланков
# ---------------------------------------------------------------------------
def format_application(record: dict) -> str:
    answers = record.get("answers", {})
    lines = [
        f"📋 <b>НОВАЯ ЗАЯВКА В СЕМЬЮ №{record.get('id')}</b>",
        "━━━━━━━━━━━━━━━━━━",
    ]
    for key, _ in QUESTIONS:
        value = html.escape(str(answers.get(key, "—")))
        lines.append(f"• <b>{FIELD_LABELS[key]}:</b> {value}")
    lines.append("━━━━━━━━━━━━━━━━━━")
    lines.append(f"👤 Telegram: {html.escape(str(record.get('telegram_name') or '—'))}")
    if record.get("telegram_username"):
        lines.append(f"🔗 @{html.escape(str(record['telegram_username']))}")
    lines.append(f"🆔 Telegram ID: <code>{record.get('telegram_id')}</code>")
    lines.append(f"🕐 {html.escape(str(record.get('date', '')[:16].replace('T', ' ')))}")
    return "\n".join(lines)


def format_result_block(record: dict) -> str:
    answers = record.get("answers", {})
    status = record.get("status") or "на рассмотрении"
    accept_date = record.get("accept_date")

    lines = [
        f"📋 <b>РЕЗУЛЬТАТ ЗАЯВКИ №{record.get('id')}</b>",
        "━━━━━━━━━━━━━━━━━━",
    ]
    for key, _ in QUESTIONS:
        value = html.escape(str(answers.get(key, "—")))
        lines.append(f"• <b>{FIELD_LABELS[key]}:</b> {value}")
    lines.append("━━━━━━━━━━━━━━━━━━")

    lowered = status.lower()
    if "одобр" in lowered:
        icon = "✅"
    elif "отклон" in lowered or "отказ" in lowered:
        icon = "❌"
    else:
        icon = "⏳"
    lines.append(f"{icon} <b>Решение:</b> {html.escape(str(status))}")
    if accept_date:
        lines.append(f"📅 <b>Могут принять:</b> {html.escape(str(accept_date))}")
    lines.append(f"🔗 <b>Ссылка на ДС:</b> <a href=\"https://discord.gg/nN8M2WKJM\">https://discord.gg/nN8M2WKJM</a>")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Доставка заявки админу + кнопки
# ---------------------------------------------------------------------------
def application_keyboard(app_id: int) -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton("✅ Одобрить", callback_data=f"appr:{app_id}"),
            InlineKeyboardButton("❌ Отклонить", callback_data=f"decl:{app_id}"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


async def deliver_to_admins(record: dict) -> int:
    """Отправляет заявку админам с кнопками. Возвращает число доставленных."""
    text = (
        format_application(record)
        + "\n\nНажмите «Одобрить» — появится время приёма (варианты заканчиваются на :00)."
    )
    markup = application_keyboard(record["id"])
    delivered = 0
    for admin_id in ADMIN_IDS:
        try:
            await ADMIN_SENDER.send_message(
                chat_id=admin_id, text=text, parse_mode=ParseMode.HTML, reply_markup=markup
            )
            delivered += 1
        except Exception:
            logger.exception("Не удалось отправить заявку админу %s", admin_id)
    return delivered


async def notify_applicant(record: dict) -> bool:
    """Отправляет финальный бланк заявителю через основной бот."""
    if MAIN_SENDER is None:
        return False
    try:
        await MAIN_SENDER.send_message(
            chat_id=record["telegram_id"],
            text=format_result_block(record),
            parse_mode=ParseMode.HTML,
        )
        return True
    except Exception:
        logger.exception("Не удалось отправить результат заявителю %s", record["telegram_id"])
        return False


DISCORD_LINK = "https://discord.gg/nN8M2WKJM"


# ---------------------------------------------------------------------------
# Анкета (основной бот)
# ---------------------------------------------------------------------------
async def send_question(update: Update, step: int) -> None:
    _, question = QUESTIONS[step]
    text = f"<b>Заявка в семью</b>  ({step + 1}/{len(QUESTIONS)})\n\n{question}"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["answers"] = {}
    context.user_data["step"] = 0
    await update.message.reply_text(
        "👋 Привет! Это бот подачи заявки на вступление в семью.\n\n"
        "Ответьте, пожалуйста, на несколько вопросов — это займёт около минуты.\n"
        "Прервать анкету можно командой /cancel.",
    )
    await send_question(update, 0)
    return ASKING


async def handle_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    step = context.user_data.get("step", 0)

    if not text:
        await update.message.reply_text("Пожалуйста, напишите ответ текстом.")
        return ASKING
    if len(text) > MAX_ANSWER_LEN:
        await update.message.reply_text(
            f"Слишком длинный ответ (максимум {MAX_ANSWER_LEN} символов). Сократите, пожалуйста."
        )
        return ASKING

    key, _ = QUESTIONS[step]
    context.user_data.setdefault("answers", {})[key] = text
    step += 1

    if step >= len(QUESTIONS):
        await finish_application(update, context)
        return ConversationHandler.END

    context.user_data["step"] = step
    await send_question(update, step)
    return ASKING


async def handle_non_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Пожалуйста, отвечайте текстом 🙂")
    return ASKING


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text(
        "Анкета отменена. Чтобы начать заново — отправьте /start."
    )
    return ConversationHandler.END


async def finish_application(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    record = save_application(user, context.user_data.get("answers", {}))

    delivered = await deliver_to_admins(record)

    if delivered:
        await update.message.reply_text(
            "✅ Спасибо! Ваша заявка отправлена администрации семьи.\n"
            "Ожидайте ответа — с вами свяжутся."
        )
    else:
        await update.message.reply_text(
            "⚠️ Не удалось доставить заявку администрации. "
            "Попробуйте позже или напишите админу напрямую."
        )
    logger.info("Заявка №%s от %s (%s): %s", record["id"], user.full_name, user.id, record["answers"])
    context.user_data.clear()


# ---------------------------------------------------------------------------
# Время приёма (всегда заканчивается на :00)
# ---------------------------------------------------------------------------
def build_time_keyboard(app_id: int) -> InlineKeyboardMarkup:
    """Клавиатура со слотами на ближайшие 12 часов — все оканчиваются на :00."""
    now = datetime.now()
    t = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    rows = []
    row = []
    for i in range(12):
        if t.date() == now.date():
            label = f"сегодня {t:%H:%M}"
        elif t.date() == now.date() + timedelta(days=1):
            label = f"завтра {t:%H:%M}"
        else:
            label = t.strftime("%d.%m %H:%M")
        data = f"time:{app_id}:{t:%d.%m.%Y %H-%M}"
        row.append(InlineKeyboardButton(label, callback_data=data))
        if len(row) == 3:
            rows.append(row)
            row = []
        t += timedelta(hours=1)
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("✏️ Другое время", callback_data=f"custom:{app_id}")])
    return InlineKeyboardMarkup(rows)


def parse_accept_time(text: str) -> datetime | None:
    """Разбирает время ввода; минуты округляются вверх до :00."""
    if not text:
        return None
    text = text.lower().strip()
    now = datetime.now()
    day = now.date()

    if "завтра" in text:
        day = day + timedelta(days=1)
    elif "сегодня" not in text:
        m = re.search(r"(\d{1,2})[.\-](\d{1,2})(?:[.\-]\s*(\d{4}))?", text)
        if m:
            try:
                year = int(m.group(3)) if m.group(3) else now.year
                day = datetime(year, int(m.group(2)), int(m.group(1))).date()
            except ValueError:
                return None

    tm = re.search(r"(\d{1,2}):(\d{2})", text)
    if tm:
        hour = int(tm.group(1))
        minute = int(tm.group(2))
    else:
        tm = re.search(r"\b(\d{1,2})\b", text)
        if not tm:
            return None
        hour = int(tm.group(1))
        minute = 0
    if minute:  # округляем до :00
        hour += 1
        minute = 0
    if hour > 23:
        hour = 0
        day = day + timedelta(days=1)
    dt = datetime(day.year, day.month, day.day, hour, 0)
    if dt <= now:
        dt += timedelta(days=1)
    return dt


async def on_approve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    app_id = int(query.data.split(":")[1])
    if find_application(app_id) is None:
        await query.answer("Заявка не найдена", show_alert=True)
        return
    await query.answer()
    text = query.message.text + "\n\n⏰ <b>Выберите время приёма</b> (все варианты на :00):"
    await query.edit_message_text(
        text, parse_mode=ParseMode.HTML, reply_markup=build_time_keyboard(app_id)
    )


async def on_decline(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    app_id = int(query.data.split(":")[1])
    record = update_application(app_id, status="отклонено", accept_date=None)
    if record is None:
        await query.answer("Заявка не найдена", show_alert=True)
        return
    await query.answer("Заявка отклонена")
    await query.edit_message_text(
        format_result_block(record) + "\n\n❌ Заявка отклонена.", parse_mode=ParseMode.HTML
    )
    await notify_applicant(record)


async def on_time_picked(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    _, app_id, dt_raw = query.data.split(":", 2)
    try:
        dt = datetime.strptime(dt_raw, "%d.%m.%Y %H-%M")
    except ValueError:
        await query.answer("Неверное время", show_alert=True)
        return
    record = update_application(
        int(app_id), status="одобрено", accept_date=dt.strftime("%d.%m.%Y %H:%M")
    )
    if record is None:
        await query.answer("Заявка не найдена", show_alert=True)
        return
    await query.answer("Заявка одобрена")
    await query.edit_message_text(
        format_result_block(record) + "\n\n✅ Бланк отправлен заявителю.",
        parse_mode=ParseMode.HTML,
    )
    await notify_applicant(record)


async def on_custom_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    app_id = int(query.data.split(":")[1])
    await query.answer()
    context.user_data["awaiting_time"] = app_id
    await query.edit_message_text(
        query.message.text
        + "\n\n✏️ <b>Напишите время приёма</b> — например: «сегодня 20:00», «завтра 15:00» "
        "или «25.12 20:00». Минуты округлятся до :00.",
        parse_mode=ParseMode.HTML,
    )


async def handle_custom_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app_id = context.user_data.get("awaiting_time")
    if app_id is None:
        await update.message.reply_text("Чтобы подать заявку, отправьте /start")
        return
    context.user_data["awaiting_time"] = None
    dt = parse_accept_time(update.message.text or "")
    if dt is None:
        context.user_data["awaiting_time"] = app_id
        await update.message.reply_text(
            "Не понял время. Напишите, например: «сегодня 20:00», «завтра 15:00» или «25.12 20:00».\n"
            "Минуты всегда округляются до :00."
        )
        return
    record = update_application(
        app_id, status="одобрено", accept_date=dt.strftime("%d.%m.%Y %H:%M")
    )
    if record is None:
        await update.message.reply_text("Заявка не найдена.")
        return
    await update.message.reply_text(
        format_result_block(record) + "\n\n✅ Бланк отправлен заявителю.",
        parse_mode=ParseMode.HTML,
    )
    await notify_applicant(record)


# ---------------------------------------------------------------------------
# Прочие команды
# ---------------------------------------------------------------------------
async def my_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    await update.message.reply_text(
        f"Ваш Telegram ID: <code>{user.id}</code>\n"
        f"ID этого чата: <code>{update.effective_chat.id}</code>\n\n"
        "Впишите нужный ID в config.json → \"admin_ids\".",
        parse_mode=ParseMode.HTML,
    )


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Проверить статус своей заявки."""
    app = None
    for record in reversed(load_applications()):
        if str(record.get("telegram_id")) == str(update.effective_user.id):
            app = record
            break
    if app is None:
        await update.message.reply_text("У вас пока нет заявки. Отправьте /start, чтобы подать.")
        return
    await update.message.reply_text(format_result_block(app), parse_mode=ParseMode.HTML)


def is_admin(user, context: ContextTypes.DEFAULT_TYPE) -> bool:
    return user.id in context.application.bot_data.get("admin_ids", [])


async def admin_results(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user, context):
        await update.message.reply_text("Эта команда доступна только администратору.")
        return
    apps = load_applications()
    if not apps:
        await update.message.reply_text("Заявок пока нет.")
        return
    lines = ["📋 <b>Последние заявки</b>:", ""]
    for app in apps[-15:]:
        name = app.get("telegram_name") or "?"
        status = app.get("status") or "⏳ на рассмотрении"
        lines.append(f"{app.get('id')}. {html.escape(str(name))} — {html.escape(str(status))}")
    lines.append("")
    lines.append(
        "Решение: /result <номер или Telegram ID> <одобрено|отклонено> <когда принимают>\n"
        "Пример: /result 1 одобрено 25.12 20:00"
    )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def admin_result(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user, context):
        await update.message.reply_text("Эта команда доступна только администратору.")
        return
    args = context.args
    if len(args) < 3:
        await update.message.reply_text(
            "Использование: /result <номер или Telegram ID> <одобрено|отклонено> <когда принимают>\n"
            "Пример: /result 1 одобрено 25.12 20:00\n"
            "Список заявок: /results"
        )
        return
    target, status = args[0], args[1]
    accept_date = " ".join(args[2:])
    app = set_application_result(target, status, accept_date)
    if app is None:
        await update.message.reply_text("Заявка не найдена. Список: /results")
        return
    ok = await notify_applicant(app)
    await update.message.reply_text(
        f"✅ Решение сохранено для заявки №{app.get('id')}."
        + (" Бланк отправлен заявителю." if ok else " ⚠️ Не удалось отправить бланк заявителю.")
    )


async def hint_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Чтобы подать заявку в семью, отправьте /start")


async def admin_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🤖 Админ-бот. Заявки из основного бота появляются здесь с кнопками "
        "«Одобрить» / «Отклонить». Список заявок: /results"
    )


async def admin_list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Список текущих админов (только админы)."""
    if not is_admin(update.effective_user, context):
        await update.message.reply_text("Эта команда доступна только администратору.")
        return
    lines = ["👮 <b>Админы системы:</b>"]
    for admin_id in ADMIN_IDS:
        marker = " ← вы" if admin_id == update.effective_user.id else ""
        lines.append(f"• <code>{admin_id}</code>{marker}")
    lines.append("")
    lines.append("Добавить нового админа: впишите его Telegram ID в config.json в список \"admin_ids\" и перезапустите бота.")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ---------------------------------------------------------------------------
# Регистрация хендлеров
# ---------------------------------------------------------------------------
def register_applicant_handlers(app: Application) -> None:
    conversation = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ASKING: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_answer),
                MessageHandler(~filters.COMMAND, handle_non_text),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )
    app.add_handler(conversation)
    app.add_handler(CommandHandler("myid", my_id))
    app.add_handler(CommandHandler("status", status_cmd))


def register_admin_handlers(app: Application, dual_mode: bool) -> None:
    if dual_mode:
        app.add_handler(CommandHandler("start", admin_start))
    app.add_handler(CommandHandler("results", admin_results))
    app.add_handler(CommandHandler("result", admin_result))
    app.add_handler(CommandHandler("admins", admin_list_cmd))
    app.add_handler(CallbackQueryHandler(on_approve, pattern=r"^appr:\d+$"))
    app.add_handler(CallbackQueryHandler(on_decline, pattern=r"^decl:\d+$"))
    app.add_handler(CallbackQueryHandler(on_time_picked, pattern=r"^time:\d+:.+$"))
    app.add_handler(CallbackQueryHandler(on_custom_time, pattern=r"^custom:\d+$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_custom_time))
    app.add_handler(MessageHandler(filters.ALL, hint_start))


# ---------------------------------------------------------------------------
# Запуск
# ---------------------------------------------------------------------------
async def run() -> None:
    global ADMIN_SENDER, MAIN_SENDER
    try:
        main_token, admin_token, admin_ids = load_config()
    except SystemExit as e:
        logger.error(str(e))
        logger.error("Бот не запущен: исправьте config.json и перезапустите.")
        return
    ADMIN_IDS.extend(admin_ids)

    # Отдельные Bot-экземпляры для отправки — main_app.bot ещё не инициализирован.
    MAIN_SENDER = Bot(token=main_token)
    ADMIN_SENDER = Bot(token=admin_token) if admin_token and admin_token != main_token else MAIN_SENDER

    main_app = Application.builder().token(main_token).build()
    main_app.bot_data["admin_ids"] = admin_ids

    dual_mode = bool(admin_token) and admin_token != main_token
    if dual_mode:
        admin_app = Application.builder().token(admin_token).build()
        admin_app.bot_data["admin_ids"] = admin_ids
        register_applicant_handlers(main_app)
        register_admin_handlers(admin_app, dual_mode=True)
        logger.info("Двухботовый режим: заявки приходят во второй бот с кнопками.")
    else:
        register_applicant_handlers(main_app)
        register_admin_handlers(main_app, dual_mode=False)
        logger.info("Один бот: заявки приходят с кнопками в этот же бот.")

    if os.getenv("RENDER") or os.getenv("RENDER_EXTERNAL_URL"):
        # Render free tier: webhook будит сервис при входящих запросах от Telegram.
        port = int(os.getenv("PORT", "8080"))
        webhook_url = f"{os.getenv('RENDER_EXTERNAL_URL')}/webhook"

        try:
            await main_app.initialize()
            await main_app.bot.set_webhook(webhook_url, allowed_updates=Update.ALL_TYPES)
            await main_app.updater.start_webhook(
                listen="0.0.0.0",
                port=port,
                url_path="/webhook",
                webhook_url=webhook_url,
                allowed_updates=Update.ALL_TYPES,
            )
            logger.info("Webhook запущен на %s:%s/webhook", "0.0.0.0", port)
        except Exception:
            logger.exception("Не удалось запустить webhook на Render.")
            return
    else:
        try:
            await main_app.initialize()
            await main_app.updater.start_polling(
                allowed_updates=Update.ALL_TYPES, drop_pending_updates=True
            )
            logger.info("Запущено (polling). Заявки уходят админам: %s", admin_ids)
        except Exception:
            logger.exception("Бот не смог подключиться к Telegram. "
                             "Если вы в РФ без VPN — бот не достигнет api.telegram.org. "
                             "Процесс останется висеть, но обновления не поступают.")

    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(run())
