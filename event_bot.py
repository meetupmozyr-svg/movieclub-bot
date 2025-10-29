#!/usr/bin/env python3
"""
Полнофункциональный Telegram бот для создания мероприятий с пошаговым вводом,
поддержкой фото, управлением событиями, экспортом участников и админ-функциями.

Совместимость: python-telegram-bot v20+
Запуск: установить BOT_TOKEN, CHANNEL, PORT, WEBHOOK_URL в переменных окружения.
Опции админов: ADMIN_IDS (comma-separated IDs) — необязательно.
"""

import os
import io
import csv
import re
from typing import Dict, List, Any
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
    InputFile,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    PicklePersistence,
    filters,
    ConversationHandler,
)
from telegram.error import BadRequest, Forbidden

# ---------------------------- Конфигурация ---------------------------------
DATA_FILE = "bot_persistence.pickle"

# Conversation states
(
    C_TITLE,
    C_DATE,
    C_CAPACITY,
    C_LOCATION,
    C_DESCRIPTION,
    C_PHOTO,
    EDIT_SELECT_FIELD,
    EDIT_NEW_VALUE,
) = range(8)

# Получить список админов из окружения (OPTIONAL)
def get_admin_ids() -> List[int]:
    raw = os.environ.get("ADMIN_IDS", "")
    if not raw:
        return []
    out = []
    for x in raw.split(","):
        x = x.strip()
        if x.isdigit():
            out.append(int(x))
    return out


ADMIN_IDS = get_admin_ids()

# ---------------------------- Утилиты -------------------------------------
def user_entry(from_user) -> Dict[str, Any]:
    """Создаёт запись пользователя для списка участников."""
    name = from_user.full_name or from_user.first_name or str(from_user.id)
    return {
        "id": from_user.id,
        "name": name,
        "username": f"@{from_user.username}" if from_user.username else None,
    }

def users_list_repr(users: List[Dict[str, Any]]) -> str:
    """Возвращает отформатированный список пользователей (HTML)"""
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
    joined_block = users_list_repr(event.get("joined", []))
    wait_block = users_list_repr(event.get("waitlist", []))
    joined_count = len(event.get("joined", []))
    wait_count = len(event.get("waitlist", []))

    text = (
        f"🎬 <b>{event['title']}</b>\n"
        f"📅 {event['date']}\n"
        f"📍 {event.get('location','(место не указано)')}\n\n"
        f"{event.get('description','(без описания)')}\n\n"
        f"👥 <b>{joined_count}/{event['capacity']}</b> участников\n\n"
        f"<b>✅ Участники:</b>\n{joined_block}\n\n"
        f"🕒 <b>Лист ожидания:</b> {wait_count}\n{wait_block}"
    )
    return text


def make_event_keyboard(event_id: str, event: Dict[str, Any]) -> InlineKeyboardMarkup:
    spots_filled = len(event.get("joined", []))
    capacity = event["capacity"]
    wait_len = len(event.get("waitlist", []))
    if spots_filled >= capacity:
        join_text = f"🕒 Встать в лист ожидания ({wait_len})"
    else:
        join_text = f"✅ Записаться ({spots_filled}/{capacity})"
    kb = [
        [
            InlineKeyboardButton(join_text, callback_data=f"join|{event_id}"),
            InlineKeyboardButton("❌ Не смогу прийти", callback_data=f"leave|{event_id}"),
        ]
    ]
    return InlineKeyboardMarkup(kb)


def is_admin(user_id: int, event: Dict[str, Any] = None) -> bool:
    if user_id in ADMIN_IDS:
        return True
    if event and event.get("creator_id") == user_id:
        return True
    return False

# Утилита для обновления сообщения в канале
async def update_event_message(context: ContextTypes.DEFAULT_TYPE, event_id: str, event: Dict[str, Any], chat_id_for_reply: int):
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
                disable_web_page_preview=True,
            )
    except Exception as e:
        print(f"Не удалось отредактировать сообщение события {event_id} в канале: {e}")
        # Это не должно мешать основному флоу, но может уведомить админа, если чат_id_for_reply известен
        if chat_id_for_reply != event["channel"]:
            try:
                await context.bot.send_message(
                    chat_id=chat_id_for_reply,
                    text=f"❗️ **ВНИМАНИЕ**: Не удалось обновить сообщение события {event_id} в канале. "
                         f"Убедитесь, что бот имеет права на редактирование сообщений.",
                    parse_mode="Markdown"
                )
            except Exception:
                pass

