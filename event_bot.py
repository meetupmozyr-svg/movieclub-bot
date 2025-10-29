# telegram_event_bot_full.py
"""
Полнофункциональный Telegram бот для создания мероприятий с фото, пошаговым вводом,
просмотром участников, экспортом и админ-командами.
"""
import os
import csv
import io
from typing import Dict, List, Any, Tuple
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
    Message,
    InputFile,
)
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    PicklePersistence,
    MessageHandler,
    filters,
    ConversationHandler,
)
from telegram.error import BadRequest, Forbidden

# persistence file
DATA_FILE = "bot_persistence.pickle"

# Conversation states for create/edit flows
(
    C_TITLE,
    C_DATE,
    C_CAPACITY,
    C_LOCATION,
    C_DESCRIPTION,
    C_PHOTO,
    EDIT_SELECT_FIELD,
    EDIT_NEW_VALUE,
) = range(20)

# Utility: get admin list from env (comma-separated IDs) or empty
def get_admin_ids() -> List[int]:
    raw = os.environ.get("ADMIN_IDS", "")
    if not raw:
        return []
    return [int(x.strip()) for x in raw.split(",") if x.strip().isdigit()]


ADMIN_IDS = get_admin_ids()


# --------------------------- Messages & formatting ---------------------------

def user_entry(from_user) -> Dict[str, Any]:
    return {
        "id": from_user.id,
        "name": from_user.full_name or from_user.first_name or str(from_user.id),
        "username": f"@{from_user.username}" if from_user.username else None,
    }


def users_list_repr(users: List[Dict[str, Any]]) -> str:
    if not users:
        return "(пусто)"
    lines = []
    for u in users:
        uid = u.get("id")
        name = u.get("name", str(uid))
        username = u.get("username")
        display = f"{name} {username}" if username else name
        lines.append(f"• <a href='tg://user?id={uid}'>{display}</a>")
    return "\n".join(lines)


def format_event_message(event: Dict[str, Any]) -> str:
    joined_block = users_list_repr(event["joined"])
    wait_block = users_list_repr(event["waitlist"])
    joined_count = len(event["joined"])
    wait_count = len(event["waitlist"])
    text = (
        f"🎬 <b>{event['title']}</b>\n"
        f"📅 {event['date']}\n"
        f"📍 {event.get('location','(место не указано)')}\n\n"
        f"{event.get('description','(без описания)')}\n\n"
        f"👥 <b>{joined_count}/{event['capacity']}</b> участников\n"
        f"{joined_block}\n\n"
        f"🕒 Лист ожидания: {wait_count}\n"
        f"{wait_block}"
    )
    return text


def make_event_keyboard(event_id: str, event: Dict[str, Any]) -> InlineKeyboardMarkup:
    spots_filled = len(event["joined"])
    capacity = event["capacity"]
    waitlist_len = len(event["waitlist"])
    if spots_filled >= capacity:
        join_text = f"🕒 Встать в лист ожидания ({waitlist_len})"
    else:
        join_text = f"✅ Записаться ({spots_filled}/{capacity})"
    kb = [
        [
            InlineKeyboardButton(join_text, callback_data=f"join|{event_id}"),
            InlineKeyboardButton("❌ Не смогу прийти", callback_data=f"leave|{event_id}"),
        ]
    ]
    return InlineKeyboardMarkup(kb)


# --------------------------- Core command handlers ---------------------------

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я бот для создания мероприятий.\n\n"
        "Команды:\n"
        "/create — создать событие (пошагово)\n"
        "/create_event Title | Date | Capacity | Location | Description — быстро (текст)\n"
        "Отправь фото с подписью в формате \"Title | Date | Capacity | ...\" чтобы создать событие с фото.\n"
        "/my_events — мои записи\n"
        "/export_event <id> — экспорт участников (только админы/создатель)\n"
        "/delete_event <id> — удалить событие (только админы/создатель)\n"
        "/edit_event <id> — редактировать событие (только админы/создатель)\n"
    )


