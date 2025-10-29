import os
import html
import datetime
import traceback
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    ApplicationBuilder,
    PicklePersistence,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# --- Configuration ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
ADMINS = set(map(int, os.getenv("ADMINS", "").split(","))) if os.getenv("ADMINS") else set()
DATA_FILE = "bot_data.pkl"

# --- States for ConversationHandler ---
(SET_TITLE, SET_DATE, SET_DESCRIPTION, SET_PHOTO, WAIT_CONFIRM) = range(5)

# --- Utilities --------------------------------------------------------

def is_admin(user_id: int, bot_data) -> bool:
    return user_id in ADMINS or user_id == bot_data.get("creator_id")

def user_entry(user) -> dict:
    display = user.first_name or user.username or str(user.id)
    return {
        "id": user.id,
        "name": html.escape(display[:64]),
        "username": f"@{user.username}" if user.username else "",
    }

def users_list_repr(users: list) -> str:
    if not users:
        return "—"
    formatted = []
    for u in users:
        name = html.escape(u.get("name", ""))
        mention = f'<a href="tg://user?id={u["id"]}">{name}</a>'
        formatted.append(mention)
    return "\n".join(formatted)

# --- Core Bot Logic ---------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Я бот для организации событий.\n"
        "Доступные команды:\n"
        "/create_event — создать событие\n"
        "/admin_panel — панель управления (только для админов)\n"
        "/remove_user — удалить участника (только для админов)\n"
        "/cancel — отменить текущее действие"
    )

# --- Event Creation Flow ---

async def create_event_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Введите название события:")
    return SET_TITLE

async def set_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["event"] = {"title": update.message.text}
    await update.message.reply_text("Введите дату и время события:")
    return SET_DATE

async def set_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["event"]["date"] = update.message.text
    await update.message.reply_text("Введите описание события:")
    return SET_DESCRIPTION

