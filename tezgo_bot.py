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
    order["status"] = "pending"
    order["driver_id"] = None
    order["driver_name"] = None
    if not DRIVERS_CHAT_ID:
        logger.info("Order %s created (no DRIVERS_CHAT_ID set; not forwarded).", order_id)
        return

    context.bot_data.setdefault("orders", {})[order_id] = order
    try:
        await forward_order_to_group(context, order)
    except Exception as exc:  # noqa: BLE001 — log and continue; don't crash the bot.
        logger.exception("Failed to forward order %s to drivers chat: %s", order_id, exc)


# --------------------------------------------------------------------------- #
# Order lifecycle: accept -> enroute -> arrived -> completed (+ cancel, rating)
# --------------------------------------------------------------------------- #

STATUS_LABELS = {
    "pending": "🟡 Kutilmoqda",
    "accepted": "🟢 Qabul qilindi",
    "enroute": "🚗 Yo'lda",
    "arrived": "📍 Yetib keldi",
    "completed": "✅ Yakunlandi",
    "cancelled": "❌ Bekor qilindi",
}


def render_order_text(order: dict) -> str:
    """Render the drivers'-group message body for the current order state."""
    class_label = CAR_CLASSES.get(order["class"], order["class"])
    lines = [
        f"🆕 <b>TezGo buyurtma</b> — <code>{order['order_id']}</code>",
        f"Holat: <b>{STATUS_LABELS.get(order.get('status', 'pending'))}</b>",
        "",
        f"👤 Mijoz: {order['customer_name']}",
        f"📍 <b>Qayerdan:</b> {order['pickup']['address']}",
        f"   <a href=\"{maps_link(order['pickup']['lat'], order['pickup']['lon'])}\">Xaritada ochish</a>",
        f"🏁 <b>Qayerga:</b> {order['destination']['address']}",
        f"   <a href=\"{maps_link(order['destination']['lat'], order['destination']['lon'])}\">Xaritada ochish</a>",
        f"🚘 Sinf: {class_label}",
        f"📏 {order['distance_km']:.1f} km · ⏱ ~{int(round(order['duration_min']))} daq",
        f"💰 Narx: {format_som(order['fare_som'])} so'm",
    ]
    if order.get("driver_name"):
        lines.append(f"\n🧑‍✈️ Haydovchi: <b>{order['driver_name']}</b>")
    return "\n".join(lines)


def order_markup(order: dict):
    """Inline buttons that match the current status (driver-side controls)."""
    st = order.get("status", "pending")
    oid = order["order_id"]
    if st == "pending":
        rows = [[InlineKeyboardButton("✅ Qabul qilish", callback_data=f"accept:{oid}")]]
    elif st == "accepted":
        rows = [[InlineKeyboardButton("🚗 Yo'ldaman", callback_data=f"stage:{oid}:enroute")],
                [InlineKeyboardButton("❌ Bekor qilish", callback_data=f"cancel:{oid}")]]
    elif st == "enroute":
        rows = [[InlineKeyboardButton("📍 Yetib keldim", callback_data=f"stage:{oid}:arrived")],
                [InlineKeyboardButton("❌ Bekor qilish", callback_data=f"cancel:{oid}")]]
    elif st == "arrived":
        rows = [[InlineKeyboardButton("✅ Yakunladim", callback_data=f"stage:{oid}:completed")],
                [InlineKeyboardButton("❌ Bekor qilish", callback_data=f"cancel:{oid}")]]
    else:
        return None
    return InlineKeyboardMarkup(rows)


async def forward_order_to_group(context: ContextTypes.DEFAULT_TYPE, order: dict) -> None:
    """Post (or re-post) an order to the drivers' group and remember its message."""
    msg = await context.bot.send_message(
        chat_id=DRIVERS_CHAT_ID,
        text=render_order_text(order),
        parse_mode=ParseMode.HTML,
        reply_markup=order_markup(order),
        disable_web_page_preview=True,
    )
    order["group_chat_id"] = msg.chat_id
    order["group_message_id"] = msg.message_id
    try:
        await context.bot.send_location(chat_id=DRIVERS_CHAT_ID,
                                        latitude=order["pickup"]["lat"], longitude=order["pickup"]["lon"])
        await context.bot.send_location(chat_id=DRIVERS_CHAT_ID,
                                        latitude=order["destination"]["lat"], longitude=order["destination"]["lon"])
    except Exception:  # noqa: BLE001
        pass