# ---------------------------- Команды -------------------------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я бот для мероприятий.\n\n"
        "Команды:\n"
        "/create — пошаговое создание события (рекомендуется)\n"
        "/create_event Название | Дата | Вместимость | Место | Описание — быстрая команда\n"
        "Отправь фото с подписью 'Название | Дата | Вместимость | ...' чтобы создать событие с фото.\n"
        "/my_events — показать твои записи\n"
        "/export_event <id> — экспорт участников (только админ/создатель)\n"
        "/delete_event <id> — удалить событие (только админ/создатель)\n"
        "/edit_event <id> — редактировать событие (пошагово, только админ/создатель)\n"
        "/remove_participant <id> <user_id> — удалить участника (только админ)\n" 
    )


# Быстрое создание через команду (legacy)
async def create_event_command_quick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return
        
    # АДМИН-ПРОВЕРКА
    if not is_admin(user.id):
        await update.message.reply_text("⛔️ У вас нет прав для быстрого создания событий.")
        return
    # =======================================
    
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

    # Атомарный ID
    event_counter = context.bot_data.get("event_counter", 0) + 1
    context.bot_data["event_counter"] = event_counter
    event_id = str(event_counter)

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


# Создание события из сообщения с фото + подпись
async def create_event_from_photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user = update.effective_user
    if not user or not msg:
        return
        
    # АДМИН-ПРОВЕРКА
    if not is_admin(user.id):
        await msg.reply_text("⛔️ У вас нет прав для создания событий с фото.")
        return
    # =======================================
        
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
    
    # Атомарный ID
    event_counter = context.bot_data.get("event_counter", 0) + 1
    context.bot_data["event_counter"] = event_counter
    event_id = str(event_counter)

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
            disable_web_page_preview=True,
        )
    except Exception as e:
        await msg.reply_text(f"Ошибка отправки фото: {e}")
        return
    event["message_id"] = sent.message_id
    context.bot_data["events"][event_id] = event
    context.bot_data.update({})
    await msg.reply_text(f"Событие с фото создано (ID {event_id}).")


# -------------------- Conversation: Create (пошагово) -----------------------
async def create_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    
    # АДМИН-ПРОВЕРКА
    if not is_admin(user.id):
        await update.message.reply_text("⛔️ У вас нет прав для пошагового создания событий.")
        return ConversationHandler.END
    # =======================================
    
    await update.message.reply_text("Создаём событие — шаг 1/6.\nПришли название события:")
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


async def create_photo_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text and update.message.text.strip().lower() == "skip":
        photo_id = None
    else:
        photo_id = update.message.photo[-1].file_id

    if "events" not in context.bot_data:
        context.bot_data["events"] = {}

    # Атомарный ID
    event_counter = context.bot_data.get("event_counter", 0) + 1
    context.bot_data["event_counter"] = event_counter
    event_id = str(event_counter)
    
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


async def create_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("new_event", None)
    await update.message.reply_text("Создание события отменено.")
    return ConversationHandler.END


