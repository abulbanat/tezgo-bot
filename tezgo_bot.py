#!/usr/bin/env python3
"""
TezGo — Telegram taxi-ordering bot.

A production-reasonable backend for a Telegram Mini App (Web App) that lets a
customer build a ride order inside a WebView and submit it back to the bot.

Flow overview
-------------
1. User sends /start -> bot shows a reply keyboard with a WebApp button.
2. User taps "🚕 Order a ride" -> Telegram opens the Mini App (WEBAPP_URL).
3. Inside the Mini App the user picks pickup/destination/car class, and the
   page calls `Telegram.WebApp.sendData(JSON.stringify(order))`.
4. Telegram delivers that JSON to this bot as `message.web_app_data.data`.
5. The bot validates the payload, confirms the order to the customer, and
   (optionally) forwards it to a drivers' group with an "Accept" button.

Requires python-telegram-bot v20+ (async API).

Configuration (environment variables)
-------------------------------------
BOT_TOKEN        (required)  Token from @BotFather.
WEBAPP_URL       (required)  HTTPS URL where the Mini App page is hosted.
DRIVERS_CHAT_ID  (optional)  Group/channel id to forward orders to (e.g. -1001234567890).
"""

import json
import logging
import os
import random
import string
from datetime import datetime, timezone

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
    WebAppInfo,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# --------------------------------------------------------------------------- #
# Configuration & logging
# --------------------------------------------------------------------------- #

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
WEBAPP_URL = os.environ.get("WEBAPP_URL", "https://corestacklabs.uz/tezgo-webapp.html").strip()
DRIVERS_CHAT_ID = os.environ.get("DRIVERS_CHAT_ID", "").strip()

# Human-readable labels for the car classes defined in the payload contract.
CAR_CLASSES = {
    "economy": "Economy",
    "comfort": "Comfort",
    "business": "Business",
}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("tezgo")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def generate_order_id() -> str:
    """Generate a short, human-friendly order id, e.g. 'TG-8F3K2Q'."""
    suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
    return f"TG-{suffix}"


def format_som(amount) -> str:
    """Format an integer amount of so'm with thousands separators."""
    try:
        return f"{int(round(float(amount))):,}".replace(",", " ")
    except (TypeError, ValueError):
        return str(amount)


def maps_link(lat, lon) -> str:
    """Build a Google Maps link for a coordinate pair."""
    return f"https://maps.google.com/?q={lat},{lon}"