async def set_description(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["event"]["description"] = update.message.text
    await update.message.reply_text("Отправьте фото события или напишите 'нет':")
    return SET_PHOTO

async def set_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    event = context.user_data["event"]
    if update.message.photo:
        file_id = update.message.photo[-1].file_id
        event["photo"] = file_id
    else:
        event["photo"] = None
    await update.message.reply_text("Подтвердить создание события? (да/нет)")
    return WAIT_CONFIRM

async def wait_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.lower()
    if text not in ("да", "нет"):
        await update.message.reply_text("Пожалуйста, ответьте 'да' или 'нет'.")
        return WAIT_CONFIRM
    if text == "нет":
        await update.message.reply_text("Создание события отменено.")
        return ConversationHandler.END

    event = context.user_data["event"]
    event_id = context.bot_data.get("event_counter", 0) + 1
    context.bot_data["event_counter"] = event_id
    event["id"] = event_id
    event["participants"] = []
    event["waitlist"] = []
    event["message_id"] = None

    # Publish event to channel
    keyboard = [[InlineKeyboardButton("✅ Записаться", callback_data=f"join_{event_id}")]]
    markup = InlineKeyboardMarkup(keyboard)

    caption = (
        f"<b>{html.escape(event['title'])}</b>\n\n"
        f"<i>{html.escape(event['date'])}</i>\n\n"
        f"{html.escape(event['description'])}"
    )
    if event["photo"]:
        msg = await context.bot.send_photo(
            chat_id=CHANNEL_ID,
            photo=event["photo"],
            caption=caption,
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
        )
    else:
        msg = await context.bot.send_message(
            chat_id=CHANNEL_ID,
            text=caption,
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
        )

    event["message_id"] = msg.message_id
    context.bot_data.setdefault("events", {})[event_id] = event

    await update.message.reply_text(f"Событие создано и опубликовано (ID {event_id}).")
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Действие отменено.")
    return ConversationHandler.END

# --- Event Join Logic ---

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    data = query.data
    if not data.startswith("join_"):
        return

    event_id = int(data.split("_")[1])
    event = context.bot_data.get("events", {}).get(event_id)
    if not event:
        await query.edit_message_caption(caption="❌ Событие не найдено.", parse_mode=ParseMode.HTML)
        return

    u = user_entry(user)
    if any(u["id"] == x["id"] for x in event["participants"] + event["waitlist"]):
        await query.message.reply_text("Вы уже записаны или в списке ожидания.")
        return

    if len(event["participants"]) < 10:
        event["participants"].append(u)
    else:
        event["waitlist"].append(u)

    await update_event_message(event, context)

async def update_event_message(event, context: ContextTypes.DEFAULT_TYPE):
    try:
        caption = (
            f"<b>{html.escape(event['title'])}</b>\n\n"
            f"<i>{html.escape(event['date'])}</i>\n\n"
            f"{html.escape(event['description'])}\n\n"
            f"<b>Участники ({len(event['participants'])}/10):</b>\n"
            f"{users_list_repr(event['participants'])}\n\n"
            f"<b>Список ожидания:</b>\n"
            f"{users_list_repr(event['waitlist'])}"
        )
        markup = InlineKeyboardMarkup(
            [[InlineKeyboardButton("✅ Записаться", callback_data=f"join_{event['id']}")]]
        )

        if event["photo"]:
            await context.bot.edit_message_caption(
                chat_id=CHANNEL_ID,
                message_id=event["message_id"],
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
            )
        else:
            await context.bot.edit_message_text(
                chat_id=CHANNEL_ID,
                message_id=event["message_id"],
                text=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
            )
    except (BadRequest, Forbidden, TelegramError):
        pass

# --- Admin Tools ---

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if not is_admin(user_id, context.bot_data):
        await update.message.reply_text("❌ У вас нет доступа.")
        return

    events = context.bot_data.get("events", {})
    if not events:
        await update.message.reply_text("Нет активных событий.")
        return

    text = "📋 Активные события:\n\n"
    for eid, e in events.items():
        text += f"ID {eid}: {e['title']} ({len(e['participants'])} участников)\n"
    await update.message.reply_text(text)

# --- NEW: Remove user from event (admin only) ---

async def remove_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if not is_admin(user_id, context.bot_data):
        await update.message.reply_text("❌ У вас нет доступа.")
        return

    args = context.args
    if len(args) != 2:
        await update.message.reply_text("Использование: /remove_user <event_id> <user_id>")
        return

    try:
        event_id, target_id = map(int, args)
    except ValueError:
        await update.message.reply_text("ID должны быть числами.")
        return

    events = context.bot_data.get("events", {})
    event = events.get(event_id)
    if not event:
        await update.message.reply_text(f"Событие с ID {event_id} не найдено.")
        return

    removed_from = None
    for lst_name in ("participants", "waitlist"):
        lst = event[lst_name]
        for u in lst:
            if u["id"] == target_id:
                lst.remove(u)
                removed_from = lst_name
                break
        if removed_from:
            break

    if not removed_from:
        await update.message.reply_text("Пользователь не найден в этом событии.")
        return

    # Promote first from waitlist if needed
    if removed_from == "participants" and event["waitlist"]:
        promoted = event["waitlist"].pop(0)
        event["participants"].append(promoted)
        try:
            await context.bot.send_message(
                chat_id=promoted["id"],
                text=f"🎉 Вы были добавлены в список участников события <b>{event['title']}</b>!",
                parse_mode=ParseMode.HTML,
            )
        except TelegramError:
            pass

    await update_event_message(event, context)
    await update.message.reply_text(
        f"✅ Пользователь {target_id} удалён из события <b>{event['title']}</b>.",
        parse_mode=ParseMode.HTML,
    )

# --- Error Logging ---

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    print(f"⚠️ Exception:\n{traceback.format_exc()}")

# --- Main -------------------------------------------------------------

def main():
    persistence = PicklePersistence(filepath=DATA_FILE)
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .persistence(persistence)
        .build()
    )

    conv = ConversationHandler(
        entry_points=[CommandHandler("create_event", create_event_start)],
        states={
            SET_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_title)],
            SET_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_date)],
            SET_DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_description)],
            SET_PHOTO: [MessageHandler((filters.PHOTO | filters.TEXT) & ~filters.COMMAND, set_photo)],
            WAIT_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, wait_confirm)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        name="event_creation",
        persistent=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(CommandHandler("admin_panel", admin_panel))
    app.add_handler(CommandHandler("remove_user", remove_user))
    app.add_error_handler(error_handler)

    print("✅ Bot started.")
    app.run_polling()

if __name__ == "__main__":
    main()