# -------------------------- Кнопки (join/leave) ----------------------------
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer() # Ответ сразу, чтобы избежать ошибки "Query is too old"
    try:
        action, event_id = query.data.split("|")
    except (ValueError, AttributeError):
        await query.answer(text="Некорректные данные.", show_alert=True)
        return

    events = context.bot_data.get("events", {})
    event = events.get(event_id)
    if not event:
        await query.answer(text="Событие не найдено или устарело.", show_alert=True)
        return

    user = query.from_user
    ue = user_entry(user)

    # удаляем пользователя из ранее занятых списков
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

    # promote from waitlist if space freed
    while len(event["joined"]) < event["capacity"] and event["waitlist"]:
        promoted = event["waitlist"].pop(0)
        if promoted["id"] not in [u["id"] for u in event["joined"]]:
            event["joined"].append(promoted)
            try:
                await context.bot.send_message(
                    chat_id=promoted["id"],
                    text=(
                        f"Хорошая новость — освободилось место!\n\n"
                        f"Вы перенесены из листа ожидания в список подтверждённых для:\n"
                        f"{event['title']} — {event['date']}"
                    ),
                )
            except (BadRequest, Forbidden) as e:
                print(f"Не удалось уведомить пользователя {promoted['id']}: {e}")

    # persist
    events[event_id] = event
    context.bot_data["events"] = events
    context.bot_data.update({})

    # обновляем сообщение в канале (edit_caption для фото, edit_text для текста)
    await update_event_message(context, event_id, event, query.from_user.id) 

    # отправляем подтверждение пользователю (лично или alert)
    try:
        await query.from_user.send_message(response)
    except (BadRequest, Forbidden):
        await query.answer(text=response, show_alert=True)


# -------------------------- my_events -------------------------------------
async def my_events_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return
    uid = user.id
    events = context.bot_data.get("events", {})
    out = []
    for e in events.values():
        if any(u["id"] == uid for u in e.get("joined", [])):
            out.append(f"✅ Записан: {e['title']} — {e['date']} (ID {e['id']})")
        elif any(u["id"] == uid for u in e.get("waitlist", [])):
            out.append(f"🕒 Лист ожидания: {e['title']} — {e['date']} (ID {e['id']})")
    if not out:
        await update.message.reply_text("У вас нет записей.")
    else:
        await update.message.reply_text("<b>Ваши записи:</b>\n\n" + "\n".join(out), parse_mode="HTML")


# -------------------------- Admin: export / delete / edit / remove_participant ------------------
async def remove_participant_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return

    # 1. ПРОВЕРКА АДМИНИСТРАТОРА
    if not is_admin(user.id):
        await update.message.reply_text("⛔️ У вас нет прав для управления участниками.")
        return

    if len(context.args) < 2:
        await update.message.reply_text(
            "Использование: /remove_participant [ID_события] [ID_пользователя]\n"
            "Пример: /remove_participant 5 123456789"
        )
        return

    try:
        event_id = context.args[0].strip()
        user_to_remove_id = int(context.args[1].strip())
    except ValueError:
        await update.message.reply_text("❗️ ID события и ID пользователя должны быть числами.")
        return

    events = context.bot_data.get('events', {})
    event = events.get(event_id)

    if not event:
        await update.message.reply_text(f"❗️ Событие с ID {event_id} не найдено.")
        return

    # 2. ПОПЫТКА УДАЛЕНИЯ
    user_removed = False
    removed_from_list = None
    
    # Ищем в основном списке
    original_joined_count = len(event.get('joined', []))
    event['joined'] = [u for u in event.get('joined', []) if u['id'] != user_to_remove_id]
    if len(event['joined']) < original_joined_count:
        user_removed = True
        removed_from_list = "основного списка"
    
    # Ищем в листе ожидания
    original_waitlist_count = len(event.get('waitlist', []))
    event['waitlist'] = [u for u in event.get('waitlist', []) if u['id'] != user_to_remove_id]
    if len(event['waitlist']) < original_waitlist_count and not user_removed:
        user_removed = True
        removed_from_list = "листа ожидания"

    if user_removed:
        # 3. АВТОМАТИЧЕСКОЕ ПРОДВИЖЕНИЕ (если освободилось место)
        promoted_user_entry = None
        
        # Проверяем, если место освободилось И есть лист ожидания
        if (len(event['joined']) < event['capacity']) and event['waitlist']:
            # Продвигаем первого в листе ожидания
            promoted_user_entry = event['waitlist'].pop(0)
            event['joined'].append(promoted_user_entry)
            
            # Отправляем уведомление продвинутому пользователю
            try:
                await context.bot.send_message(
                    chat_id=promoted_user_entry['id'],
                    text=f"🎉 **Поздравляем!** Вы переведены из листа ожидания в основной список на событие '{event['title']}' (ID: {event_id}).",
                    parse_mode="Markdown"
                )
            except Exception as e:
                print(f"Не удалось уведомить пользователя {promoted_user_entry['id']} о продвижении: {e}")

        # 4. ОБНОВЛЕНИЕ СООБЩЕНИЯ В КАНАЛЕ
        context.bot_data["events"][event_id] = event
        context.bot_data.update({})
        await update_event_message(context, event_id, event, update.message.chat_id)
        
        confirmation_text = f"✅ Участник ID **{user_to_remove_id}** удален из **{removed_from_list}** события **{event['title']}** (ID: {event_id})."
        
        if promoted_user_entry:
            confirmation_text += f"\n➡️ Пользователь **{promoted_user_entry['name']}** (ID: {promoted_user_entry['id']}) автоматически переведен из листа ожидания."
            
        await update.message.reply_text(confirmation_text, parse_mode="Markdown")

    else:
        await update.message.reply_text(
            f"❗️ Пользователь ID **{user_to_remove_id}** не найден в списках события **{event_id}**.",
            parse_mode="Markdown"
        )

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
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["status", "id", "name", "username"])
    for u in event.get("joined", []):
        writer.writerow(["joined", u.get("id"), u.get("name"), u.get("username") or ""])
    for u in event.get("waitlist", []):
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
        await update.message.reply_text(
             f"Событие не найдено. Указанный ID: **{event_id}**. "
             f"Убедитесь, что ID правильный и не содержит пробелов."
        )
        return

    if not is_admin(user.id, event):
        await update.message.reply_text("Только админ или создатель может удалить событие.")
        return

    # Попытаться удалить сообщение в канале (если есть права)
    try:
        await context.bot.delete_message(chat_id=event["channel"], message_id=event["message_id"])
    except Exception as e:
        print(f"Ошибка при удалении сообщения канала для события {event_id}: {e}")
        pass

    # Удаление из хранилища
    events.pop(event_id, None)
    context.bot_data["events"] = events
    context.bot_data.update({})

    await update.message.reply_text(f"Событие **{event_id}** удалено.")

