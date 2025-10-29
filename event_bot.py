import os
from typing import Dict
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    PicklePersistence,
)
from telegram.error import BadRequest, Forbidden

# The file where all bot data will be persistently stored
DATA_FILE = "bot_persistence.pickle"


def make_event_keyboard(event_id: str, event: Dict):
    """Creates the keyboard with Join/Leave buttons for an event."""
    joined = event["joined"]
    waitlist = event["waitlist"]
    capacity = event["capacity"]
    spots_filled = len(joined)

    # The "Join" button's text changes based on capacity
    if spots_filled >= capacity:
        join_text = f"🕒 Join Waitlist ({len(waitlist)})"
    else:
        join_text = f"✅ Join ({spots_filled}/{capacity})"

    kb = [
        [
            InlineKeyboardButton(join_text, callback_data=f"join|{event_id}"),
            InlineKeyboardButton("❌ Can't come", callback_data=f"leave|{event_id}")
        ],
    ]
    return InlineKeyboardMarkup(kb)


def format_event_message(event: Dict):
    """Formats the event details into a nice message."""
    return (
        f"🎬 <b>{event['title']}</b>\n"
        f"📅 {event['date']}\n"
        f"📍 {event.get('location','(no location)')}\n\n"
        f"{event.get('description','(no description)')}\n\n"
        f"👥 <b>{len(event['joined'])}/{event['capacity']}</b> spots filled\n"
        f"🕒 Waitlist: {len(event['waitlist'])}"
    )


