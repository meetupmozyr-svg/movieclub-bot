import os
import json
from datetime import datetime
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
    ConversationHandler,
)
from telegram.ext.persistence import PicklePersistence

# --- КОНСТАНТЫ СОСТОЯНИЙ ---
# Состояния для диалога создания события
(
    CHOOSING_TITLE, 
    CHOOSING_DATE, 
    CHOOSING_TIME, 
    CHOOSING_DESCRIPTION, 
    CONFIRM_EVENT
) = range(5)

# Состояния для диалога регистрации
WAITING_FOR_EVENT_ID, WAITING_FOR_NAME = range(5, 7)

# --- ИНИЦИАЛИЗАЦИЯ ПЕРЕМЕННЫХ ---
# Получение токена бота
BOT_TOKEN = os.environ.get("BOT_TOKEN")

# Настройки персистентности (для сохранения данных между перезапусками)
# Загрузка персистентности
persistence = PicklePersistence(filepath="movie_club_persistence.pkl")
print("Персистенс загружен успешно.")

# --- СТРУКТУРА ДАННЫХ ДЛЯ СОБЫТИЙ ---
# events = {
#     "event_id": {
#         "title": "...",
#         "date": "...",
#         "time": "...",
#         "description": "...",
#         "creator_id": 12345,
#         "registrations": {
#             12345678: "User Name 1",
#             98765432: "User Name 2",
#             ...
#         }
#     },
#     ...
# }
events = persistence.get_user_data().get("events", {})
if not events:
    # Инициализируем 'events' в user_data, если его там нет
    persistence.user_data["events"] = {}
    events = persistence.user_data["events"]

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---

def generate_event_id() -> str:
    """Генерирует уникальный ID события."""
    return str(len(events) + 1).zfill(4) # 0001, 0002, ...

