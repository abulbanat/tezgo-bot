#!/usr/bin/env python3
"""
TezGo — Driver bot (@TezGoDriver_bot).
Made by CoreStack Labs.

Handles driver registration (documents), daily selfie verification, and
online/offline status. Writes directly to the shared PostgreSQL database, so
new registrations appear in the Admin panel as pending "applications".

Registration collects:
  phone, full name, driver's licence (front/back), vehicle tech passport
  (front/back), taxi licence, car photos (front/back/left/right/interior),
  car make/colour/plate.

A driver may go Online only if:  status = 'approved'  AND  today's selfie is approved.

Env:
  DRIVER_BOT_TOKEN  (required)  BotFather token for the driver bot
  DATABASE_URL      (required)  same Postgres as the backend
"""

import logging
import os

import asyncpg
from telegram import (
    KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove, Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
    ConversationHandler, MessageHandler, filters,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tezgo-driver")

TOKEN = os.environ.get("DRIVER_BOT_TOKEN", "").strip()
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
BACKEND_URL = os.environ.get("BACKEND_URL", "").strip().rstrip("/")  # to accept orders

# Conversation states
(PHONE, NAME, LIC_F, LIC_B, TECH_F, TECH_B, TAXI, CAR_F, CAR_B, CAR_L, CAR_R,
 CAR_INT, MAKE, COLOR, PLATE, SELFIE) = range(16)

pool: asyncpg.Pool | None = None


# --------------------------------------------------------------------------- #
# DB helpers
# --------------------------------------------------------------------------- #
async def get_driver(telegram_id: int):
    async with pool.acquire() as con:
        return await con.fetchrow("SELECT * FROM drivers WHERE telegram_id=$1", telegram_id)


def phone_kb():
    return ReplyKeyboardMarkup(
        [[KeyboardButton("📱 Raqamni ulashish", request_contact=True)]],
        resize_keyboard=True, one_time_keyboard=True,
    )


def photo_of(update: Update) -> str | None:
    """Return the file_id of the largest photo in the message, or None."""
    if update.message and update.message.photo:
        return update.message.photo[-1].file_id
    return None


# --------------------------------------------------------------------------- #
# Registration flow
# --------------------------------------------------------------------------- #
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    drv = await get_driver(user.id)
    if drv:
        st = drv["status"]
        if st == "approved":
            await update.message.reply_text(
                "✅ Siz tasdiqlangan haydovchisiz.\n"
                "Har kuni buyurtma olishdan oldin /selfie yuboring.\n"
                "/online — buyurtma qabul qilishni yoqish · /offline — o'chirish",
            )
        elif st == "pending":
            await update.message.reply_text("⏳ Arizangiz ko'rib chiqilmoqda. Tasdiqlangach xabar beramiz.")
        elif st == "rejected":
            await update.message.reply_text(
                f"❌ Arizangiz rad etilgan. Sabab: {drv['reject_reason'] or 'ko`rsatilmagan'}\n"
                "Qayta ro'yxatdan o'tish uchun /reregister yuboring.")
        else:
            await update.message.reply_text("Hisobingiz bloklangan. Admin bilan bog'laning.")
        return ConversationHandler.END

    await update.message.reply_text(
        "🚕 <b>TezGo haydovchi</b> ro'yxatidan o'tish.\n\n"
        "Bir necha hujjat so'raymiz. Boshlash uchun telefon raqamingizni ulashing:",
        parse_mode=ParseMode.HTML, reply_markup=phone_kb(),
    )
    return PHONE


async def reregister(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with pool.acquire() as con:
        await con.execute("DELETE FROM drivers WHERE telegram_id=$1", update.effective_user.id)
    return await start(update, context)


async def got_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    c = update.message.contact
    if not c or (c.user_id and c.user_id != update.effective_user.id):
        await update.message.reply_text("Iltimos, o'z raqamingizni tugma orqali ulashing.", reply_markup=phone_kb())
        return PHONE
    context.user_data["phone"] = c.phone_number
    await update.message.reply_text("👤 To'liq F.I.O. ni yozing (pasport bo'yicha):",
                                    reply_markup=ReplyKeyboardRemove())
    return NAME


async def got_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["name"] = update.message.text.strip()
    await update.message.reply_text("🪪 Haydovchilik guvohnomasi — <b>old</b> tomoni fotosi:",
                                    parse_mode=ParseMode.HTML)
    return LIC_F


def _photo_step(key: str, nxt: int, prompt: str):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        fid = photo_of(update)
        if not fid:
            await update.message.reply_text("Iltimos, rasm (foto) yuboring.")
            return context.user_data.get("_state")
        context.user_data[key] = fid
        context.user_data["_state"] = nxt
        await update.message.reply_text(prompt, parse_mode=ParseMode.HTML)
        return nxt
    return handler


got_lic_f = _photo_step("license_front", LIC_B, "🪪 Guvohnoma — <b>orqa</b> tomoni fotosi:")
got_lic_b = _photo_step("license_back", TECH_F, "📄 Texpassport — <b>old</b> tomoni fotosi:")
got_tech_f = _photo_step("techpass_front", TECH_B, "📄 Texpassport — <b>orqa</b> tomoni fotosi:")
got_tech_b = _photo_step("techpass_back", TAXI, "📜 Taksi <b>litsenziyasi</b> fotosi:")
got_taxi = _photo_step("taxi_license", CAR_F, "🚗 Mashina — <b>old</b> tomondan fotosi:")
got_car_f = _photo_step("car_photo_front", CAR_B, "🚗 Mashina — <b>orqa</b> tomondan fotosi:")
got_car_b = _photo_step("car_photo_back", CAR_L, "🚗 Mashina — <b>chap</b> tomondan fotosi:")
got_car_l = _photo_step("car_photo_left", CAR_R, "🚗 Mashina — <b>o'ng</b> tomondan fotosi:")
got_car_r = _photo_step("car_photo_right", CAR_INT, "🚗 Mashina — <b>salon</b> fotosi:")


async def got_car_int(update: Update, context: ContextTypes.DEFAULT_TYPE):
    fid = photo_of(update)
    if not fid:
        await update.message.reply_text("Iltimos, salon fotosini yuboring.")
        return CAR_INT
    context.user_data["car_photo_interior"] = fid
    await update.message.reply_text("🚘 Mashina rusumi (masalan: Chevrolet Cobalt):")
    return MAKE


async def got_make(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["car_make"] = update.message.text.strip()
    await update.message.reply_text("🎨 Mashina rangi:")
    return COLOR


async def got_color(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["car_color"] = update.message.text.strip()
    await update.message.reply_text("🔢 Davlat raqami (masalan: 01A123BC):")
    return PLATE


async def got_plate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = context.user_data
    d["car_plate"] = update.message.text.strip()
    user = update.effective_user
    async with pool.acquire() as con:
        await con.execute(
            """INSERT INTO drivers
               (telegram_id, full_name, username, phone,
                license_front, license_back, techpass_front, techpass_back, taxi_license,
                car_photo_front, car_photo_back, car_photo_left, car_photo_right, car_photo_interior,
                car_make, car_color, car_plate, status)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,'pending')
               ON CONFLICT (telegram_id) DO UPDATE SET
                 full_name=EXCLUDED.full_name, phone=EXCLUDED.phone,
                 license_front=EXCLUDED.license_front, license_back=EXCLUDED.license_back,
                 techpass_front=EXCLUDED.techpass_front, techpass_back=EXCLUDED.techpass_back,
                 taxi_license=EXCLUDED.taxi_license,
                 car_photo_front=EXCLUDED.car_photo_front, car_photo_back=EXCLUDED.car_photo_back,
                 car_photo_left=EXCLUDED.car_photo_left, car_photo_right=EXCLUDED.car_photo_right,
                 car_photo_interior=EXCLUDED.car_photo_interior,
                 car_make=EXCLUDED.car_make, car_color=EXCLUDED.car_color, car_plate=EXCLUDED.car_plate,
                 status='pending', reject_reason=NULL""",
            user.id, d.get("name"), ("@" + user.username) if user.username else None, d.get("phone"),
            d.get("license_front"), d.get("license_back"), d.get("techpass_front"), d.get("techpass_back"),
            d.get("taxi_license"), d.get("car_photo_front"), d.get("car_photo_back"), d.get("car_photo_left"),
            d.get("car_photo_right"), d.get("car_photo_interior"),
            d.get("car_make"), d.get("car_color"), d.get("car_plate"),
        )
    context.user_data.clear()
    await update.message.reply_text(
        "✅ Arizangiz qabul qilindi va ko'rib chiqishga yuborildi.\n"
        "Tasdiqlangach sizga xabar beramiz. Rahmat!",
        reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Bekor qilindi. /start bilan qайta boshlang.",
                                    reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


# --------------------------------------------------------------------------- #
# Daily selfie
# --------------------------------------------------------------------------- #
async def selfie_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    drv = await get_driver(update.effective_user.id)
    if not drv or drv["status"] != "approved":
        await update.message.reply_text("Bu buyruq faqat tasdiqlangan haydovchilar uchun.")
        return ConversationHandler.END
    await update.message.reply_text("🤳 Bugungi selfi rasmingizni yuboring:")
    return SELFIE


async def got_selfie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    fid = photo_of(update)
    if not fid:
        await update.message.reply_text("Iltimos, selfi (foto) yuboring.")
        return SELFIE
    drv = await get_driver(update.effective_user.id)
    async with pool.acquire() as con:
        await con.execute(
            """INSERT INTO driver_checkins (driver_id, day, selfie_file_id, status)
               VALUES ($1, CURRENT_DATE, $2, 'approved')
               ON CONFLICT (driver_id, day) DO UPDATE
               SET selfie_file_id=EXCLUDED.selfie_file_id, status='approved', created_at=now()""",
            drv["id"], fid)
    await update.message.reply_text("✅ Selfi qabul qilindi. Endi /online bo'lib buyurtma olishingiz mumkin.")
    return ConversationHandler.END


# --------------------------------------------------------------------------- #
# Online / offline
# --------------------------------------------------------------------------- #
async def _set_online(update, context, value: bool):
    drv = await get_driver(update.effective_user.id)
    if not drv or drv["status"] != "approved":
        await update.message.reply_text("Faqat tasdiqlangan haydovchilar uchun.")
        return
    if value:
        async with pool.acquire() as con:
            ok = await con.fetchval(
                "SELECT 1 FROM driver_checkins WHERE driver_id=$1 AND day=CURRENT_DATE AND status='approved'",
                drv["id"])
        if not ok:
            await update.message.reply_text("Avval bugungi selfingizni yuboring: /selfie")
            return
    async with pool.acquire() as con:
        await con.execute("UPDATE drivers SET is_online=$2 WHERE id=$1", drv["id"], value)
    await update.message.reply_text("🟢 Onlayn — buyurtma qabul qilyapsiz." if value
                                    else "⚪️ Oflayn — buyurtma qabul qilmayapsiz.")


async def online_cmd(update, context): await _set_online(update, context, True)
async def offline_cmd(update, context): await _set_online(update, context, False)


# --------------------------------------------------------------------------- #
# Accept an order (driver taps the inline button on a dispatched order)
# --------------------------------------------------------------------------- #
async def accept_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import httpx
    q = update.callback_query
    try:
        _, code = q.data.split(":", 1)
    except ValueError:
        await q.answer()
        return
    drv = await get_driver(q.from_user.id)
    if not drv or drv["status"] != "approved":
        await q.answer("Faqat tasdiqlangan haydovchilar uchun.", show_alert=True)
        return
    if not BACKEND_URL:
        await q.answer("Backend sozlanmagan.", show_alert=True)
        return
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(f"{BACKEND_URL}/api/orders/{code}/accept",
                             json={"driver_telegram_id": q.from_user.id})
            j = r.json()
    except Exception:  # noqa: BLE001
        await q.answer("Xatolik. Qayta urinib ko'ring.", show_alert=True)
        return
    if not j.get("ok"):
        msg = {"taken": "Buyurtma allaqachon olingan.",
               "notfound": "Buyurtma topilmadi.",
               "not_driver": "Siz tasdiqlangan haydovchi emassiz."}.get(j.get("error"), "Olib bo'lmadi.")
        await q.answer(msg, show_alert=True)
        try:
            await q.edit_message_reply_markup(None)
        except Exception:  # noqa: BLE001
            pass
        return
    await q.answer("Qabul qilindi ✅")
    try:
        await q.edit_message_text((q.message.text or "") +
                                  "\n\n✅ Siz qabul qildingiz. Mijozga xabar berildi.")
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #
async def post_init(app: Application):
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    log.info("Driver bot connected to DB.")


def build() -> Application:
    app = Application.builder().token(TOKEN).post_init(post_init).build()

    reg = ConversationHandler(
        entry_points=[CommandHandler("start", start), CommandHandler("reregister", reregister)],
        states={
            PHONE: [MessageHandler(filters.CONTACT, got_phone)],
            NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_name)],
            LIC_F: [MessageHandler(filters.PHOTO, got_lic_f)],
            LIC_B: [MessageHandler(filters.PHOTO, got_lic_b)],
            TECH_F: [MessageHandler(filters.PHOTO, got_tech_f)],
            TECH_B: [MessageHandler(filters.PHOTO, got_tech_b)],
            TAXI: [MessageHandler(filters.PHOTO, got_taxi)],
            CAR_F: [MessageHandler(filters.PHOTO, got_car_f)],
            CAR_B: [MessageHandler(filters.PHOTO, got_car_b)],
            CAR_L: [MessageHandler(filters.PHOTO, got_car_l)],
            CAR_R: [MessageHandler(filters.PHOTO, got_car_r)],
            CAR_INT: [MessageHandler(filters.PHOTO, got_car_int)],
            MAKE: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_make)],
            COLOR: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_color)],
            PLATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_plate)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    selfie_conv = ConversationHandler(
        entry_points=[CommandHandler("selfie", selfie_cmd)],
        states={SELFIE: [MessageHandler(filters.PHOTO, got_selfie)]},
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(reg)
    app.add_handler(selfie_conv)
    app.add_handler(CommandHandler("online", online_cmd))
    app.add_handler(CommandHandler("offline", offline_cmd))
    app.add_handler(CallbackQueryHandler(accept_cb, pattern=r"^accept:"))
    return app


def main():
    if not TOKEN:
        raise SystemExit("DRIVER_BOT_TOKEN is not set")
    if not DATABASE_URL:
        raise SystemExit("DATABASE_URL is not set")
    log.info("Starting TezGo driver bot…")
    build().run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