def validate_order(data: dict) -> dict:
    """
    Validate and normalize the order payload sent by the Mini App.

    Raises ValueError with a human-readable message if anything is wrong.
    Returns a cleaned dict with guaranteed types.
    """
    if not isinstance(data, dict):
        raise ValueError("Payload is not a JSON object.")

    # Version check — the Mini App must speak the same contract.
    if data.get("v") != 1:
        raise ValueError("Unsupported payload version.")

    def parse_point(name: str) -> dict:
        point = data.get(name)
        if not isinstance(point, dict):
            raise ValueError(f"Missing '{name}' object.")
        try:
            lat = float(point["lat"])
            lon = float(point["lon"])
        except (KeyError, TypeError, ValueError):
            raise ValueError(f"Invalid coordinates for '{name}'.")
        # Basic sanity range check for latitude/longitude.
        if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
            raise ValueError(f"Coordinates for '{name}' are out of range.")
        address = str(point.get("address") or "").strip() or "Unknown location"
        return {"lat": lat, "lon": lon, "address": address}

    pickup = parse_point("pickup")
    destination = parse_point("destination")

    car_class = str(data.get("class", "")).lower().strip()
    if car_class not in CAR_CLASSES:
        raise ValueError("Unknown car class.")

    try:
        distance_km = float(data["distance_km"])
        duration_min = float(data["duration_min"])
        fare_som = float(data["fare_som"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("Missing or invalid trip metrics (distance/duration/fare).")

    if distance_km < 0 or duration_min < 0 or fare_som < 0:
        raise ValueError("Trip metrics cannot be negative.")

    return {
        "pickup": pickup,
        "destination": destination,
        "class": car_class,
        "distance_km": distance_km,
        "duration_min": duration_min,
        "fare_som": fare_som,
    }


def main_keyboard() -> ReplyKeyboardMarkup:
    """
    Build the reply keyboard that carries the WebApp button.

    NOTE: `web_app_data` is only delivered when the Web App is opened from a
    KeyboardButton on a ReplyKeyboardMarkup (not from an inline button), so we
    use a reply keyboard here on purpose.
    """
    button = KeyboardButton(text="🚕 Order a ride", web_app=WebAppInfo(url=WEBAPP_URL))
    return ReplyKeyboardMarkup(
        [[button]],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Tap 🚕 Order a ride to begin",
    )


# --------------------------------------------------------------------------- #
# Command handlers
# --------------------------------------------------------------------------- #

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start — greet the user and show the WebApp button."""
    user = update.effective_user
    name = user.first_name if user else "there"
    text = (
        f"👋 Hi {name}, welcome to <b>TezGo</b>!\n"
        f"🚕 Salom! TezGo orqali tez va oson taksi buyurtma qiling.\n\n"
        "Tap the button below to open the app, choose your pickup and "
        "destination, and confirm your ride."
    )
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=main_keyboard(),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/help — explain how to use the bot."""
    text = (
        "<b>TezGo — how it works</b>\n\n"
        "1. Send /start to show the <b>🚕 Order a ride</b> button.\n"
        "2. Tap it to open the TezGo app.\n"
        "3. Pick your pickup point, destination and car class "
        "(Economy / Comfort / Business).\n"
        "4. Confirm — you'll get an order summary with the fare and an order id.\n"
        "5. A nearby driver accepts and comes to pick you up.\n\n"
        "Commands:\n"
        "/start — open the app\n"
        "/help — show this help"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def id_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/id — reply with this chat's numeric id (used to configure DRIVERS_CHAT_ID)."""
    chat = update.effective_chat
    kind = chat.type if chat else "unknown"
    await update.message.reply_text(
        "🆔 <b>Chat ID</b>\n"
        f"<code>{chat.id}</code>\n\n"
        f"Type: {kind}\n"
        "Set this value as <b>DRIVERS_CHAT_ID</b> in Railway to receive orders here.",
        parse_mode=ParseMode.HTML,
    )


# --------------------------------------------------------------------------- #
# Web App data handler
# --------------------------------------------------------------------------- #

async def web_app_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle the JSON payload sent by the Mini App via Telegram.WebApp.sendData().

    Steps:
      - parse & validate the JSON (graceful errors on malformed input)
      - confirm the order to the customer
      - if DRIVERS_CHAT_ID is configured, forward the order to the drivers'
        group with pickup/destination locations and an "Accept" button.
    """
    message = update.effective_message
    raw = message.web_app_data.data

    # 1. Parse JSON — fail gracefully if malformed.
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.warning("Malformed web_app_data received: %r", raw)
        await message.reply_text(
            "⚠️ Sorry, we couldn't read your order. Please try again from the app.",
            reply_markup=main_keyboard(),
        )
        return

    # 2. Validate the contract.
    try:
        order = validate_order(payload)
    except ValueError as exc:
        logger.warning("Invalid order payload: %s | raw=%r", exc, raw)
        await message.reply_text(
            f"⚠️ Your order looks incomplete ({exc}). Please try again.",
            reply_markup=main_keyboard(),
        )
        return

    # 3. Build the order record.
    order_id = generate_order_id()
    user = update.effective_user
    order["order_id"] = order_id
    order["customer_id"] = user.id if user else None
    order["customer_name"] = user.full_name if user else "Customer"
    order["created_at"] = datetime.now(timezone.utc).isoformat()

    class_label = CAR_CLASSES[order["class"]]

    # 4. Confirm to the customer.
    confirmation = (
        f"✅ <b>Order confirmed!</b>\n"
        f"Order ID: <code>{order_id}</code>\n\n"
        f"📍 <b>Pickup:</b> {order['pickup']['address']}\n"
        f"🏁 <b>Destination:</b> {order['destination']['address']}\n"
        f"🚘 <b>Car class:</b> {class_label}\n"
        f"📏 <b>Distance:</b> {order['distance_km']:.1f} km\n"
        f"⏱ <b>Duration:</b> ~{int(round(order['duration_min']))} min\n"
        f"💰 <b>Fare:</b> {format_som(order['fare_som'])} so'm\n\n"
        "We're finding a driver for you now…"
    )
    await message.reply_text(
        confirmation,
        parse_mode=ParseMode.HTML,
        reply_markup=main_keyboard(),
    )

    # 5. Forward to the drivers' group (if configured).
    if not DRIVERS_CHAT_ID:
        logger.info("Order %s created (no DRIVERS_CHAT_ID set; not forwarded).", order_id)
        return

    # Stash the order so the "Accept" callback can look it up later.
    context.bot_data.setdefault("orders", {})[order_id] = order

    driver_text = (
        f"🆕 <b>New TezGo order</b> — <code>{order_id}</code>\n\n"
        f"👤 Customer: {order['customer_name']}\n"
        f"📍 <b>Pickup:</b> {order['pickup']['address']}\n"
        f"   <a href=\"{maps_link(order['pickup']['lat'], order['pickup']['lon'])}\">Open in Maps</a>\n"
        f"🏁 <b>Destination:</b> {order['destination']['address']}\n"
        f"   <a href=\"{maps_link(order['destination']['lat'], order['destination']['lon'])}\">Open in Maps</a>\n"
        f"🚘 Class: {class_label}\n"
        f"📏 {order['distance_km']:.1f} km · ⏱ ~{int(round(order['duration_min']))} min\n"
        f"💰 Fare: {format_som(order['fare_som'])} so'm"
    )
    accept_markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton("✅ Accept", callback_data=f"accept:{order_id}")]]
    )

    try:
        # Text summary with the Accept button.
        await context.bot.send_message(
            chat_id=DRIVERS_CHAT_ID,
            text=driver_text,
            parse_mode=ParseMode.HTML,
            reply_markup=accept_markup,
            disable_web_page_preview=True,
        )
        # Pin the two points on the map as live location messages.
        await context.bot.send_location(
            chat_id=DRIVERS_CHAT_ID,
            latitude=order["pickup"]["lat"],
            longitude=order["pickup"]["lon"],
        )
        await context.bot.send_location(
            chat_id=DRIVERS_CHAT_ID,
            latitude=order["destination"]["lat"],
            longitude=order["destination"]["lon"],
        )
    except Exception as exc:  # noqa: BLE001 — log and continue; don't crash the bot.
        logger.exception("Failed to forward order %s to drivers chat: %s", order_id, exc)


# --------------------------------------------------------------------------- #
# Callback (driver accepts an order)
# --------------------------------------------------------------------------- #

async def accept_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle a driver tapping the 'Accept' button in the drivers' group."""
    query = update.callback_query
    await query.answer()

    try:
        _, order_id = query.data.split(":", 1)
    except ValueError:
        return

    orders = context.bot_data.get("orders", {})
    order = orders.get(order_id)

    driver = query.from_user
    driver_name = driver.full_name if driver else "A driver"

    # Update the drivers' group message so nobody else grabs the same order.
    try:
        await query.edit_message_text(
            text=f"{query.message.text_html}\n\n🚗 <b>Accepted by {driver_name}</b>",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except Exception:  # noqa: BLE001 — editing is best-effort.
        logger.info("Could not edit drivers' message for %s.", order_id)

    if not order:
        logger.info("Accept for unknown/expired order %s.", order_id)
        return

    # Simulate assignment: notify the customer that a driver is coming.
    customer_id = order.get("customer_id")
    if customer_id:
        try:
            await context.bot.send_message(
                chat_id=customer_id,
                text=(
                    f"🚗 Good news! A driver has accepted your order "
                    f"<code>{order_id}</code> and is on the way to your pickup point.\n"
                    "Rahmat! Haydovchingiz yo'lda."
                ),
                parse_mode=ParseMode.HTML,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not notify customer %s: %s", customer_id, exc)

    # Mark as assigned (kept simple for now).
    order["status"] = "assigned"
    order["driver_id"] = driver.id if driver else None


# --------------------------------------------------------------------------- #
# Fallback for plain text
# --------------------------------------------------------------------------- #

async def fallback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Nudge the user toward the WebApp button for any other message."""
    await update.message.reply_text(
        "Tap 🚕 <b>Order a ride</b> below to start, or send /help.",
        parse_mode=ParseMode.HTML,
        reply_markup=main_keyboard(),
    )


# --------------------------------------------------------------------------- #
# Application wiring
# --------------------------------------------------------------------------- #

def build_application() -> Application:
    """Create the Application and register all handlers."""
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("id", id_command))

    # Web App data arrives as a special message; match it first.
    application.add_handler(
        MessageHandler(filters.StatusUpdate.WEB_APP_DATA, web_app_data)
    )

    # Driver accepting an order.
    application.add_handler(CallbackQueryHandler(accept_order, pattern=r"^accept:"))

    # Any other plain text -> gentle nudge.
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, fallback)
    )

    return application


def main() -> None:
    """Entry point: start long polling."""
    logger.info("Starting TezGo bot… WEBAPP_URL=%s", WEBAPP_URL)
    if DRIVERS_CHAT_ID:
        logger.info("Orders will be forwarded to drivers chat %s", DRIVERS_CHAT_ID)
    else:
        logger.info("DRIVERS_CHAT_ID not set — orders won't be forwarded.")

    application = build_application()
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    if not BOT_TOKEN:
        raise SystemExit(
            "❌ BOT_TOKEN is not set.\n"
            "Get a token from @BotFather and export it, e.g.:\n"
            "    export BOT_TOKEN='123456:ABC-DEF...'\n"
            "    export WEBAPP_URL='https://corestacklabs.uz/tezgo-webapp.html'\n"
            "    export DRIVERS_CHAT_ID='-1001234567890'   # optional\n"
            "then run:  python tezgo_bot.py"
        )
    main()