def save_events_to_persistence(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Сохраняет текущее состояние словаря events в персистентность."""
    context.application.persistence.user_data["events"] = events
    context.application.persistence.flush()

def format_event_details(event_data: dict, event_id: str) -> str:
    """Форматирует данные события для вывода пользователю."""
    registrations = event_data.get("registrations", {})
    
    details = (
        f"🎬 **ID События:** `{event_id}`\n"
        f"🌟 **Название:** {event_data['title']}\n"
        f"📅 **Дата:** {event_data['date']}\n"
        f"⏰ **Время:** {event_data['time']}\n"
        f"📝 **Описание:** {event_data['description']}\n"
        f"\n"
        f"👤 **Зарегистрированы ({len(registrations)}):**\n"
    )
    
    if registrations:
        names = "\n".join([f"  • {name}" for name in registrations.values()])
        details += names
    else:
        details += "  _Пока никто не зарегистрировался._"

    return details

# --- ОБРАБОТЧИКИ ДЛЯ КОМАНД ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает команду /start."""
    welcome_message = (
        "Привет! Я бот для организации киноклуба.\n\n"
        "Доступные команды:\n"
        "• /create - Создать новое событие киноклуба (доступно всем).\n"
        "• /register - Зарегистрироваться на событие.\n"
        "• /events - Показать список всех активных событий.\n"
        "• /cancel - Отменить создание или регистрацию."
    )
    await update.message.reply_text(welcome_message)

async def show_events(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает команду /events и показывает список всех событий."""
    if not events:
        await update.message.reply_text("В данный момент нет активных событий. Создайте первое с помощью /create!")
        return
    
    response_text = "✨ **Список активных событий** ✨\n\n"
    
    for event_id, data in events.items():
        # Форматирование краткой информации о регистрации
        reg_count = len(data.get("registrations", {}))
        
        response_text += (
            f"**{data['title']}** (`{event_id}`)\n"
            f"  • Дата: {data['date']} в {data['time']}\n"
            f"  • Участников: {reg_count}\n"
            f"  • /details{event_id} - Подробнее и регистрация\n\n"
        )
        
    await update.message.reply_text(response_text, parse_mode="Markdown")

async def show_details(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Обрабатывает динамические команды типа /details0001
    Показывает детальную информацию о событии.
    """
    # Извлекаем ID события из команды (например, /details0001 -> 0001)
    event_id = update.message.text.split('/details')[1]

    if event_id not in events:
        await update.message.reply_text(f"Событие с ID `{event_id}` не найдено.", parse_mode="Markdown")
        return

    event_data = events[event_id]
    details = format_event_details(event_data, event_id)
    
    keyboard = [
        [f"Зарегистрироваться на {event_id}"],
        [f"Отменить регистрацию на {event_id}"]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)

    await update.message.reply_text(
        details, 
        parse_mode="Markdown", 
        reply_markup=reply_markup
    )

# --- ДИАЛОГ СОЗДАНИЯ СОБЫТИЯ (/create) ---

async def create_event(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Начало диалога создания события."""
    # ************************************************
    # *** УДАЛЕНА ПРОВЕРКА АДМИНИСТРАТОРА. ДОСТУПНО ВСЕМ. ***
    # ************************************************
    
    # Инициализация словаря для сбора данных события
    context.user_data['new_event'] = {
        'creator_id': update.effective_user.id
    }
    
    await update.message.reply_text(
        "🎬 **Начинаем создание события!**\n"
        "Введите **название** фильма или события (например, 'Просмотр фильма "
        "Интерстеллар').",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode="Markdown"
    )
    return CHOOSING_TITLE

async def receive_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Получает название события."""
    title = update.message.text
    context.user_data['new_event']['title'] = title
    
    await update.message.reply_text(
        f"Название: **{title}**.\n\n"
        "Теперь введите **дату** события (например, '01.12.2025' или 'завтра').",
        parse_mode="Markdown"
    )
    return CHOOSING_DATE

async def receive_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Получает дату события."""
    date_text = update.message.text
    context.user_data['new_event']['date'] = date_text
    
    await update.message.reply_text(
        f"Дата: **{date_text}**.\n\n"
        "Теперь введите **время** начала (например, '20:00' или 'семь вечера').",
        parse_mode="Markdown"
    )
    return CHOOSING_TIME

async def receive_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Получает время события."""
    time_text = update.message.text
    context.user_data['new_event']['time'] = time_text
    
    await update.message.reply_text(
        f"Время: **{time_text}**.\n\n"
        "Теперь введите **описание** события (например, 'Смотрим в формате 4K, "
        "запасаемся попкорном!').",
        parse_mode="Markdown"
    )
    return CHOOSING_DESCRIPTION

async def receive_description(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Получает описание события и просит подтвердить."""
    description = update.message.text
    context.user_data['new_event']['description'] = description
    
    # Формируем сообщение для подтверждения
    temp_id = generate_event_id() # Используем временный ID для предварительного просмотра
    event_data = context.user_data['new_event']
    
    confirmation_text = "✨ **Проверьте ваше новое событие** ✨\n\n" + \
                        format_event_details(event_data, temp_id)
    
    keyboard = [["Да, создать!", "Нет, отменить"]]
    reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    
    await update.message.reply_text(
        confirmation_text,
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )
    return CONFIRM_EVENT

async def confirm_event(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Подтверждает и сохраняет событие."""
    user_choice = update.message.text
    
    if user_choice == "Да, создать!":
        new_event_data = context.user_data['new_event']
        new_event_data['registrations'] = {} # Инициализируем пустой список регистрации
        
        event_id = generate_event_id()
        events[event_id] = new_event_data
        
        save_events_to_persistence(context) # Сохраняем в персистентность
        
        await update.message.reply_text(
            f"✅ **Событие создано!**\n"
            f"ID: `{event_id}`. Поделитесь им с друзьями для регистрации!\n\n"
            f"Используйте команду /details{event_id} для просмотра деталей.",
            reply_markup=ReplyKeyboardRemove(),
            parse_mode="Markdown"
        )
        # Очищаем данные диалога
        del context.user_data['new_event']
        return ConversationHandler.END
    
    elif user_choice == "Нет, отменить":
        await update.message.reply_text(
            "❌ **Создание отменено.**",
            reply_markup=ReplyKeyboardRemove()
        )
        del context.user_data['new_event']
        return ConversationHandler.END
    
    else:
        # Если пользователь ввел что-то другое
        keyboard = [["Да, создать!", "Нет, отменить"]]
        reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
        await update.message.reply_text(
            "Пожалуйста, используйте кнопки 'Да, создать!' или 'Нет, отменить'.",
            reply_markup=reply_markup
        )
        return CONFIRM_EVENT # Остаемся в текущем состоянии

# --- ДИАЛОГ РЕГИСТРАЦИИ (/register) ---

async def register_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Начало диалога регистрации."""
    
    if not events:
        await update.message.reply_text("В данный момент нет активных событий для регистрации. Создайте первое с помощью /create!")
        return ConversationHandler.END

    event_ids = list(events.keys())
    # Формируем клавиатуру с доступными ID
    keyboard = [[event_id] for event_id in event_ids]
    reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)

    await update.message.reply_text(
        "📝 **Начинаем регистрацию!**\n"
        "Введите **ID** события, на которое хотите зарегистрироваться:",
        reply_markup=reply_markup
    )
    return WAITING_FOR_EVENT_ID

async def receive_event_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Получает ID события для регистрации."""
    event_id = update.message.text.strip().upper().zfill(4) # Чистим и приводим к формату ID
    
    if event_id not in events:
        keyboard = [[eid] for eid in events.keys()]
        reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
        
        await update.message.reply_text(
            f"❌ Событие с ID `{event_id}` не найдено. Пожалуйста, выберите ID из списка или введите корректный ID:",
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )
        return WAITING_FOR_EVENT_ID
        
    context.user_data['registration_target_id'] = event_id
    
    # Проверка, не зарегистрирован ли уже пользователь
    user_id = update.effective_user.id
    if user_id in events[event_id].get('registrations', {}):
        keyboard = [["Да, отменить регистрацию"]]
        reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
        
        await update.message.reply_text(
            f"Вы уже зарегистрированы на событие **{events[event_id]['title']}**!\n"
            "Хотите отменить регистрацию?",
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )
        # Используем существующее состояние регистрации для обработки отмены
        return WAITING_FOR_NAME 
    
    await update.message.reply_text(
        f"Вы выбрали событие **{events[event_id]['title']}**.\n\n"
        "Введите **имя**, под которым вы хотите быть зарегистрированы "
        "(например, 'Алексей' или 'Мария Р.'):",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode="Markdown"
    )
    return WAITING_FOR_NAME

async def receive_name_and_register(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Получает имя и регистрирует пользователя."""
    user_id = update.effective_user.id
    event_id = context.user_data.get('registration_target_id')
    user_name = update.message.text.strip()
    
    # Обработка отмены регистрации, если пользователь нажал кнопку
    if user_name == "Да, отменить регистрацию":
        if user_id in events[event_id]['registrations']:
            del events[event_id]['registrations'][user_id]
            save_events_to_persistence(context)
            await update.message.reply_text(
                f"✅ **Регистрация отменена** на событие `{event_id}`: **{events[event_id]['title']}**.",
                reply_markup=ReplyKeyboardRemove(),
                parse_mode="Markdown"
            )
        else:
             await update.message.reply_text(
                "Вы не были зарегистрированы на это событие. Отмена не требуется.",
                reply_markup=ReplyKeyboardRemove()
            )
        del context.user_data['registration_target_id']
        return ConversationHandler.END

    # Основная логика регистрации
    if not user_name:
        await update.message.reply_text(
            "Имя не может быть пустым. Пожалуйста, введите ваше имя:"
        )
        return WAITING_FOR_NAME

    # Если registrations еще нет, создаем его
    if 'registrations' not in events[event_id]:
        events[event_id]['registrations'] = {}
        
    events[event_id]['registrations'][user_id] = user_name
    
    save_events_to_persistence(context)
    
    await update.message.reply_text(
        f"🎉 **Готово!** Вы зарегистрированы как **{user_name}** на событие **{events[event_id]['title']}**.",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode="Markdown"
    )
    # Очищаем данные диалога
    del context.user_data['registration_target_id']
    return ConversationHandler.END

# --- ОТМЕНА ДИАЛОГОВ ---

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Отменяет текущий диалог и сбрасывает user_data."""
    # Очищаем данные, связанные с созданием события
    if 'new_event' in context.user_data:
        del context.user_data['new_event']
    
    # Очищаем данные, связанные с регистрацией
    if 'registration_target_id' in context.user_data:
        del context.user_data['registration_target_id']

    await update.message.reply_text(
        "🚫 **Операция отменена.**", 
        reply_markup=ReplyKeyboardRemove()
    )
    return ConversationHandler.END

# --- ОСНОВНАЯ ФУНКЦИЯ ЗАПУСКА ---

def main() -> None:
    """Запуск бота."""
    if not BOT_TOKEN:
        print("Ошибка: BOT_TOKEN не установлен в переменных окружения.")
        return

    # Создание экземпляра Application с персистентностью
    application = Application.builder().token(BOT_TOKEN).persistence(persistence).build()
    
    # --- ОБРАБОТЧИКИ КОМАНД ---
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("events", show_events))
    
    # Динамический обработчик для /detailsXXXX
    application.add_handler(MessageHandler(
        filters.Regex(r'^/details[0-9]{4}$'), show_details
    ))
    
    # --- ДИАЛОГ СОЗДАНИЯ СОБЫТИЯ ---
    create_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("create", create_event)],
        states={
            CHOOSING_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_title)],
            CHOOSING_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_date)],
            CHOOSING_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_time)],
            CHOOSING_DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_description)],
            CONFIRM_EVENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, confirm_event)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        persistent=True,
        name="create_event_conversation"
    )
    application.add_handler(create_conv_handler)

    # --- ДИАЛОГ РЕГИСТРАЦИИ ---
    register_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("register", register_start)],
        states={
            WAITING_FOR_EVENT_ID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_event_id),
            ],
            WAITING_FOR_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_name_and_register)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        persistent=True,
        name="registration_conversation"
    )
    application.add_handler(register_conv_handler)
    
    print("Бот настроен. Запуск в режиме Webhook...")
    
    # Запуск в режиме Webhook (для Render)
    # Переменные PORT и WEBHOOK_URL устанавливаются автоматически в Render
    PORT = int(os.environ.get("PORT", 8080))
    WEBHOOK_URL = os.environ.get("WEBHOOK_URL")

    if WEBHOOK_URL:
        # Устанавливаем и запускаем Webhook
        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=BOT_TOKEN,
            webhook_url=WEBHOOK_URL + BOT_TOKEN
        )
    else:
        # Если WEBHOOK_URL не установлен (локальный запуск), используем опрос
        print("WEBHOOK_URL не установлен. Запуск в режиме Long Polling (для локального тестирования).")
        application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