# --------------------------- Legacy quick create (text) ---------------------------

async def create_event_command_quick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return
    if not context.args:
        await update.message.reply_text(
            "Использование:\n/create_event Название | Дата | Вместимость | Место (опционально) | Описание (опционально)"
        )
        return
    if "events" not in context.bot_data:
        context.bot_data["events"] = {}

    raw = " ".join(context.args)
    parts = [p.strip() for p in raw.split("|")]
    if len(parts) < 3:
        await update.message.reply_text("Нужно минимум: Название | Дата | Вместимость")
        return
    try:
        title, date = parts[0], parts[1]
        capacity = int(parts[2])
    except (ValueError, IndexError):
        await update.message.reply_text("Вместимость должна быть числом.")
        return
    location = parts[3] if len(parts) > 3 else ""
    description = parts[4] if len(parts) > 4 else ""
    event_id = str(max([int(k) for k in context.bot_data["events"].keys()] + [0]) + 1)
    event = {
        "id": event_id,
        "title": title,
        "date": date,
        "capacity": capacity,
        "location": location,
        "description": description,
        "creator_id": user.id,
        "message_id": None,
        "channel": os.environ.get("CHANNEL", "@kinovinomoz"),
        "joined": [],
        "waitlist": [],
        "photo_id": None,
    }
    text = format_event_message(event)
    kb = make_event_keyboard(event_id, event)
    try:
        sent = await context.bot.send_message(
            chat_id=event["channel"],
            text=text,
            reply_markup=kb,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception as e:
        await update.message.reply_text(f"Ошибка отправки: {e}")
        return
    event["message_id"] = sent.message_id
    context.bot_data["events"][event_id] = event
    context.bot_data.update({})
    await update.message.reply_text(f"Событие создано и опубликовано (ID {event_id}).")


# --------------------------- Create from photo caption (legacy) ---------------------------

async def create_event_from_photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg: Message = update.message
    user = update.effective_user
    if not user or not msg:
        return
    caption = msg.caption or ""
    parts = [p.strip() for p in caption.split("|")]
    if len(parts) < 3:
        await msg.reply_text(
            "Чтобы создать событие с фото — пришли фото с подписью:\n"
            "Название | Дата | Вместимость | Место (опционально) | Описание (опционально)"
        )
        return
    if "events" not in context.bot_data:
        context.bot_data["events"] = {}
    try:
        title, date = parts[0], parts[1]
        capacity = int(parts[2])
    except (ValueError, IndexError):
        await msg.reply_text("Вместимость должна быть числом.")
        return
    location = parts[3] if len(parts) > 3 else ""
    description = parts[4] if len(parts) > 4 else ""
    event_id = str(max([int(k) for k in context.bot_data["events"].keys()] + [0]) + 1)
    photo_file_id = msg.photo[-1].file_id if msg.photo else None
    event = {
        "id": event_id,
        "title": title,
        "date": date,
        "capacity": capacity,
        "location": location,
        "description": description,
        "creator_id": user.id,
        "message_id": None,
        "channel": os.environ.get("CHANNEL", "@kinovinomoz"),
        "joined": [],
        "waitlist": [],
        "photo_id": photo_file_id,
    }
    text = format_event_message(event)
    kb = make_event_keyboard(event_id, event)
    try:
        sent = await context.bot.send_photo(
            chat_id=event["channel"],
            photo=event["photo_id"],
            caption=text,
            reply_markup=kb,
            parse_mode="HTML",
        )
    except Exception as e:
        await msg.reply_text(f"Ошибка отправки фото: {e}")
        return
    event["message_id"] = sent.message_id
    context.bot_data["events"][event_id] = event
    context.bot_data.update({})
    await msg.reply_text(f"Событие с фото создано (ID {event_id}).")


# --------------------------- Conversation: Create (pошагово) ---------------------------

async def create_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Создаём новое событие — шаг 1/6.\nПришли название события:")
    # temporary store partials in user_data
    context.user_data["new_event"] = {}
    return C_TITLE


async def create_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_event"]["title"] = update.message.text.strip()
    await update.message.reply_text("Шаг 2/6. Укажи дату/время (например: 2025-11-02 20:00):")
    return C_DATE


async def create_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_event"]["date"] = update.message.text.strip()
    await update.message.reply_text("Шаг 3/6. Укажи вместимость (целое число):")
    return C_CAPACITY


async def create_capacity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    try:
        capacity = int(txt)
        if capacity <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Вместимость должна быть положительным целым числом. Попробуй ещё раз:")
        return C_CAPACITY
    context.user_data["new_event"]["capacity"] = capacity
    await update.message.reply_text("Шаг 4/6. Укажи место (или отправь '-' чтобы пропустить):")
    return C_LOCATION


async def create_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    context.user_data["new_event"]["location"] = "" if txt == "-" else txt
    await update.message.reply_text("Шаг 5/6. Напиши описание (или '-' чтобы пропустить):")
    return C_DESCRIPTION


async def create_description(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    context.user_data["new_event"]["description"] = "" if txt == "-" else txt
    await update.message.reply_text(
        "Шаг 6/6 (опционально). Пришли фото сейчас, либо отправь 'skip' чтобы завершить без фото."
    )
    return C_PHOTO


async def create_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # if user sent 'skip' text earlier, they would reach here? we check type
    if update.message.text and update.message.text.strip().lower() == "skip":
        photo_id = None
    else:
        if not update.message.photo:
            await update.message.reply_text("Ожидаю фото или 'skip'.")
            return C_PHOTO
        photo_id = update.message.photo[-1].file_id

    # finalize event creation
    if "events" not in context.bot_data:
        context.bot_data["events"] = {}
    event_id = str(max([int(k) for k in context.bot_data["events"].keys()] + [0]) + 1)
    ne = context.user_data["new_event"]
    event = {
        "id": event_id,
        "title": ne["title"],
        "date": ne["date"],
        "capacity": ne["capacity"],
        "location": ne.get("location", ""),
        "description": ne.get("description", ""),
        "creator_id": update.effective_user.id,
        "message_id": None,
        "channel": os.environ.get("CHANNEL", "@kinovinomoz"),
        "joined": [],
        "waitlist": [],
        "photo_id": photo_id,
    }
    text = format_event_message(event)
    kb = make_event_keyboard(event_id, event)
    try:
        if photo_id:
            sent = await context.bot.send_photo(
                chat_id=event["channel"],
                photo=photo_id,
                caption=text,
                reply_markup=kb,
                parse_mode="HTML",
            )
        else:
            sent = await context.bot.send_message(
                chat_id=event["channel"],
                text=text,
                reply_markup=kb,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
    except Exception as e:
        await update.message.reply_text(f"Ошибка при публикации: {e}")
        context.user_data.pop("new_event", None)
        return ConversationHandler.END

    event["message_id"] = sent.message_id
    context.bot_data["events"][event_id] = event
    context.bot_data.update({})

    await update.message.reply_text(f"Событие создано и опубликовано (ID {event_id}).")
    context.user_data.pop("new_event", None)
    return ConversationHandler.END


async def create_photo_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # user typed 'skip' instead of sending photo
    update.message.text = "skip"  # hack: mark it
    return await create_photo(update, context)


async def create_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("new_event", None)
    await update.message.reply_text("Создание события отменено.")
    return ConversationHandler.END


# --------------------------- Button handler (join/leave) ---------------------------

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = context.bot_data
    try:
        action, event_id = query.data.split("|")
    except (ValueError, AttributeError):
        await query.edit_message_text("Неверные данные callback.")
        return
    event = data.get("events", {}).get(event_id)
    if not event:
        await query.edit_message_text("Событие не найдено или устарело.")
        return
    user = query.from_user
    ue = user_entry(user)
    # remove existing entries
    event["joined"] = [u for u in event["joined"] if u["id"] != ue["id"]]
    event["waitlist"] = [u for u in event["waitlist"] if u["id"] != ue["id"]]
    response = ""
    if action == "join":
        if len(event["joined"]) < event["capacity"]:
            event["joined"].append(ue)
            response = "Вы успешно записаны ✅"
        else:
            event["waitlist"].append(ue)
            response = "Событие полное — вы добавлены в лист ожидания 🕒"
    elif action == "leave":
        response = "Вы помечены как не придёте ❌"
    else:
        response = "Неизвестное действие."
    # Promote from waitlist
    while len(event["joined"]) < event["capacity"] and event["waitlist"]:
        promoted = event["waitlist"].pop(0)
        if promoted["id"] not in [u["id"] for u in event["joined"]]:
            event["joined"].append(promoted)
            try:
                await context.bot.send_message(
                    chat_id=promoted["id"],
                    text=(
                        f"Хорошая новость — освободилось место!\n\n"
                        f"Вы автоматически перенесены из листа ожидания в подтверждённые: {event['title']} — {event['date']}"
                    ),
                )
            except (BadRequest, Forbidden) as e:
                print(f"Не удалось уведомить пользователя {promoted['id']}: {e}")
    # save
    data["events"][event_id] = event
    context.bot_data.update({})
    # update channel message
    try:
        if event.get("photo_id"):
            await context.bot.edit_message_caption(
                chat_id=event["channel"],
                message_id=event["message_id"],
                caption=format_event_message(event),
                reply_markup=make_event_keyboard(event_id, event),
                parse_mode="HTML",
            )
        else:
            await context.bot.edit_message_text(
                chat_id=event["channel"],
                message_id=event["message_id"],
                text=format_event_message(event),
                reply_markup=make_event_keyboard(event_id, event),
                parse_mode="HTML",
            )
    except (BadRequest, Forbidden) as e:
        print(f"Не удалось отредактировать сообщение события {event_id}: {e}")
    # respond to clicking user
    try:
        await query.from_user.send_message(response)
    except (BadRequest, Forbidden):
        await query.answer(text=response, show_alert=True)


# --------------------------- my_events ---------------------------

async def my_events_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return
    uid = user.id
    events = context.bot_data.get("events", {})
    out = []
    for e in events.values():
        if any(u["id"] == uid for u in e["joined"]):
            out.append(f"✅ Записан: {e['title']} — {e['date']} (ID {e['id']})")
        elif any(u["id"] == uid for u in e["waitlist"]):
            out.append(f"🕒 Лист ожидания: {e['title']} — {e['date']} (ID {e['id']})")
    if not out:
        await update.message.reply_text("У вас нет записей.")
    else:
        await update.message.reply_text("<b>Ваши записи:</b>\n\n" + "\n".join(out), parse_mode="HTML")


# --------------------------- Admin actions: export / delete / edit ---------------------------

def is_admin(user_id: int, event: Dict[str, Any] = None) -> bool:
    if user_id in ADMIN_IDS:
        return True
    if event and event.get("creator_id") == user_id:
        return True
    return False


async def export_event_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not context.args:
        await update.message.reply_text("Использование: /export_event <event_id>")
        return
    event_id = context.args[0].strip()
    events = context.bot_data.get("events", {})
    event = events.get(event_id)
    if not event:
        await update.message.reply_text("Событие не найдено.")
        return
    if not is_admin(user.id, event):
        await update.message.reply_text("Только админ или создатель может экспортировать участников.")
        return
    # create CSV in memory
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["status", "id", "name", "username"])
    for u in event["joined"]:
        writer.writerow(["joined", u.get("id"), u.get("name"), u.get("username") or ""])
    for u in event["waitlist"]:
        writer.writerow(["waitlist", u.get("id"), u.get("name"), u.get("username") or ""])
    buf.seek(0)
    bio = io.BytesIO(buf.getvalue().encode("utf-8"))
    bio.name = f"event_{event_id}_participants.csv"
    try:
        await context.bot.send_document(chat_id=user.id, document=InputFile(bio))
    except (BadRequest, Forbidden) as e:
        await update.message.reply_text(f"Не удалось отправить файл: {e}")


async def delete_event_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not context.args:
        await update.message.reply_text("Использование: /delete_event <event_id>")
        return
    event_id = context.args[0].strip()
    events = context.bot_data.get("events", {})
    event = events.get(event_id)
    if not event:
        await update.message.reply_text("Событие не найдено.")
        return
    if not is_admin(user.id, event):
        await update.message.reply_text("Только админ или создатель может удалить событие.")
        return
    # try delete message in channel
    try:
        await context.bot.delete_message(chat_id=event["channel"], message_id=event["message_id"])
    except Exception:
        # ignore failures (bot may have lost perms)
        pass
    # remove from storage
    events.pop(event_id, None)
    context.bot_data["events"] = events
    context.bot_data.update({})
    await update.message.reply_text(f"Событие {event_id} удалено.")


# Edit flow: start -> choose field -> provide new value (photo handled)
async def edit_event_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not context.args:
        await update.message.reply_text("Использование: /edit_event <event_id>")
        return
    event_id = context.args[0].strip()
    events = context.bot_data.get("events", {})
    event = events.get(event_id)
    if not event:
        await update.message.reply_text("Событие не найдено.")
        return
    if not is_admin(user.id, event):
        await update.message.reply_text("Только админ или создатель может редактировать событие.")
        return
    # store editing context
    context.user_data["edit_event_id"] = event_id
    await update.message.reply_text(
        "Что редактировать? Выбери номер:\n"
        "1 — Название\n2 — Дата\n3 — Вместимость\n4 — Место\n5 — Описание\n6 — Фото (пришли новое фото)\n\n"
        "Отправь номер поля."
    )
    return EDIT_SELECT_FIELD


async def edit_select_field(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    if txt not in {"1", "2", "3", "4", "5", "6"}:
        await update.message.reply_text("Неверный выбор. Отправь номер (1-6).")
        return EDIT_SELECT_FIELD
    context.user_data["edit_field"] = int(txt)
    if txt == "6":
        await update.message.reply_text("Пришли новое фото (или 'remove' чтобы убрать фото).")
        return EDIT_NEW_VALUE
    else:
        await update.message.reply_text("Пришли новое значение для выбранного поля:")
        return EDIT_NEW_VALUE


async def edit_new_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    events = context.bot_data.get("events", {})
    event_id = context.user_data.get("edit_event_id")
    if not event_id:
        await update.message.reply_text("Контекст редактирования потерян.")
        return ConversationHandler.END
    event = events.get(event_id)
    if not event:
        await update.message.reply_text("Событие уже не существует.")
        return ConversationHandler.END
    fld = context.user_data.get("edit_field")
    if fld is None:
        await update.message.reply_text("Поле не выбрано.")
        return ConversationHandler.END
    try:
        if fld == 1:  # title
            event["title"] = update.message.text.strip()
        elif fld == 2:  # date
            event["date"] = update.message.text.strip()
        elif fld == 3:  # capacity
            capacity = int(update.message.text.strip())
            if capacity <= 0:
                raise ValueError
            event["capacity"] = capacity
            # if capacity reduced, trim joined and promote from waitlist appropriately
            if len(event["joined"]) > capacity:
                # move extra joined to waitlist (FIFO from tail)
                overflow = event["joined"][capacity:]
                event["joined"] = event["joined"][:capacity]
                # prepend overflow to front of waitlist (or append? choose append to preserve order)
                event["waitlist"] = event["waitlist"] + overflow
        elif fld == 4:  # location
            event["location"] = update.message.text.strip()
        elif fld == 5:  # description
            event["description"] = update.message.text.strip()
        elif fld == 6:  # photo
            txt = (update.message.text or "").strip().lower()
            if txt == "remove":
                event["photo_id"] = None
            else:
                if not update.message.photo:
                    await update.message.reply_text("Ожидаю фото или 'remove'.")
                    return EDIT_NEW_VALUE
                event["photo_id"] = update.message.photo[-1].file_id
        # save and persist
        events[event_id] = event
        context.bot_data["events"] = events
        context.bot_data.update({})
        # update channel message
        try:
            if event.get("photo_id"):
                await context.bot.edit_message_caption(
                    chat_id=event["channel"],
                    message_id=event["message_id"],
                    caption=format_event_message(event),
                    reply_markup=make_event_keyboard(event_id, event),
                    parse_mode="HTML",
                )
            else:
                await context.bot.edit_message_text(
                    chat_id=event["channel"],
                    message_id=event["message_id"],
                    text=format_event_message(event),
                    reply_markup=make_event_keyboard(event_id, event),
                    parse_mode="HTML",
                )
        except Exception as e:
            # if the message was originally photo and now removed, editing caption may fail; ignore
            print(f"Не удалось обновить сообщение при редактировании события {event_id}: {e}")
        await update.message.reply_text("Событие обновлено.")
    except ValueError:
        await update.message.reply_text("Для вместимости нужно положительное целое число. Попробуй ещё раз.")
        return EDIT_NEW_VALUE
    finally:
        # cleanup
        context.user_data.pop("edit_field", None)
        context.user_data.pop("edit_event_id", None)
    return ConversationHandler.END


async def edit_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("edit_field", None)
    context.user_data.pop("edit_event_id", None)
    await update.message.reply_text("Редактирование отменено.")
    return ConversationHandler.END


# --------------------------- bootstrap & main ---------------------------

def main():
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise ValueError("Пожалуйста, установите BOT_TOKEN в окружении.")
    persistence = PicklePersistence(filepath=DATA_FILE)
    app = ApplicationBuilder().token(token).persistence(persistence).build()

    # initialize events storage
    if "events" not in app.bot_data:
        app.bot_data["events"] = {}

    # Basic handlers
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("create_event", create_event_command_quick))
    app.add_handler(MessageHandler(filters.PHOTO & filters.Caption(True), create_event_from_photo_message))
    app.add_handler(CommandHandler("my_events", my_events_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(CommandHandler("export_event", export_event_command))
    app.add_handler(CommandHandler("delete_event", delete_event_command))

    # Edit conversation
    edit_conv = ConversationHandler(
        entry_points=[CommandHandler("edit_event", edit_event_command)],
        states={
            EDIT_SELECT_FIELD: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_select_field)],
            EDIT_NEW_VALUE: [
                MessageHandler((filters.PHOTO & ~filters.COMMAND) | (filters.TEXT & ~filters.COMMAND), edit_new_value)
            ],
        },
        fallbacks=[CommandHandler("cancel", edit_cancel)],
        conversation_timeout=300,
    )
    app.add_handler(edit_conv)

    # Create conversation (pошагово)
    create_conv = ConversationHandler(
        entry_points=[CommandHandler("create", create_start)],
        states={
            C_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_title)],
            C_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_date)],
            C_CAPACITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_capacity)],
            C_LOCATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_location)],
            C_DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_description)],
            C_PHOTO: [
                MessageHandler((filters.PHOTO & ~filters.COMMAND) | (filters.TEXT("skip") & ~filters.COMMAND), create_photo)
            ],
        },
        fallbacks=[CommandHandler("cancel", create_cancel)],
        conversation_timeout=600,
    )
    app.add_handler(create_conv)

    print("Бот запущен. Ctrl-C для остановки.")
    app.run_polling()


if __name__ == "__main__":
    main()