# Редактирование события (Conversation)
async def edit_event_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not context.args:
        await update.message.reply_text("Использование: /edit_event <event_id>")
        return ConversationHandler.END
    event_id = context.args[0].strip()
    events = context.bot_data.get("events", {})
    event = events.get(event_id)
    if not event:
        await update.message.reply_text("Событие не найдено.")
        return ConversationHandler.END
    if not is_admin(user.id, event):
        await update.message.reply_text("Только админ или создатель может редактировать событие.")
        return ConversationHandler.END
    context.user_data["edit_event_id"] = event_id
    await update.message.reply_text(
        "Что редактировать? Отправь номер:\n"
        "1 — Название\n2 — Дата\n3 — Вместимость\n4 — Место\n5 — Описание\n6 — Фото (пришли новое фото или 'remove')"
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
        if fld == 1:
            event["title"] = update.message.text.strip()
        elif fld == 2:
            event["date"] = update.message.text.strip()
        elif fld == 3:
            capacity = int(update.message.text.strip())
            if capacity <= 0:
                raise ValueError
            event["capacity"] = capacity
            # если уменьшили вместимость, корректируем списки
            if len(event["joined"]) > capacity:
                overflow = event["joined"][capacity:]
                event["joined"] = event["joined"][:capacity]
                # overflow возвращаем в waitlist в конец
                event["waitlist"].extend(overflow)
        elif fld == 4:
            event["location"] = update.message.text.strip()
        elif fld == 5:
            event["description"] = update.message.text.strip()
        elif fld == 6:
            txt = (update.message.text or "").strip().lower()
            if txt == "remove":
                event["photo_id"] = None
            else:
                if not update.message.photo:
                    await update.message.reply_text("Ожидаю фото или 'remove'.")
                    return EDIT_NEW_VALUE
                event["photo_id"] = update.message.photo[-1].file_id
        # сохранить и обновить
        events[event_id] = event
        context.bot_data["events"] = events
        context.bot_data.update({})
        
        await update_event_message(context, event_id, event, update.message.chat_id)
        
        await update.message.reply_text("Событие обновлено.")
    except ValueError:
        await update.message.reply_text("Для вместимости нужно положительное целое число. Попробуй ещё раз.")
        return EDIT_NEW_VALUE
    finally:
        context.user_data.pop("edit_field", None)
        context.user_data.pop("edit_event_id", None)
    return ConversationHandler.END


async def edit_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("edit_field", None)
    context.user_data.pop("edit_event_id", None)
    await update.message.reply_text("Редактирование отменено.")
    return ConversationHandler.END


# ---------------------------- Main / Bootstrap -----------------------------
def main():
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise ValueError("Пожалуйста, установите BOT_TOKEN в окружении.")

    # Переменные для Webhook
    port = int(os.environ.get('PORT', '8443')) 
    webhook_url = os.environ.get("WEBHOOK_URL") 
    if not webhook_url:
        raise ValueError("Пожалуйста, установите WEBHOOK_URL (URL вашего сервиса Render) в окружении.")

    WEBHOOK_PATH = f"/webhook/{token}"

    # --- БЕЗОПАСНАЯ ИНИЦИАЛИЗАЦИЯ ПЕРСИСТЕНСА ---
    try:
        persistence = PicklePersistence(filepath=DATA_FILE)
        print("Персистенс загружен успешно.")
    except Exception as e:
        print(f"ВНИМАНИЕ: Ошибка загрузки персистенса ({e}). Начинаем с чистых данных.")
        persistence = PicklePersistence(filepath=DATA_FILE, on_flush=False)
    # ----------------------------------------------------------------------
    
    app = (
        ApplicationBuilder()
        .token(token)
        .persistence(persistence)
        .build()
    )

    if "events" not in app.bot_data:
        app.bot_data["events"] = {}

    # 1. Базовые хендлеры (команды)
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("create_event", create_event_command_quick))
    app.add_handler(CommandHandler("my_events", my_events_command))
    app.add_handler(CommandHandler("export_event", export_event_command))
    app.add_handler(CommandHandler("delete_event", delete_event_command))
    app.add_handler(CommandHandler("remove_participant", remove_participant_command))

    # 2. Хендлер для создания события с фото (с подписью)
    app.add_handler(
        MessageHandler(
            filters.PHOTO & filters.CAPTION,
            create_event_from_photo_message
        )
    )

    # 3. Хендлер для кнопок (join/leave)
    app.add_handler(CallbackQueryHandler(button_handler, pattern=r"^(join|leave)\|\d+$"))

    # 4. Хендлер для пошагового создания (ConversationHandler)
    create_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("create", create_start)],
        states={
            C_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_title)],
            C_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_date)],
            C_CAPACITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_capacity)],
            C_LOCATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_location)],
            C_DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_description)],
            C_PHOTO: [
                MessageHandler(filters.PHOTO & ~filters.COMMAND, create_photo_step),
                MessageHandler(filters.Regex(re.compile("^skip$", re.IGNORECASE)), create_photo_step), # <-- ИСПРАВЛЕНО
            ],
        },
        fallbacks=[CommandHandler("cancel", create_cancel)],
        persistent=True,
        name="create_conversation",
    )
    app.add_handler(create_conv_handler)
    
    # 5. Хендлер для редактирования (ConversationHandler)
    edit_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("edit_event", edit_event_command)],
        states={
            EDIT_SELECT_FIELD: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_select_field)],
            EDIT_NEW_VALUE: [
                MessageHandler((filters.TEXT | filters.PHOTO) & ~filters.COMMAND, edit_new_value),
            ],
        },
        fallbacks=[CommandHandler("cancel", edit_cancel)],
        persistent=True,
        name="edit_conversation",
    )
    app.add_handler(edit_conv_handler)

    # Запуск бота через Webhook (для Render/Heroku)
    print("Бот настроен. Запуск в режиме Webhook...")
    app.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=WEBHOOK_PATH,
        webhook_url=webhook_url + WEBHOOK_PATH,
    )

if __name__ == "__main__":
    main()
