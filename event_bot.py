#!/usr/bin/env python3
"""
Полнофункциональный Telegram бот для создания мероприятий с пошаговым вводом,
поддержкой фото, управлением событиями, экспортом участников и админ-функциями.

Совместимость: python-telegram-bot v20+
Запуск: установить BOT_TOKEN, CHANNEL (обязательно), PORT, WEBHOOK_URL в переменных окружения.
Опции админов: ADMIN_IDS (comma-separated IDs) — необязательно.
"""

import os
import io
import csv
import re
import traceback
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

# 🛑 Обязательная проверка CHANNEL при запуске
CHANNEL = os.environ.get("CHANNEL")
if not CHANNEL:
    # 📌 Используем более мягкий подход для запуска, но логируем
    print("⚠️ WARNING: Переменная окружения 'CHANNEL' не установлена. Используется дефолтное значение '@testchannel'.")
    CHANNEL = "@testchannel"

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
    # Более надежная проверка имени
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
        # 📌 Улучшенная логика отображения: username только если он есть
        display = name
        if username and username != "@None":
            display += f" ({username})" 
        lines.append(f"• <a href='tg://user?id={uid}'>{display}</a>")
    return "\n".join(lines)


def format_event_message(event: Dict[str, Any]) -> str:
    joined_block = users_list_repr(event.get("joined", []))
    wait_block = users_list_repr(event.get("waitlist", []))
    joined_count = len(event.get("joined", []))
    wait_count = len(event.get("waitlist", []))
    
    # 📌 Добавлена проверка на существование ключей, хотя они должны быть
    text = (
        f"🎬 <b>{event.get('title', 'Без названия')}</b>\n"
        f"📅 {event.get('date', 'Дата не указана')}\n"
        f"📍 {event.get('location','(место не указано)')}\n\n"
        f"{event.get('description','(без описания)')}\n\n"
        f"👥 <b>{joined_count}/{event.get('capacity', 0)}</b> участников\n\n"
        f"<b>✅ Участники:</b>\n{joined_block}\n\n"
        f"🕒 <b>Лист ожидания:</b> {wait_count}\n{wait_block}"
    )
    return text