async def refresh_group_message(context: ContextTypes.DEFAULT_TYPE, order: dict) -> None:
    """Edit the group message in place to reflect the current status/buttons."""
    try:
        await context.bot.edit_message_text(
            chat_id=order.get("group_chat_id"),
            message_id=order.get("group_message_id"),
            text=render_order_text(order),
            parse_mode=ParseMode.HTML,
            reply_markup=order_markup(order),
            disable_web_page_preview=True,
        )
    except Exception:  # noqa: BLE001
        pass


async def notify_customer(context: ContextTypes.DEFAULT_TYPE, order: dict, text: str) -> None:
    cid = order.get("customer_id")
    if not cid:
        return
    try:
        await context.bot.send_message(chat_id=cid, text=text, parse_mode=ParseMode.HTML)
    except Exception as exc:  # noqa: BLE001
        logger.warning("notify_customer failed for %s: %s", cid, exc)


def _driver_stats(context, driver) -> dict:
    stats = context.bot_data.setdefault("driver_stats", {}).setdefault(
        driver.id, {"name": "", "accepted": 0, "completed": 0, "cancelled": 0, "ratings": []}
    )
    stats["name"] = ("@" + driver.username) if driver.username else driver.full_name
    return stats


async def accept_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A driver taps 'Qabul qilish'. First-come locks the order to that driver."""
    query = update.callback_query
    try:
        _, order_id = query.data.split(":", 1)
    except ValueError:
        await query.answer()
        return
    order = context.bot_data.get("orders", {}).get(order_id)
    if not order:
        await query.answer("Buyurtma topilmadi yoki eskirgan.", show_alert=True)
        return
    if order.get("status") != "pending" or order.get("driver_id"):
        await query.answer(f"Allaqachon qabul qilingan ({order.get('driver_name') or '—'}).", show_alert=True)
        return

    driver = query.from_user
    order["status"] = "accepted"
    order["driver_id"] = driver.id
    order["driver_name"] = ("@" + driver.username) if driver.username else driver.full_name
    stats = _driver_stats(context, driver)
    stats["accepted"] += 1

    await query.answer("Buyurtma sizga biriktirildi ✅")
    await refresh_group_message(context, order)
    await notify_customer(
        context, order,
        f"🟢 Buyurtmangiz <code>{order_id}</code> qabul qilindi!\n"
        f"Haydovchi: <b>{order['driver_name']}</b> tez orada bog'lanadi.",
    )


async def stage_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The assigned driver advances the ride: enroute -> arrived -> completed."""
    query = update.callback_query
    try:
        _, order_id, stage = query.data.split(":", 2)
    except ValueError:
        await query.answer()
        return
    order = context.bot_data.get("orders", {}).get(order_id)
    if not order:
        await query.answer("Buyurtma topilmadi.", show_alert=True)
        return
    driver = query.from_user
    if order.get("driver_id") != driver.id:
        await query.answer("Bu buyurtma sizga tegishli emas.", show_alert=True)
        return

    order["status"] = stage
    await query.answer("Yangilandi ✅")
    await refresh_group_message(context, order)

    if stage == "enroute":
        await notify_customer(context, order, f"🚗 Haydovchi <b>{order['driver_name']}</b> yo'lda — sizga kelmoqda.")
    elif stage == "arrived":
        await notify_customer(context, order, f"📍 Haydovchi <b>{order['driver_name']}</b> yetib keldi. Iltimos, chiqing.")
    elif stage == "completed":
        stats = context.bot_data.get("driver_stats", {}).get(driver.id)
        if stats:
            stats["completed"] += 1
        cid = order.get("customer_id")
        if cid:
            rate_rows = [[InlineKeyboardButton("⭐" * n + f"  {n}", callback_data=f"rate:{order_id}:{n}")]
                         for n in range(5, 0, -1)]
            try:
                await context.bot.send_message(
                    chat_id=cid,
                    text=(f"✅ Safar yakunlandi. Rahmat!\n"
                          f"Haydovchi <b>{order['driver_name']}</b>ni baholang:"),
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup(rate_rows),
                )
            except Exception:  # noqa: BLE001
                pass


