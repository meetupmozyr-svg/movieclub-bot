#!/usr/bin/env python3
"""
Telegram bot for creating and managing events.
"""

import os
import io
import csv
from typing import Dict, List, Any
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, InputFile
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    PicklePersistence,
    ConversationHandler,
    filters,
)

DATA_FILE = "bot_persistence.pickle"
CHANNEL = os.environ.get("CHANNEL", "@testchannel")

C_TITLE, C_DATE, C_CAPACITY, C_LOCATION, C_DESCRIPTION, C_PHOTO, EDIT_SELECT_FIELD, EDIT_NEW_VALUE = range(8)

def user_entry(from_user) -> Dict[str, Any]:
    name = from_user.full_name or from_user.first_name or str(from_user.id)
    return {"id": from_user.id, "name": name, "username": f"@{from_user.username}" if from_user.username else None}

def users_list_repr(users: List[Dict[str, Any]]) -> str:
    if not users:
        return "(пусто)"
    lines = []
    for u in users:
        display = u["name"]
        if u.get("username"):
            display += f" ({u['username']})"
        lines.append(f"• <a href='tg://user?id={u['id']}'>{display}</a>")
    return "\n".join(lines)

def format_event_message(event: Dict[str, Any]) -> str:
    joined_block = users_list_repr(event.get("joined", []))
    wait_block = users_list_repr(event.get("waitlist", []))
    joined_count = len(event.get("joined", []))
    wait_count = len(event.get("waitlist", []))
    return (
        f"🎬 <b>{event.get('title', 'Без названия')}</b>\n"
        f"📅 {event.get('date', 'Дата не указана')}\n"
        f"📍 {event.get('location','(место не указано)')}\n\n"
        f"{event.get('description','(без описания)')}\n\n"
        f"👥 <b>{joined_count}/{event.get('capacity', 0)}</b> участников\n\n"
        f"<b>✅ Участники:</b>\n{joined_block}\n\n"
        f"🕒 <b>Лист ожидания:</b> {wait_count}\n{wait_block}"
    )

def make_event_keyboard(event_id: str, event: Dict[str, Any]) -> InlineKeyboardMarkup:
    spots_filled = len(event.get("joined", []))
    capacity = event.get("capacity", 0)
    wait_len = len(event.get("waitlist", []))
    if capacity <= 0 or spots_filled >= capacity:
        join_text = f"🕒 Встать в лист ожидания ({wait_len})" if capacity > 0 else "🔒 Запись закрыта"
        join_data = f"join|{event_id}" if capacity > 0 else "no_join"
    else:
        join_text = f"✅ Записаться ({spots_filled}/{capacity})"
        join_data = f"join|{event_id}"
    kb = [[InlineKeyboardButton(join_text, callback_data=join_data),
           InlineKeyboardButton("❌ Не смогу прийти", callback_data=f"leave|{event_id}")]]
    return InlineKeyboardMarkup(kb)

async def update_event_message(context: ContextTypes.DEFAULT_TYPE, event_id: str, event: Dict[str, Any]):
    channel_id = event.get("channel", CHANNEL)
    message_id = event.get("message_id")
    if not message_id:
        return
    try:
        if event.get("photo_id"):
            await context.bot.edit_message_caption(chat_id=channel_id, message_id=message_id,
                                                   caption=format_event_message(event),
                                                   reply_markup=make_event_keyboard(event_id, event),
                                                   parse_mode="HTML")
        else:
            await context.bot.edit_message_text(chat_id=channel_id, message_id=message_id,
                                                text=format_event_message(event),
                                                reply_markup=make_event_keyboard(event_id, event),
                                                parse_mode="HTML",
                                                disable_web_page_preview=True)
    except Exception:
        pass

# ---------------- Commands ----------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    await update.message.reply_text(
        "Привет! Я бот для мероприятий.\n\n"
        "Команды:\n"
        "/create_event Название | Дата | Вместимость | Место | Описание — создать событие\n"
        "Отправь фото с подписью 'Название | Дата | Вместимость | ...' чтобы создать событие с фото\n"
        "/my_events — показать твои записи\n"
        "/export_event <id> — экспорт участников\n"
        "/delete_event <id> — удалить событие"
    )

async def create_event_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not context.args:
        return
    if "events" not in context.bot_data:
        context.bot_data["events"] = {}
    raw = " ".join(context.args)
    parts = [p.strip() for p in raw.split("|")]
    if len(parts) < 3:
        await update.message.reply_text("Нужно минимум: Название | Дата | Вместимость")
        return
    try:
        title, date, capacity = parts[0], parts[1], int(parts[2])
        if capacity <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Вместимость должна быть положительным числом.")
        return
    location = parts[3] if len(parts) > 3 else ""
    description = parts[4] if len(parts) > 4 else ""
    event_id = str(context.bot_data.get("event_counter", 0) + 1)
    context.bot_data["event_counter"] = int(event_id)
    event = {
        "id": event_id,
        "title": title,
        "date": date,
        "capacity": capacity,
        "location": location,
        "description": description,
        "creator_id": update.effective_user.id,
        "message_id": None,
        "channel": CHANNEL,
        "joined": [],
        "waitlist": [],
        "photo_id": None,
    }
    try:
        sent = await context.bot.send_message(chat_id=CHANNEL, text=format_event_message(event),
                                              reply_markup=make_event_keyboard(event_id, event),
                                              parse_mode="HTML",
                                              disable_web_page_preview=True)
        event["message_id"] = sent.message_id
        context.bot_data["events"][event_id] = event
        await update.message.reply_text(f"Событие создано (ID {event_id}).")
    except Exception as e:
        await update.message.reply_text(f"Ошибка публикации: {e}")

async def join_leave_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()
    action, event_id = query.data.split("|")
    event = context.bot_data.get("events", {}).get(event_id)
    if not event:
        return
    user = user_entry(update.effective_user)
    joined = event.get("joined", [])
    waitlist = event.get("waitlist", [])
    capacity = event.get("capacity", 0)

    if action == "join":
        if user not in joined and user not in waitlist:
            if len(joined) < capacity:
                joined.append(user)
            else:
                waitlist.append(user)
    elif action == "leave":
        if user in joined:
            joined.remove(user)
            if waitlist:
                joined.append(waitlist.pop(0))
        elif user in waitlist:
            waitlist.remove(user)

    event["joined"], event["waitlist"] = joined, waitlist
    await update_event_message(context, event_id, event)

# ---------------- Main ----------------
def main():
    persistence = PicklePersistence(filepath=DATA_FILE)
    app = ApplicationBuilder().token(os.environ.get("BOT_TOKEN")).persistence(persistence).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("create_event", create_event_command))
    app.add_handler(CallbackQueryHandler(join_leave_callback))

    # Photo with caption creating event
    app.add_handler(MessageHandler(filters.PHOTO & filters.CaptionRegex(r".*\|.*\|.*"), create_event_command))

    print("Bot started")
    app.run_polling()

if __name__ == "__main__":
    main()