async def create_event_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Creates a new event and posts it to the channel."""
    user = update.effective_user
    if not user:
        return
    if not context.args:
        await update.message.reply_text(
            "Usage:\n/create_event Title | 2025-11-02 20:00 | capacity | location | description"
        )
        return

    raw = " ".join(context.args)
    parts = [p.strip() for p in raw.split("|")]
    if len(parts) < 3:
        await update.message.reply_text("Please provide at least Title | Date | Capacity")
        return

    try:
        title, date = parts[0], parts[1]
        capacity = int(parts[2])
    except (ValueError, IndexError):
        await update.message.reply_text("Invalid format. Check capacity (must be a number) and try again.")
        return

    location = parts[3] if len(parts) > 3 else ""
    description = parts[4] if len(parts) > 4 else ""

    # Get a new event ID. Uses bot_data for safe, persistent storage.
    event_id = str(max([int(k) for k in context.bot_data['events'].keys()] + [0]) + 1)

    event = {
        "id": event_id,
        "title": title,
        "date": date,
        "capacity": capacity,
        "location": location,
        "description": description,
        "creator_id": user.id,
        "message_id": None,  # Will be filled after sending
        "channel": os.environ.get("CHANNEL", "@kinovinomoz"),
        "joined": [],
        "waitlist": []
    }

    # Send the message first to get its ID
    app = context.application
    text = format_event_message(event)
    keyboard = make_event_keyboard(event_id, event)
    
    try:
        sent = await app.bot.send_message(
            chat_id=event["channel"], 
            text=text, 
            reply_markup=keyboard, 
            parse_mode="HTML"
        )
    except Exception as e:
        await update.message.reply_text(f"Error sending message to channel: {e}")
        return

    # Now add the message_id to the event and save it *once*
    event["message_id"] = sent.message_id
    context.bot_data["events"][event_id] = event

    await update.message.reply_text(f"Event created and posted to {event['channel']} (ID {event_id}).")


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles all button presses for joining, leaving, and waitlisting."""
    query = update.callback_query
    await query.answer()

    # Use bot_data as the single source of truth
    data = context.bot_data
    
    try:action, event_id = query.data.split("|")
    except (ValueError, AttributeError):
        await query.edit_message_text("Invalid callback data.")
        return

    event = data["events"].get(event_id)

    if not event:
        await query.edit_message_text("Event not found or expired.")
        return

    user_id = query.from_user.id
    user_name = query.from_user.full_name  # Good to have for notifications

    # First, always remove user from all lists to handle any state
    if user_id in event["joined"]:
        event["joined"].remove(user_id)
    if user_id in event["waitlist"]:
        event["waitlist"].remove(user_id)

    # Then, add them to the correct list based on action
    if action == "join":
        if len(event["joined"]) < event["capacity"]:
            event["joined"].append(user_id)
            response = "You are registered ✅"
        else:
            event["waitlist"].append(user_id)
            response = "Event is full — you were added to the waitlist 🕒"
    elif action == "leave":
        response = "You are marked as not coming ❌"
    elif action == "wait":  # This is still here for legacy, but "join" handles it
        event["waitlist"].append(user_id)
        response = "You were added to the waitlist 🕒"
    else:
        response = "Unknown action."

    # --- Waitlist Promotion Logic ---
    # While there are spots and people on the waitlist, promote them
    while len(event["joined"]) < event["capacity"] and event["waitlist"]:
        promoted_user_id = event["waitlist"].pop(0)
        if promoted_user_id not in event["joined"]:
            event["joined"].append(promoted_user_id)
            try:
                # Notify the promoted user
                await context.bot.send_message(
                    chat_id=promoted_user_id,
                    text=f"Good news! A spot opened up.\n\nYou were moved from the waitlist to confirmed for:\n{event['title']} on {event['date']} ✅",
                    parse_mode="Markdown"
                )
            except (BadRequest, Forbidden) as e:
                print(f"Failed to notify user {promoted_user_id} of promotion: {e}")
            except Exception as e:
                print(f"Unexpected error notifying user {promoted_user_id}: {e}")
    # --------------------------------

    # Persist changes (this is automatic with PicklePersistence,
    # but explicitly setting it doesn't hurt and ensures state)
    data["events"][event_id] = event

    # Update the original message
    try:
        await context.bot.edit_message_text(
            chat_id=event["channel"],
            message_id=event["message_id"],
            text=format_event_message(event),
            reply_markup=make_event_keyboard(event_id, event),
            parse_mode="HTML",
        )
    except (BadRequest, Forbidden) as e:
        # Common errors: message not modified, or bot can't edit
        print(f"Could not edit event message {event_id}: {e}")
    except Exception as e:
        print(f"Unexpected error editing message {event_id}: {e}")

    # Send a confirmation to the user who clicked
    try:
        # Try to send a private message (fails if user hasn't started bot)
        await query.from_user.send_message(response)
    except (BadRequest, Forbidden):
        # Fallback to an alert on their screen
        await query.answer(text=response, show_alert=True)
    except Exception as e:
        print(f"Unexpected error sending response to user {user_id}: {e}")


async def my_events_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows the user all events they are registered for."""
    user_id = update.effective_user.id
    
    # Access data safely from context
    data = context.bot_data
    out = []
    
    # Note: This iterates all events. Can be slow with 10k+ events.
    for e in data["events"].values():
        if user_id in e["joined"]:
            out.append(f"✅ Joined: {e['title']} — {e['date']}")
        elif user_id in e["waitlist"]:
            out.append(f"🕒 Waitlist: {e['title']} — {e['date']}")if not out:
        await update.message.reply_text("You have no upcoming RSVPs.")
    else:
        await update.message.reply_text("<b>Your RSVPs:</b>\n\n" + "\n".join(out), parse_mode="HTML")


def main():
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise ValueError("Please set the BOT_TOKEN environment variable.")

    # Set up persistence
    persistence = PicklePersistence(filepath=DATA_FILE)

    app = ApplicationBuilder().token(token).persistence(persistence).build()

    # Initialize bot_data["events"] if this is the first run
    if "events" not in app.bot_data:
        app.bot_data["events"] = {}

    # Add handlers
    app.add_handler(CommandHandler("create_event", create_event_command))
    app.add_handler(CommandHandler("my_events", my_events_command))
    app.add_handler(CallbackQueryHandler(button_handler))

    print("Bot started (polling). Press Ctrl-C to stop.")
    app.run_polling()


if name == "__main__":
    main()