async def cancel_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The assigned driver cancels — the order is re-opened to the group."""
    query = update.callback_query
    try:
        _, order_id = query.data.split(":", 1)
    except ValueError:
        await query.answer()
        return
    order = context.bot_data.get("orders", {}).get(order_id)
    if not order:
        await query.answer("Buyurtma topilmadi.", show_alert=True)
        return
    driver = query.from_user
    if order.get("driver_id") != driver.id:
        await query.answer("Bu buyurtma sizga tegishli emas.", show_alert=True)
        return

    await query.answer("Buyurtma bekor qilindi.")
    stats = context.bot_data.get("driver_stats", {}).get(driver.id)
    if stats:
        stats["cancelled"] += 1

    order["status"] = "cancelled"
    await refresh_group_message(context, order)
    await notify_customer(
        context, order,
        f"⚠️ Buyurtmangiz <code>{order_id}</code> haydovchi tomonidan bekor qilindi. "
        "Yangi haydovchi qidirilmoqda…",
    )
    # Re-open as a fresh pending order (new message in the group).
    order["status"] = "pending"
    order["driver_id"] = None
    order["driver_name"] = None
    try:
        await forward_order_to_group(context, order)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Re-forward failed for %s: %s", order_id, exc)


async def rate_driver(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Customer rates the driver 1–5 after completion."""
    query = update.callback_query
    try:
        _, order_id, n = query.data.split(":", 2)
        n = int(n)
    except ValueError:
        await query.answer()
        return
    order = context.bot_data.get("orders", {}).get(order_id)
    if not order:
        await query.answer("Baholash muddati o'tgan.", show_alert=True)
        return
    if query.from_user.id != order.get("customer_id"):
        await query.answer("Faqat mijoz baholaydi.", show_alert=True)
        return

    order["rating"] = n
    stats = context.bot_data.get("driver_stats", {}).get(order.get("driver_id"))
    if stats:
        stats["ratings"].append(n)
    await query.answer("Rahmat! Bahoyingiz qabul qilindi.")
    try:
        await query.edit_message_text(f"⭐ Bahoyingiz: {'⭐' * n} ({n}/5)\nRahmat, fikringiz uchun!")
    except Exception:  # noqa: BLE001
        pass
    if DRIVERS_CHAT_ID:
        avg = (sum(stats["ratings"]) / len(stats["ratings"])) if stats and stats["ratings"] else n
        try:
            await context.bot.send_message(
                chat_id=DRIVERS_CHAT_ID,
                text=(f"⭐ <code>{order_id}</code> — {order.get('driver_name') or ''} "
                      f"baholandi: {n}/5 (o'rtacha {avg:.1f})"),
                parse_mode=ParseMode.HTML,
            )
        except Exception:  # noqa: BLE001
            pass


async def drivers_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/drivers — show a simple leaderboard of driver activity and ratings."""
    stats = context.bot_data.get("driver_stats", {})
    if not stats:
        await update.message.reply_text("Hozircha haydovchi statistikasi yo'q.")
        return
    lines = ["<b>🏁 Haydovchilar statistikasi</b>", ""]
    for _did, s in sorted(stats.items(), key=lambda kv: kv[1]["completed"], reverse=True):
        r = s["ratings"]
        avg = f"{sum(r) / len(r):.1f}⭐" if r else "—"
        lines.append(
            f"{s['name']}: ✅ {s['completed']} · qabul {s['accepted']} · bekor {s['cancelled']} · {avg}"
        )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


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
    application.add_handler(CommandHandler("drivers", drivers_stats))

    # Web App data arrives as a special message; match it first.
    application.add_handler(
        MessageHandler(filters.StatusUpdate.WEB_APP_DATA, web_app_data)
    )

    # Order lifecycle callbacks.
    application.add_handler(CallbackQueryHandler(accept_order, pattern=r"^accept:"))
    application.add_handler(CallbackQueryHandler(stage_order, pattern=r"^stage:"))
    application.add_handler(CallbackQueryHandler(cancel_order, pattern=r"^cancel:"))
    application.add_handler(CallbackQueryHandler(rate_driver, pattern=r"^rate:"))

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