def make_event_keyboard(event_id: str, event: Dict[str, Any]) -> InlineKeyboardMarkup:
    spots_filled = len(event.get("joined", []))
    capacity = event.get("capacity", 0) # 📌 Дефолт 0
    wait_len = len(event.get("waitlist", []))
    
    # 📌 Логика кнопки "Записаться"
    if capacity <= 0: # 📌 Обработка вместимости <= 0
        join_text = "🔒 Запись закрыта"
        join_data = "no_join" # Новое, чтобы избежать ошибок
    elif spots_filled >= capacity:
        join_text = f"🕒 Встать в лист ожидания ({wait_len})"
        join_data = f"join|{event_id}"
    else:
        join_text = f"✅ Записаться ({spots_filled}/{capacity})"
        join_data = f"join|{event_id}"
        
    kb = [
        [
            InlineKeyboardButton(join_text, callback_data=join_data),
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
    # 📌 Используем глобальную CHANNEL
    channel_id = event.get("channel", CHANNEL)
    message_id = event.get("message_id")
    if not message_id:
        print(f"Невозможно обновить сообщение: отсутствует message_id для события {event_id}")
        return

    try:
        if event.get("photo_id"):
            await context.bot.edit_message_caption(
                chat_id=channel_id,
                message_id=message_id,
                caption=format_event_message(event),
                reply_markup=make_event_keyboard(event_id, event),
                parse_mode="HTML",
            )
        else:
            await context.bot.edit_message_text(
                chat_id=channel_id,
                message_id=message_id,
                text=format_event_message(event),
                reply_markup=make_event_keyboard(event_id, event),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
    except Exception as e:
        print(f"Не удалось отредактировать сообщение события {event_id} в канале: {e}")
        # Это не должно мешать основному флоу, но может уведомить админа, если chat_id_for_reply известен
        if chat_id_for_reply != channel_id:
            try:
                await context.bot.send_message(
                    chat_id=chat_id_for_reply,
                    text=(f"❗️ **ВНИМАНИЕ**: Не удалось обновить сообщение события {event_id} в канале. "
                          f"Убедитесь, что бот имеет права на редактирование сообщений."),
                    parse_mode="Markdown"
                )
            except Exception:
                pass

# ---------------------------- Команды -------------------------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 📌 Добавлена проверка на update.message, чтобы избежать NoneType в случае CallbackQuery
    if not update.message:
        return
    await update.message.reply_text(
        "Привет! Я бот для мероприятий.\n\n"
        "Команды:\n"
        "/create — пошаговое создание события (только админ)\n"
        "/create_event Название | Дата | Вместимость | Место | Описание — быстрая команда (только админ)\n"
        "Отправь фото с подписью 'Название | Дата | Вместимость | ...' чтобы создать событие с фото (только админ).\n"
        "/my_events — показать твои записи\n"
        "/export_event <id> — экспорт участников (только админ/создатель)\n"
        "/delete_event <id> — удалить событие (только админ/создатель)\n"
        "/edit_event <id> — редактировать событие (пошагово, только админ/создатель)\n"
        "/remove_participant <id> <user_id> — удалить участника (только админ)\n"
    )


# Быстрое создание через команду (legacy)
async def create_event_command_quick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not update.message:
        return
        
    # Проверка администратора
    if not is_admin(user.id):
        await update.message.reply_text("⛔️ У вас нет прав для быстрого создания событий.")
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
    
    # 📌 Добавлена проверка на минимальное количество частей
    if len(parts) < 3:
        await update.message.reply_text("Нужно минимум: Название | Дата | Вместимость")
        return
        
    try:
        title, date = parts[0], parts[1]
        capacity = int(parts[2])
        if capacity <= 0:
            raise ValueError("Вместимость должна быть положительным числом.")
    except (ValueError, IndexError) as e: # 📌 Интегрирована IndexError
        await update.message.reply_text(f"Ошибка в формате: {e}. Вместимость должна быть положительным числом.")
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
        "channel": CHANNEL, # 📌 Используем глобальную CHANNEL
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
        await update.message.reply_text(f"Ошибка отправки в канал ({CHANNEL}): {e}")
        return
        
    event["message_id"] = sent.message_id
    context.bot_data["events"][event_id] = event
    context.bot_data.update({})
    await update.message.reply_text(f"Событие создано и опубликовано (ID **{event_id}**).", parse_mode="Markdown")


# Создание события из сообщения с фото + подпись
async def create_event_from_photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user = update.effective_user
    if not user or not msg or not msg.photo: # Проверка на наличие фото
        return
        
    # Проверка администратора
    if not is_admin(user.id):
        # Чтобы не спамить, можно пропустить ответ, но лучше уведомить
        return
        
    caption = msg.caption or ""
    parts = [p.strip() for p in caption.split("|")]
    
    # 📌 Проверка на минимальное количество частей
    if len(parts) < 3:
        # Без ответа, чтобы не мешать обычным фото
        return
        
    if "events" not in context.bot_data:
        context.bot_data["events"] = {}
        
    try:
        title, date = parts[0], parts[1]
        capacity = int(parts[2])
        if capacity <= 0:
            raise ValueError("Вместимость должна быть положительным числом.")
    except (ValueError, IndexError):
        await msg.reply_text("Вместимость должна быть положительным числом.")
        return
        
    location = parts[3] if len(parts) > 3 else ""
    description = parts[4] if len(parts) > 4 else ""
    
    # Атомарный ID
    event_counter = context.bot_data.get("event_counter", 0) + 1
    context.bot_data["event_counter"] = event_counter
    event_id = str(event_counter)

    photo_file_id = msg.photo[-1].file_id if msg.photo else None
    
    if not photo_file_id:
        # Это не должно произойти из-за начальной проверки, но для надежности
        await msg.reply_text("Не удалось получить ID фото.")
        return

    event = {
        "id": event_id,
        "title": title,
        "date": date,
        "capacity": capacity,
        "location": location,
        "description": description,
        "creator_id": user.id,
        "message_id": None,
        "channel": CHANNEL, # 📌 Используем глобальную CHANNEL
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
        await msg.reply_text(f"Ошибка отправки фото в канал ({CHANNEL}): {e}")
        return
        
    event["message_id"] = sent.message_id
    context.bot_data["events"][event_id] = event
    context.bot_data.update({})
    await msg.reply_text(f"Событие с фото создано (ID **{event_id}**).", parse_mode="Markdown")


# -------------------- Conversation: Create (пошагово) -----------------------
async def create_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    
    # 📌 Проверка на update.message, необходимая для CommandHandler
    if not update.message:
        return ConversationHandler.END

    # Проверка администратора
    if not is_admin(user.id):
        await update.message.reply_text("⛔️ У вас нет прав для пошагового создания событий.")
        return ConversationHandler.END
    
    await update.message.reply_text("Создаём событие — шаг 1/6.\nПришли **название** события:")
    context.user_data["new_event"] = {}
    return C_TITLE


async def create_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.text:
        await update.message.reply_text("Ожидаю текст для названия.")
        return C_TITLE
        
    context.user_data["new_event"]["title"] = update.message.text.strip()
    await update.message.reply_text("Шаг 2/6. Укажи **дату/время** (например: 2025-11-02 20:00):")
    return C_DATE


async def create_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.text:
        await update.message.reply_text("Ожидаю текст для даты.")
        return C_DATE
        
    context.user_data["new_event"]["date"] = update.message.text.strip()
    await update.message.reply_text("Шаг 3/6. Укажи **вместимость** (целое число):")
    return C_CAPACITY


async def create_capacity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.text:
        await update.message.reply_text("Ожидаю число для вместимости.")
        return C_CAPACITY
        
    txt = update.message.text.strip()
    try:
        capacity = int(txt)
        if capacity <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Вместимость должна быть **положительным целым числом**. Попробуй ещё раз:")
        return C_CAPACITY
        
    context.user_data["new_event"]["capacity"] = capacity
    await update.message.reply_text("Шаг 4/6. Укажи **место** (или отправь '-' чтобы пропустить):")
    return C_LOCATION


async def create_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.text:
        await update.message.reply_text("Ожидаю текст для места.")
        return C_LOCATION
        
    txt = update.message.text.strip()
    context.user_data["new_event"]["location"] = "" if txt == "-" else txt
    await update.message.reply_text("Шаг 5/6. Напиши **описание** (или '-' чтобы пропустить):")
    return C_DESCRIPTION


async def create_description(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.text:
        await update.message.reply_text("Ожидаю текст для описания.")
        return C_DESCRIPTION
        
    txt = update.message.text.strip()
    context.user_data["new_event"]["description"] = "" if txt == "-" else txt
    await update.message.reply_text(
        "Шаг 6/6 (опционально). Пришли **фото** сейчас, либо отправь **'skip'** чтобы завершить без фото."
    )
    return C_PHOTO


async def create_photo_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo_id = None
    
    if update.message.photo:
        photo_id = update.message.photo[-1].file_id
    elif update.message.text and update.message.text.strip().lower() == "skip":
        photo_id = None # Пропуск
    elif update.message.text:
        # Если пришел текст, но не 'skip', просим повторить
        await update.message.reply_text("Пожалуйста, пришли **фото** или слово **'skip'**.")
        return C_PHOTO
    else:
        # Если ни фото, ни текст, тоже просим повторить
        await update.message.reply_text("Пожалуйста, пришли **фото** или слово **'skip'**.")
        return C_PHOTO


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
        "channel": CHANNEL, # 📌 Используем глобальную CHANNEL
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
        await update.message.reply_text(f"Ошибка при публикации в канал ({CHANNEL}): {e}")
        context.user_data.pop("new_event", None)
        return ConversationHandler.END

    event["message_id"] = sent.message_id
    context.bot_data["events"][event_id] = event
    context.bot_data.update({})

    await update.message.reply_text(f"Событие создано и опубликовано (ID **{event_id}**).", parse_mode="Markdown")
    context.user_data.pop("new_event", None)
    return ConversationHandler.END


async def create_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 📌 Проверка на update.message, необходимая для CommandHandler
    if not update.message:
        return ConversationHandler.END
        
    context.user_data.pop("new_event", None)
    await update.message.reply_text("Создание события отменено.")
    return ConversationHandler.END


# -------------------------- Кнопки (join/leave) ----------------------------
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    # 📌 Используем query.answer() сразу, чтобы избежать ошибки "Query is too old"
    await query.answer() 
    
    # Обработка 'no_join'
    if query.data == "no_join":
        await query.answer(text="Запись на это событие закрыта.", show_alert=True)
        return

    # Обработка некорректных данных
    if not query.data:
        await query.answer(text="Некорректные данные запроса.", show_alert=True)
        return
        
    try:
        action, event_id = query.data.split("|")
    except (ValueError, TypeError):
        await query.answer(text="Некорректный формат данных.", show_alert=True)
        return

    events = context.bot_data.get("events", {})
    event = events.get(event_id)
    if not event:
        await query.answer(text="Событие не найдено или устарело.", show_alert=True)
        return

    user = query.from_user
    ue = user_entry(user)
    uid = ue["id"]

    # Проверка, находится ли пользователь уже в списке
    is_joined = any(u["id"] == uid for u in event.get("joined", []))
    is_waiting = any(u["id"] == uid for u in event.get("waitlist", []))
    
    response = ""
    
    # 1. Сначала удаляем пользователя, если он был
    if is_joined or is_waiting:
        event["joined"] = [u for u in event["joined"] if u["id"] != uid]
        event["waitlist"] = [u for u in event["waitlist"] if u["id"] != uid]
        
    # 2. Обрабатываем новое действие
    if action == "join":
        if len(event["joined"]) < event.get("capacity", 0):
            event["joined"].append(ue)
            response = "Вы успешно записаны ✅"
        else:
            event["waitlist"].append(ue)
            response = "Событие полное — вы добавлены в лист ожидания 🕒"
    elif action == "leave":
        # Если пользователь был в списках, даем подтверждение
        if is_joined or is_waiting:
            response = "Вы помечены как не придёте ❌"
        else:
            # Если пользователь не был в списках, но нажал 'leave'
            await query.answer(text="Вы не были записаны на это событие.", show_alert=True)
            # Не обновляем сообщение, т.к. ничего не изменилось
            return 
    else:
        response = "Неизвестное действие."

    # 3. promote from waitlist if space freed (только если место освободилось из joined)
    if is_joined and action == "leave":
        while len(event["joined"]) < event["capacity"] and event["waitlist"]:
            promoted = event["waitlist"].pop(0)
            event["joined"].append(promoted) 
            try:
                await context.bot.send_message(
                    chat_id=promoted["id"],
                    text=(
                        f"Хорошая новость — освободилось место!\n\n"
                        f"Вы перенесены из листа ожидания в список подтверждённых для:\n"
                        f"**{event['title']}** — {event['date']}"
                    ),
                    parse_mode="Markdown"
                )
            except (BadRequest, Forbidden) as e:
                print(f"Не удалось уведомить пользователя {promoted['id']} о продвижении: {e}")

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
        # Если не удалось отправить в личку, показываем alert
        await query.answer(text=response, show_alert=True)


# -------------------------- my_events -------------------------------------
async def my_events_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not update.message:
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
    if not user or not update.message:
        return

    # 1. ПРОВЕРКА АДМИНИСТРАТОРА И АРГУМЕНТОВ
    # 📌 Только полный админ может удалять
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔️ У вас нет прав для управления участниками.")
        return
        
    if len(context.args) < 2:
        await update.message.reply_text(
            "Использование: `/remove_participant [ID_события] [ID_пользователя]`\n"
            "Пример: `/remove_participant 5 123456789`",
            parse_mode="Markdown"
        )
        return

    try:
        event_id = context.args[0].strip()
        user_to_remove_id = int(context.args[1].strip())
    except ValueError:
        await update.message.reply_text("❗️ ID события и ID пользователя должны быть числами.")
        return
    except IndexError:
        await update.message.reply_text("Недостаточно аргументов. Используйте `/remove_participant [ID_события] [ID_пользователя]`")
        return


    events = context.bot_data.get('events', {})
    event = events.get(event_id)

    if not event:
        await update.message.reply_text(f"❗️ Событие с ID **{event_id}** не найдено.", parse_mode="Markdown")
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
        promoted_users_count = 0
        
        # Промоушен происходит, только если место освободилось в основном списке
        if removed_from_list == "основного списка":
            while len(event["joined"]) < event["capacity"] and event["waitlist"]:
                promoted_user_entry = event['waitlist'].pop(0)
                event['joined'].append(promoted_user_entry)
                promoted_users_count += 1
                
                # Отправляем уведомление продвинутому пользователю
                try:
                    await context.bot.send_message(
                        chat_id=promoted_user_entry['id'],
                        text=f"🎉 **Поздравляем!** Вы переведены из листа ожидания в основной список на событие '*{event['title']}*' (ID: {event_id}).",
                        parse_mode="Markdown"
                    )
                except Exception as e:
                    print(f"Не удалось уведомить пользователя {promoted_user_entry['id']} о продвижении: {e}")

        # 4. ОБНОВЛЕНИЕ СООБЩЕНИЯ В КАНАЛЕ
        context.bot_data["events"][event_id] = event
        context.bot_data.update({})
        await update_event_message(context, event_id, event, update.message.chat_id)
        
        confirmation_text = f"✅ Участник ID **{user_to_remove_id}** удален из **{removed_from_list}** события **{event['title']}** (ID: {event_id})."
        
        if promoted_users_count > 0:
            confirmation_text += f"\n➡️ **{promoted_users_count}** участник(ов) автоматически переведен(ы) из листа ожидания."
            
        await update.message.reply_text(confirmation_text, parse_mode="Markdown")

    else:
        await update.message.reply_text(
            f"❗️ Пользователь ID **{user_to_remove_id}** не найден в списках события **{event_id}**.",
            parse_mode="Markdown"
        )


async def export_event_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    # 📌 Защита от отсутствия аргументов
    if not user or not context.args or not update.effective_message:
        await update.effective_message.reply_text("Использование: `/export_event <event_id>`", parse_mode="Markdown")
        return
        
    event_id = context.args[0].strip()
    events = context.bot_data.get("events", {})
    event = events.get(event_id)
    
    if not event:
        await update.effective_message.reply_text(f"Событие с ID **{event_id}** не найдено.", parse_mode="Markdown")
        return
        
    if not is_admin(user.id, event):
        await update.effective_message.reply_text("Только админ или создатель может экспортировать участников.")
        return
        
    # 📌 Улучшено кодирование: использование utf-8-sig для корректного отображения кириллицы в Excel
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["status", "id", "name", "username"])
    for u in event.get("joined", []):
        writer.writerow(["joined", u.get("id"), u.get("name"), u.get("username") or ""])
    for u in event.get("waitlist", []):
        writer.writerow(["waitlist", u.get("id"), u.get("name"), u.get("username") or ""])
        
    buf.seek(0)
    # Используем io.BytesIO и 'utf-8-sig' для CSV с кириллицей
    bio = io.BytesIO(buf.getvalue().encode("utf-8-sig")) 
    bio.name = f"event_{event_id}_participants.csv"
    
    try:
        # Отправка документа в приватный чат пользователя
        await context.bot.send_document(
            chat_id=user.id, 
            document=InputFile(bio), 
            caption=f"Экспорт участников события *{event_id}*", 
            parse_mode="Markdown"
        )
        # 📌 Подтверждение в чат, откуда пришла команда (более надежно)
        await update.effective_message.reply_text(
            f"✅ Экспорт участников для события **{event_id}** отправлен вам в личные сообщения.", 
            parse_mode="Markdown"
        )
    except (BadRequest, Forbidden) as e:
        # 📌 Исправленная обработка ошибки: используем effective_chat.id
        await context.bot.send_message(
            chat_id=update.effective_chat.id, 
            text=f"❗️ Не удалось отправить файл в личные сообщения: {e}.\nУбедитесь, что вы запустили команду в приватном чате с ботом и он может вам писать.",
            parse_mode="Markdown"
        )


async def delete_event_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    # 📌 Защита от отсутствия аргументов
    if not user or not context.args or not update.effective_message:
        await update.effective_message.reply_text("Использование: `/delete_event <event_id>`", parse_mode="Markdown")
        return

    event_id = context.args[0].strip() 

    events = context.bot_data.get("events", {})
    event = events.get(event_id)

    if not event:
        await update.effective_message.reply_text(
             f"Событие не найдено. Указанный ID: **{event_id}**. "
             f"Убедитесь, что ID правильный и не содержит пробелов.",
             parse_mode="Markdown"
        )
        return

    if not is_admin(user.id, event):
        await update.effective_message.reply_text("Только админ или создатель может удалить событие.")
        return

    # Попытаться удалить сообщение в канале (если есть права)
    try:
        await context.bot.delete_message(chat_id=event.get("channel", CHANNEL), message_id=event.get("message_id"))
    except Exception as e:
        print(f"Ошибка при удалении сообщения канала для события {event_id}: {e}")
        pass

    # Удаление из хранилища
    events.pop(event_id, None)
    context.bot_data["events"] = events
    context.bot_data.update({})

    await update.effective_message.reply_text(f"Событие **{event_id}** удалено.", parse_mode="Markdown")

# Редактирование события (Conversation)
async def edit_event_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    # 📌 Защита от отсутствия аргументов
    if not user or not context.args or not update.message:
        await update.effective_message.reply_text("Использование: `/edit_event <event_id>`", parse_mode="Markdown")
        return ConversationHandler.END # 📌 Возвращаем END, если нет нужных данных
        
    event_id = context.args[0].strip()
    events = context.bot_data.get("events", {})
    event = events.get(event_id)
    
    if not event:
        await update.effective_message.reply_text(f"Событие с ID **{event_id}** не найдено.", parse_mode="Markdown")
        return ConversationHandler.END
        
    if not is_admin(user.id, event):
        await update.effective_message.reply_text("Только админ или создатель может редактировать событие.")
        return ConversationHandler.END
        
    context.user_data["edit_event_id"] = event_id
    await update.message.reply_text(
        "Что редактировать? Отправь номер:\n"
        "1 — Название\n2 — Дата\n3 — Вместимость\n4 — Место\n5 — Описание\n6 — Фото (пришли новое фото или 'remove')"
    )
    return EDIT_SELECT_FIELD


async def edit_select_field(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        await update.message.reply_text("Ожидаю номер поля (1-6).")
        return EDIT_SELECT_FIELD
        
    txt = update.message.text.strip()
    if txt not in {"1", "2", "3", "4", "5", "6"}:
        await update.message.reply_text("Неверный выбор. Отправь номер (1-6).")
        return EDIT_SELECT_FIELD
        
    context.user_data["edit_field"] = int(txt)
    
    if txt == "6":
        await update.message.reply_text("Пришли **новое фото** (или **'remove'** чтобы убрать фото).")
    else:
        await update.message.reply_text("Пришли **новое значение** для выбранного поля:")
        
    return EDIT_NEW_VALUE


async def edit_new_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return EDIT_NEW_VALUE # Должен прийти либо текст, либо фото

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
        
    
    message_text = update.message.text.strip() if update.message.text else ""
    
    try:
        field_name = ""
        if fld in {1, 2, 4, 5}: # Текстовые поля
            if not message_text:
                await update.message.reply_text("Ожидаю текстовое значение. Попробуй ещё раз.")
                return EDIT_NEW_VALUE
            if fld == 1:
                event["title"] = message_text
                field_name = "Название"
            elif fld == 2:
                event["date"] = message_text
                field_name = "Дата"
            elif fld == 4:
                event["location"] = message_text
                field_name = "Место"
            elif fld == 5:
                event["description"] = message_text
                field_name = "Описание"
                
        elif fld == 3: # Вместимость
            if not message_text:
                await update.message.reply_text("Ожидаю числовое значение для вместимости. Попробуй ещё раз.")
                return EDIT_NEW_VALUE
                
            capacity = int(message_text)
            if capacity <= 0:
                raise ValueError("Вместимость должна быть положительным числом.")
            
            event["capacity"] = capacity
            field_name = "Вместимость"
            
            # Корректировка списков при изменении вместимости
            promoted_count = 0
            if len(event["joined"]) > capacity:
                # Уменьшение вместимости: переносим лишних в waitlist
                overflow = event["joined"][capacity:]
                event["joined"] = event["joined"][:capacity]
                event["waitlist"] = overflow + event["waitlist"] # Добавляем в начало waitlist
            elif len(event["joined"]) < capacity:
                # Увеличение вместимости: продвигаем из waitlist
                while len(event["joined"]) < capacity and event["waitlist"]:
                    promoted = event["waitlist"].pop(0)
                    event["joined"].append(promoted)
                    promoted_count += 1
                    # Отправляем уведомление продвинутому пользователю
                    try:
                        await context.bot.send_message(
                            chat_id=promoted["id"],
                            text=f"🎉 **Поздравляем!** Вы переведены из листа ожидания в основной список на событие '*{event['title']}*' (ID: {event_id}).",
                            parse_mode="Markdown"
                        )
                    except Exception:
                        print(f"Не удалось уведомить пользователя {promoted['id']} о продвижении.")
            
            if promoted_count > 0:
                await update.message.reply_text(f"➡️ Дополнительно **{promoted_count}** участник(ов) переведен(ы) из листа ожидания.")


        elif fld == 6: # Фото
            photo_id = None
            if update.message.photo:
                photo_id = update.message.photo[-1].file_id
                field_name = "Фото"
            elif message_text.lower() == "remove":
                photo_id = None
                field_name = "Фото (удалено)"
            else:
                await update.message.reply_text("Ожидаю **фото** или слово **'remove'**. Попробуй ещё раз.")
                return EDIT_NEW_VALUE
                
            event["photo_id"] = photo_id

        else:
            await update.message.reply_text("Неизвестное поле для редактирования.")
            return ConversationHandler.END

        # Публикация изменений в канал
        await update_event_message(context, event_id, event, update.message.chat_id)
        
        # Обновление персистентности
        context.bot_data["events"][event_id] = event
        context.bot_data.update({})
        
        await update.message.reply_text(
            f"✅ Поле **{field_name}** обновлено для события **{event_id}**.", 
            parse_mode="Markdown"
        )
        context.user_data.pop("edit_event_id", None)
        context.user_data.pop("edit_field", None)
        return ConversationHandler.END

    except ValueError as e:
        await update.message.reply_text(f"❗️ Ошибка ввода: {e}. Попробуй ещё раз с правильным форматом.")
        return EDIT_NEW_VALUE
    except Exception as e:
        await update.message.reply_text(f"❗️ Непредвиденная ошибка при сохранении: {e}")
        context.user_data.pop("edit_event_id", None)
        context.user_data.pop("edit_field", None)
        return ConversationHandler.END

# ---------------------------- ОБРАБОТЧИК ОШИБОК ---------------------------------
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Логирует ошибки и отправляет уведомление администраторам."""
    # Логирование полной трассировки стека
    print(f"Update: {update} caused error: {context.error}")
    trace = "".join(traceback.format_tb(context.error.__traceback__))
    
    # Сообщение для админа
    message = (
        f"🚨 **Критическая ошибка в боте!**\n\n"
        f"**Ошибка:** `{context.error}`\n"
        f"**Update:** `{update}`\n"
        f"**Трассировка:**\n`{trace}`"
    )

    # Уведомление администраторов
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=message,
                parse_mode="Markdown"
            )
        except (Forbidden, BadRequest):
            # Если не можем отправить админу, то просто логируем
            print(f"Не удалось уведомить администратора {admin_id} об ошибке.")
            
    # Дополнительно: если ошибка произошла в чате, сообщить пользователю (если это не CallbackQuery)
    if isinstance(update, Update) and update.effective_chat:
        try:
            if update.effective_chat.type in ["group", "supergroup", "private"]:
                await update.effective_chat.send_message("❌ Извините, произошла непредвиденная ошибка. Администратор был уведомлен.", parse_mode="Markdown")
        except Exception:
            pass

# ---------------------------- Запуск --------------------------------------

def main():
    # 📌 Обязательная проверка BOT_TOKEN
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise EnvironmentError("Переменная окружения 'BOT_TOKEN' не установлена.")

    persistence = PicklePersistence(filepath=DATA_FILE)
    
    # 📌 Использование ApplicationBuilder
    application = ApplicationBuilder().token(token).persistence(persistence).build()

    # ------------------ Хендлеры ------------------
    
    # 🚨 Добавляем хендлер ошибок
    application.add_error_handler(error_handler)
    
    # Комманды
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("create_event", create_event_command_quick))
    application.add_handler(CommandHandler("my_events", my_events_command))
    application.add_handler(CommandHandler("export_event", export_event_command))
    application.add_handler(CommandHandler("delete_event", delete_event_command))
    application.add_handler(CommandHandler("remove_participant", remove_participant_command))

    # Создание из фото с подписью
    application.add_handler(
        MessageHandler(filters.PHOTO & filters.Caption(), create_event_from_photo_message)
    )

    # Кнопки
    application.add_handler(CallbackQueryHandler(button_handler, pattern=r"^(join|leave|no_join)\|\d+$"))

    # Пошаговое создание
    create_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("create", create_start)],
        states={
            C_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_title)],
            C_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_date)],
            C_CAPACITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_capacity)],
            C_LOCATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_location)],
            C_DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_description)],
            C_PHOTO: [
                MessageHandler(filters.PHOTO | (filters.TEXT & filters.Regex(r"^(?i)skip$")), create_photo_step)
            ],
        },
        fallbacks=[CommandHandler("cancel", create_cancel)],
        allow_reentry=True,
        persistent=True, 
        name="create_event_conversation",
    )
    application.add_handler(create_conv_handler)
    
    # Пошаговое редактирование
    edit_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("edit_event", edit_event_command)],
        states={
            EDIT_SELECT_FIELD: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_select_field)],
            EDIT_NEW_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND | filters.PHOTO, edit_new_value)],
        },
        fallbacks=[CommandHandler("cancel", create_cancel)], 
        allow_reentry=True,
        persistent=True,
        name="edit_event_conversation",
    )
    application.add_handler(edit_conv_handler)


    # ------------------ Запуск ------------------
    PORT = int(os.environ.get("PORT", 8080))
    WEBHOOK_URL = os.environ.get("WEBHOOK_URL")

    if WEBHOOK_URL:
        # Webhook mode
        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=token,
            webhook_url=f"{WEBHOOK_URL}/{token}"
        )
        print(f"Бот запущен в режиме Webhook на порту {PORT}")
    else:
        # Polling mode (для локального запуска)
        application.run_polling(drop_pending_updates=True) # 📌 Добавил очистку очереди
        print("Бот запущен в режиме Polling")


if __name__ == "__main__":
    main()
    
