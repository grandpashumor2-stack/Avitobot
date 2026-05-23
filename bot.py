import os
import json
import asyncio
import logging
import anthropic
import base64
import httpx
import sqlite3
import uuid
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
    PreCheckoutQueryHandler
)
from telegram.constants import ParseMode

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = "8525104782:AAF6mmW1n4tthUL9fRQ7XQFkjhB6hsJmiyE"
ANTHROPIC_API_KEY = "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"
YOOKASSA_SHOP_ID = "YOUR_SHOP_ID"
YOOKASSA_SECRET = "YOUR_SECRET_KEY"
PAYMENT_PROVIDER_TOKEN = ""
FREE_LIMIT = 3
PRICE_MONTH = 199
PRICE_FOREVER = 999

anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
user_states = {}
DB_PATH = "avito_bot.db"

def init_db():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY, username TEXT,
        free_used INTEGER DEFAULT 0, plan TEXT DEFAULT 'free',
        plan_until TEXT DEFAULT NULL, created_at TEXT DEFAULT (datetime('now')))""")
    cur.execute("""CREATE TABLE IF NOT EXISTS payments (
        payment_id TEXT PRIMARY KEY, user_id INTEGER, plan TEXT,
        amount INTEGER, status TEXT DEFAULT 'pending',
        created_at TEXT DEFAULT (datetime('now')))""")
    con.commit()
    con.close()

def get_user(user_id):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT user_id,username,free_used,plan,plan_until FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    con.close()
    if row:
        return {"user_id":row[0],"username":row[1],"free_used":row[2],"plan":row[3],"plan_until":row[4]}
    return None

def ensure_user(user_id, username):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("INSERT OR IGNORE INTO users (user_id,username) VALUES (?,?)", (user_id, username))
    con.commit()
    con.close()

def increment_usage(user_id):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("UPDATE users SET free_used=free_used+1 WHERE user_id=?", (user_id,))
    con.commit()
    con.close()

def activate_plan(user_id, plan):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    if plan == "month":
        until = (datetime.utcnow()+timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        cur.execute("UPDATE users SET plan='month',plan_until=? WHERE user_id=?", (until,user_id))
    elif plan == "forever":
        cur.execute("UPDATE users SET plan='forever',plan_until=NULL WHERE user_id=?", (user_id,))
    con.commit()
    con.close()

def save_payment(payment_id, user_id, plan, amount):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("INSERT OR IGNORE INTO payments (payment_id,user_id,plan,amount) VALUES (?,?,?,?)",
                (payment_id,user_id,plan,amount))
    con.commit()
    con.close()

def confirm_payment(payment_id):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("UPDATE payments SET status='paid' WHERE payment_id=?", (payment_id,))
    con.commit()
    con.close()

def is_subscribed(user):
    if user["plan"] == "forever":
        return True
    if user["plan"] == "month" and user["plan_until"]:
        until = datetime.strptime(user["plan_until"], "%Y-%m-%d %H:%M:%S")
        return datetime.utcnow() < until
    return False

def can_use(user):
    if is_subscribed(user):
        return True
    return user["free_used"] < FREE_LIMIT

def status_text(user):
    if user["plan"] == "forever":
        return "♾️ Навсегда"
    if user["plan"] == "month" and user["plan_until"]:
        until = datetime.strptime(user["plan_until"], "%Y-%m-%d %H:%M:%S")
        if datetime.utcnow() < until:
            days_left = (until-datetime.utcnow()).days
            return f"📅 До {until.strftime('%d.%m.%Y')} (ещё {days_left} дн.)"
    left = max(0, FREE_LIMIT-user["free_used"])
    return f"🆓 Бесплатно: {left} из {FREE_LIMIT} осталось"

SYSTEM_PROMPT = """Ты — профессиональный помощник для создания объявлений на Авито.
Когда пользователь описывает товар или присылает фото — создай карточку в формате JSON:
{"title":"Заголовок до 50 символов","price":"цена числом в рублях","price_hint":"почему такая цена",
"category":"категория","subcategory":"подкатегория","description":"описание 5-7 предложений",
"condition":"Новое/Отличное/Хорошее/Удовлетворительное","tags":["тег1","тег2","тег3"],
"seo_keywords":"ключевые слова","call_to_action":"призыв к действию",
"photo_tips":"3 совета по фото","warnings":"что указать чтобы избежать споров"}
Возвращай ТОЛЬКО JSON без markdown и пояснений."""

def format_card(data):
    price = data.get("price","")
    try:
        price_fmt = f"{int(price):,}".replace(",", " ") + " ₽"
    except:
        price_fmt = f"{price} ₽"
    tags_str = " ".join([f"#{t.replace(' ','_')}" for t in data.get("tags",[])])
    text = (f"✅ *Карточка готова!*\n\n"
            f"📌 *Заголовок:*\n`{data.get('title','')}`\n\n"
            f"💰 *Цена:* {price_fmt}\n_{data.get('price_hint','')}_\n\n"
            f"📂 *Категория:* {data.get('category','')} → {data.get('subcategory','')}\n"
            f"🔧 *Состояние:* {data.get('condition','')}\n\n"
            f"📝 *Описание:*\n{data.get('description','')}\n\n"
            f"🔍 *SEO:*\n`{data.get('seo_keywords','')}`\n\n"
            f"🏷 *Теги:*\n{tags_str}\n\n"
            f"💬 _{data.get('call_to_action','')}_")
    if data.get("warnings"):
        text += f"\n\n⚠️ *Важно:*\n{data['warnings']}"
    if data.get("photo_tips"):
        text += f"\n\n📸 *Фото:*\n{data['photo_tips']}"
    return text

def get_card_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Изменить цену", callback_data="edit_price"),
         InlineKeyboardButton("📝 Другое описание", callback_data="edit_desc")],
        [InlineKeyboardButton("🔤 3 заголовка", callback_data="alt_titles"),
         InlineKeyboardButton("📸 Советы по фото", callback_data="photo_tips")],
        [InlineKeyboardButton("📋 Копировать", callback_data="copy_text"),
         InlineKeyboardButton("🔁 Новый товар", callback_data="new_item")],
    ])

def get_subscribe_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 Месяц — 199 ₽", callback_data="pay_month")],
        [InlineKeyboardButton("♾️ Навсегда — 999 ₽", callback_data="pay_forever")],
    ])

async def generate_card(prompt, image_b64=None, image_mime=None):
    content = []
    if image_b64 and image_mime:
        content.append({"type":"image","source":{"type":"base64","media_type":image_mime,"data":image_b64}})
    content.append({"type":"text","text":prompt})
    response = anthropic_client.messages.create(
        model="claude-sonnet-4-20250514", max_tokens=1500,
        system=SYSTEM_PROMPT, messages=[{"role":"user","content":content}])
    raw = response.content[0].text.strip().replace("```json","").replace("```","").strip()
    try:
        return json.loads(raw)
    except:
        return raw

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(u.id, u.username or u.first_name)
    user = get_user(u.id)
    text = (f"👋 Привет, {u.first_name}!\n\n"
            f"Я *AvitoHelperBot* — создаю карточки для Авито за секунды.\n\n"
            f"📸 Анализирую фото товара\n📝 Пишу продающие описания\n"
            f"💰 Рекомендую цену\n🏷 Подбираю теги\n\n"
            f"*Твой статус:* {status_text(user)}\n\n"
            f"Отправь фото или опиши товар!")
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 Подписка", callback_data="show_plans"),
             InlineKeyboardButton("👤 Мой статус", callback_data="my_status")],
            [InlineKeyboardButton("❓ Помощь", callback_data="show_help")],
        ]))

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(u.id, u.username or u.first_name)
    user = get_user(u.id)
    text = f"👤 *Твой аккаунт*\n\n📊 Статус: {status_text(user)}\n"
    kb = None if is_subscribed(user) else get_subscribe_keyboard()
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

async def subscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (f"💳 *Тарифы*\n\n🆓 Бесплатно — {FREE_LIMIT} карточки\n\n"
            f"📅 Месяц — {PRICE_MONTH} ₽\nБезлимит на 30 дней\n\n"
            f"♾️ Навсегда — {PRICE_FOREVER} ₽\nБезлимит навсегда")
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN,
                                    reply_markup=get_subscribe_keyboard())

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = ("*Как пользоваться:*\n\n"
            "1️⃣ Отправь фото товара\n"
            "2️⃣ Или напиши описание\n"
            "3️⃣ Получи готовую карточку\n\n"
            "/status — твой тариф\n/subscribe — подписка\n/tips — советы по продажам")
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def tips_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "*Советы для продаж:*\n\n📸 Минимум 5 фото, дневной свет\n"
        "📝 Все характеристики, год покупки\n💰 Оставь 10-15% на торг\n"
        "⚡ Отвечай быстро, предлагай доставку", parse_mode=ParseMode.MARKDOWN)

async def _process_and_reply(update, user_id, prompt, image_b64=None, image_mime=None):
    user = get_user(user_id)
    if not can_use(user):
        await update.message.reply_text(
            f"🚫 *Бесплатные попытки закончились*\n\nИспользовал все {FREE_LIMIT} карточки.\nОформи подписку 👇",
            parse_mode=ParseMode.MARKDOWN, reply_markup=get_subscribe_keyboard())
        return
    if not is_subscribed(user):
        increment_usage(user_id)
        user = get_user(user_id)
        left = max(0, FREE_LIMIT-user["free_used"])
        msg = f"⏳ Создаю карточку... (осталось бесплатных: {left})" if left > 0 else "⏳ Последняя бесплатная попытка..."
        await update.message.reply_text(msg)
    else:
        await update.message.reply_text("⏳ Создаю карточку...")
    result = await asyncio.to_thread(generate_card, prompt, image_b64, image_mime)
    if isinstance(result, dict):
        user_states[user_id] = {"last_card":result,"original_prompt":prompt,"image_b64":image_b64,"image_mime":image_mime}
        await update.message.reply_text(format_card(result), parse_mode=ParseMode.MARKDOWN, reply_markup=get_card_keyboard())
    else:
        await update.message.reply_text(result)

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(u.id, u.username or u.first_name)
    await _process_and_reply(update, u.id, update.message.text)

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(u.id, u.username or u.first_name)
    caption = update.message.caption or ""
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    async with httpx.AsyncClient() as client:
        resp = await client.get(file.file_path)
        image_bytes = resp.content
    image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
    prompt = caption if caption else "Создай карточку для этого товара на Авито."
    await _process_and_reply(update, u.id, prompt, image_b64, "image/jpeg")

async def send_invoice(query, plan):
    user_id = query.from_user.id
    if plan == "month":
        title, description, amount = "Подписка на месяц", "Безлимит на 30 дней", PRICE_MONTH*100
        payload = f"month_{user_id}_{uuid.uuid4().hex[:8]}"
    else:
        title, description, amount = "Подписка навсегда", "Безлимит без срока", PRICE_FOREVER*100
        payload = f"forever_{user_id}_{uuid.uuid4().hex[:8]}"
    save_payment(payload, user_id, plan, amount//100)
    if not PAYMENT_PROVIDER_TOKEN:
        await query.message.reply_text(
            f"⚙️ *Оплата пока не настроена*\n\nДля подключения ЮКассы нужно:\n"
            f"1. Зарегистрироваться на yookassa.ru\n2. Получить токен в @BotFather → Payments\n\n"
            f"_Выбран план: {title} — {amount//100} ₽_", parse_mode=ParseMode.MARKDOWN)
        return
    await query.message.reply_invoice(
        title=title, description=description, payload=payload,
        provider_token=PAYMENT_PROVIDER_TOKEN, currency="RUB",
        prices=[LabeledPrice(title, amount)])

async def pre_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.pre_checkout_query.answer(ok=True)

async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payload = update.message.successful_payment.invoice_payload
    user_id = update.effective_user.id
    plan = "month" if payload.startswith("month_") else "forever"
    confirm_payment(payload)
    activate_plan(user_id, plan)
    label = "на месяц" if plan == "month" else "навсегда"
    user = get_user(user_id)
    await update.message.reply_text(
        f"🎉 *Оплата прошла!*\n\n✅ Подписка *{label}* активирована\n{status_text(user)}",
        parse_mode=ParseMode.MARKDOWN)

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data
    state = user_states.get(user_id, {})
    ensure_user(user_id, query.from_user.username or query.from_user.first_name)
    if data == "show_help":
        await query.message.reply_text("Отправь фото или опиши товар!\n/help /status /subscribe")
    elif data == "my_status":
        user = get_user(user_id)
        kb = None if is_subscribed(user) else get_subscribe_keyboard()
        await query.message.reply_text(f"👤 *Статус:* {status_text(user)}", parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    elif data == "show_plans":
        await subscribe_command(query, context)
    elif data == "pay_month":
        await send_invoice(query, "month")
    elif data == "pay_forever":
        await send_invoice(query, "forever")
    elif data == "new_item":
        await query.message.reply_text("🔁 Отправь описание или фото нового товара!")
    elif data == "copy_text" and state.get("last_card"):
        card = state["last_card"]
        text = f"{card.get('title','')}\n\n{card.get('description','')}\n\n{card.get('call_to_action','')}"
        await query.message.reply_text(f"📋 *Копируй:*\n\n`{text}`", parse_mode=ParseMode.MARKDOWN)
    elif data == "photo_tips" and state.get("last_card"):
        await query.message.reply_text(f"📸 *Советы по фото:*\n\n{state['last_card'].get('photo_tips','')}", parse_mode=ParseMode.MARKDOWN)
    elif data in ["edit_price","edit_desc","alt_titles"] and state.get("original_prompt"):
        prompts = {
            "edit_price": "Предложи 3 варианта цены: эконом, оптимальная, премиум. Объясни каждый.",
            "edit_desc": "Перепиши описание более эмоционально и продающе.",
            "alt_titles": "Придумай 3 варианта заголовка до 50 символов.",
        }
        await query.message.reply_text("⏳ Генерирую...")
        result = await asyncio.to_thread(generate_card,
            f"Товар: {state['original_prompt']}\n\n{prompts[data]}",
            state.get("image_b64"), state.get("image_mime"))
        if isinstance(result, dict):
            user_states[user_id]["last_card"] = result
            await query.message.reply_text(format_card(result), parse_mode=ParseMode.MARKDOWN, reply_markup=get_card_keyboard())
        else:
            await query.message.reply_text(result, parse_mode=ParseMode.MARKDOWN)

def main():
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("subscribe", subscribe_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("tips", tips_command))
    app.add_handler(PreCheckoutQueryHandler(pre_checkout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(handle_callback))
    logger.info("AvitoHelperBot запущен!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
