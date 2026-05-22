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

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
#  НАСТРОЙКИ — замени на свои
# ─────────────────────────────────────────────
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN",   "8525104782:AAF6mmW1n4tthUL9fRQ7XQFkjhB6hsJmiyE")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "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")

# ЮКасса — вставь свои данные после регистрации на yookassa.ru
YOOKASSA_SHOP_ID  = os.getenv("YOOKASSA_SHOP_ID",  "YOUR_SHOP_ID")
YOOKASSA_SECRET   = os.getenv("YOOKASSA_SECRET",   "YOUR_SECRET_KEY")

# Telegram Payments провайдер-токен (получить у @BotFather → Payments)
PAYMENT_PROVIDER_TOKEN = os.getenv("PAYMENT_PROVIDER_TOKEN", "")

FREE_LIMIT = 3
PRICE_MONTH   = 199   # рублей
PRICE_FOREVER = 999   # рублей

anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
user_states: dict = {}

# ─────────────────────────────────────────────
#  БАЗА ДАННЫХ (SQLite)
# ─────────────────────────────────────────────
DB_PATH = "avito_bot.db"

def init_db():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id     INTEGER PRIMARY KEY,
            username    TEXT,
            free_used   INTEGER DEFAULT 0,
            plan        TEXT DEFAULT 'free',
            plan_until  TEXT DEFAULT NULL,
            created_at  TEXT DEFAULT (datetime('now'))
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            payment_id  TEXT PRIMARY KEY,
            user_id     INTEGER,
            plan        TEXT,
            amount      INTEGER,
            status      TEXT DEFAULT 'pending',
            created_at  TEXT DEFAULT (datetime('now'))
        )
    """)
    con.commit()
    con.close()

def get_user(user_id: int) -> dict:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT user_id, username, free_used, plan, plan_until FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    con.close()
    if row:
        return {"user_id": row[0], "username": row[1], "free_used": row[2], "plan": row[3], "plan_until": row[4]}
    return None

def ensure_user(user_id: int, username: str):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)", (user_id, username))
    con.commit()
    con.close()

def increment_usage(user_id: int):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("UPDATE users SET free_used = free_used + 1 WHERE user_id=?", (user_id,))
    con.commit()
    con.close()

def activate_plan(user_id: int, plan: str):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    if plan == "month":
        until = (datetime.utcnow() + timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        cur.execute("UPDATE users SET plan='month', plan_until=? WHERE user_id=?", (until, user_id))
    elif plan == "forever":
        cur.execute("UPDATE users SET plan='forever', plan_until=NULL WHERE user_id=?", (user_id,))
    con.commit()
    con.close()

def save_payment(payment_id: str, user_id: int, plan: str, amount: int):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("INSERT OR IGNORE INTO payments (payment_id, user_id, plan, amount) VALUES (?,?,?,?)",
                (payment_id, user_id, plan, amount))
    con.commit()
    con.close()

def confirm_payment(payment_id: str):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("UPDATE payments SET status='paid' WHERE payment_id=?", (payment_id,))
    con.commit()
    con.close()

def can_use(user: dict) -> bool:
    """Проверяет, может ли пользователь создать карточку."""
    if user["plan"] == "forever":
        return True
    if user["plan"] == "month" and user["plan_until"]:
        until = datetime.strptime(user["plan_until"], "%Y-%m-%d %H:%M:%S")
        if datetime.utcnow() < until:
            return True
    if user["free_used"] < FREE_LIMIT:
        return True
    return False

def is_subscribed(user: dict) -> bool:
    if user["plan"] == "forever":
        return True
    if user["plan"] == "month" and user["plan_until"]:
        until = datetime.strptime(user["plan_until"], "%Y-%m-%d %H:%M:%S")
        return datetime.utcnow() < until
    return False

def status_text(user: dict) -> str:
    if user["plan"] == "forever":
        return "♾️ Навсегда"
    if user["plan"] == "month" and user["plan_until"]:
        until = datetime.strptime(user["plan_until"], "%Y-%m-%d %H:%M:%S")
        if datetime.utcnow() < until:
            days_left = (until - datetime.utcnow()).days
            return f"📅 До {until.strftime('%d.%m.%Y')} (ещё {days_left} дн.)"
    used = user["free_used"]
    left = max(0, FREE_LIMIT - used)
    return f"🆓 Бесплатно: {left} из {FREE_LIMIT} осталось"

# ─────────────────────────────────────────────
#  AI
# ─────────────────────────────────────────────
SYSTEM_PROMPT = """Ты — профессиональный помощник для создания объявлений на Авито.

Когда пользователь описывает товар или присылает фото — создай карточку объявления в формате JSON:
{
  "title": "Заголовок до 50 символов (продающий, с ключевыми словами)",
  "price": "Рекомендуемая цена (только число в рублях, без символов)",
  "price_hint": "Краткое объяснение почему такая цена",
  "category": "Категория на Авито",
  "subcategory": "Подкатегория",
  "description": "Продающее описание 5-7 предложений. Укажи характеристики, состояние, комплектацию, причину продажи, преимущества.",
  "condition": "Новое / Отличное / Хорошее / Удовлетворительное",
  "tags": ["тег1", "тег2", "тег3", "тег4", "тег5"],
  "seo_keywords": "ключевые слова через запятую для поиска",
  "call_to_action": "Призыв к действию (1 предложение)",
  "photo_tips": "3 конкретных совета как лучше сфотографировать этот товар",
  "warnings": "Что указать в объявлении чтобы избежать споров (необязательное поле)"
}

Возвращай ТОЛЬКО JSON без markdown-блоков, пояснений и комментариев.
Если информации недостаточно — запроси уточнения на русском языке (не JSON).
"""

def format_card(data: dict) -> str:
    price = data.get("price", "Не указана")
    try:
        price_fmt = f"{int(price):,}".replace(",", " ") + " ₽"
    except (ValueError, TypeError):
        price_fmt = f"{price} ₽"
    tags_str = " ".join([f"#{t.replace(' ', '_')}" for t in data.get("tags", [])])
    text = (
        f"✅ *Карточка объявления готова!*\n\n"
        f"📌 *Заголовок:*\n`{data.get('title', '')}`\n\n"
        f"💰 *Цена:* {price_fmt}\n"
        f"_{data.get('price_hint', '')}_\n\n"
        f"📂 *Категория:* {data.get('category', '')} → {data.get('subcategory', '')}\n"
        f"🔧 *Состояние:* {data.get('condition', '')}\n\n"
        f"📝 *Описание:*\n{data.get('description', '')}\n\n"
        f"🔍 *SEO-ключи:*\n`{data.get('seo_keywords', '')}`\n\n"
        f"🏷 *Теги:*\n{tags_str}\n\n"
        f"💬 *Призыв к действию:*\n_{data.get('call_to_action', '')}_"
    )
    if data.get("warnings"):
        text += f"\n\n⚠️ *Важно указать:*\n{data['warnings']}"
    if data.get("photo_tips"):
        text += f"\n\n📸 *Советы по фото:*\n{data['photo_tips']}"
    return text

def get_card_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Изменить цену", callback_data="edit_price"),
         InlineKeyboardButton("📝 Другое описание", callback_data="edit_desc")],
        [InlineKeyboardButton("🔤 3 варианта заголовка", callback_data="alt_titles"),
         InlineKeyboardButton("📸 Советы по фото", callback_data="photo_tips")],
        [InlineKeyboardButton("📋 Копировать текст", callback_data="copy_text"),
         InlineKeyboardButton("🔁 Новый товар", callback_data="new_item")],
    ])

def get_subscribe_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 Месяц — 199 ₽", callback_data="pay_month")],
        [InlineKeyboardButton("♾️ Навсегда — 999 ₽", callback_data="pay_forever")],
    ])

async def generate_card(prompt: str, image_b64: str = None, image_mime: str = None):
    content = []
    if image_b64 and image_mime:
        content.append({"type": "image", "source": {"type": "base64", "media_type": image_mime, "data": image_b64}})
    content.append({"type": "text", "text": prompt})
    response = anthropic_client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1500,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}]
    )
    raw = response.content[0].text.strip().replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw

# ─────────────────────────────────────────────
#  ОБРАБОТЧИКИ
# ─────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(u.id, u.username or u.first_name)
    user = get_user(u.id)
    text = (
        f"👋 Привет, {u.first_name}!\n\n"
        f"Я *AvitoHelperBot* — создаю продающие карточки для Авито за секунды.\n\n"
        f"*Что умею:*\n"
        f"📸 Анализировать фото товара\n"
        f"📝 Писать продающие описания\n"
        f"💰 Рекомендовать цену\n"
        f"🏷 Подбирать теги и ключевые слова\n\n"
        f"*Твой статус:* {status_text(user)}\n\n"
        f"Просто отправь фото или опиши товар текстом!"
    )
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
    text = (
        f"👤 *Твой аккаунт*\n\n"
        f"🆔 ID: `{u.id}`\n"
        f"📊 Статус: {status_text(user)}\n\n"
    )
    if not is_subscribed(user):
        text += f"Осталось бесплатных попыток: *{max(0, FREE_LIMIT - user['free_used'])}*\n\n"
        text += "Оформи подписку чтобы пользоваться без ограничений 👇"
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN,
                                        reply_markup=get_subscribe_keyboard())
    else:
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def subscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "💳 *Тарифы AvitoHelperBot*\n\n"
        f"🆓 *Бесплатно* — {FREE_LIMIT} карточки\n\n"
        f"📅 *Месяц* — {PRICE_MONTH} ₽\n"
        "Безлимитные карточки на 30 дней\n\n"
        f"♾️ *Навсегда* — {PRICE_FOREVER} ₽\n"
        "Безлимит без ограничений по времени\n\n"
        "Оплата через ЮКассу — банковские карты, СБП, ЮMoney"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN,
                                    reply_markup=get_subscribe_keyboard())

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "*Как пользоваться ботом:*\n\n"
        "1️⃣ Отправь *фото* товара\n"
        "2️⃣ Или напиши *описание*: что продаёшь, состояние, характеристики\n"
        "3️⃣ Получи готовую карточку с кнопками для правок\n\n"
        "*Команды:*\n"
        "/start — главное меню\n"
        "/status — твой тариф и остаток\n"
        "/subscribe — оформить подписку\n"
        "/help — эта справка\n"
        "/tips — советы по продажам"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def tips_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "*💡 Советы для продаж на Авито:*\n\n"
        "📸 *Фото:* минимум 5 снимков, дневной свет, честно покажи дефекты\n"
        "📝 *Описание:* все характеристики, год покупки, комплект\n"
        "💰 *Цена:* оставь 10-15% на торг, изучи похожие объявления\n"
        "⚡ *Скорость:* отвечай быстро, предлагай доставку"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

# ─── Проверка лимита и генерация ─────────────
async def _process_and_reply(update, user_id: int, prompt: str, image_b64=None, image_mime=None):
    user = get_user(user_id)
    if not can_use(user):
        await update.message.reply_text(
            f"🚫 *Бесплатные попытки закончились*\n\n"
            f"Ты использовал все {FREE_LIMIT} бесплатные карточки.\n"
            f"Оформи подписку чтобы продолжить 👇",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_subscribe_keyboard()
        )
        return

    # Считаем попытку только у бесплатных
    if not is_subscribed(user):
        increment_usage(user_id)
        user = get_user(user_id)
        left = max(0, FREE_LIMIT - user["free_used"])
        if left > 0:
            await update.message.reply_text(f"⏳ Создаю карточку... (осталось бесплатных: {left})")
        else:
            await update.message.reply_text(
                f"⏳ Создаю карточку... Это твоя последняя бесплатная попытка.\n"
                f"После этого потребуется подписка 👇",
            )
    else:
        await update.message.reply_text("⏳ Создаю карточку...")

    result = await asyncio.to_thread(generate_card, prompt, image_b64, image_mime)

    if isinstance(result, dict):
        user_states[user_id] = {"last_card": result, "original_prompt": prompt,
                                 "image_b64": image_b64, "image_mime": image_mime}
        await update.message.reply_text(format_card(result),
                                        parse_mode=ParseMode.MARKDOWN,
                                        reply_markup=get_card_keyboard())
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
    prompt = caption if caption else "Создай карточку для этого товара на Авито. Определи товар, оцени состояние, предложи цену."
    await _process_and_reply(update, u.id, prompt, image_b64, "image/jpeg")

# ─── Платежи через Telegram Payments (ЮКасса) ─
async def send_invoice(query, plan: str):
    user_id = query.from_user.id
    if plan == "month":
        title       = "Подписка на месяц"
        description = "Безлимитные карточки Авито на 30 дней"
        amount      = PRICE_MONTH * 100   # в копейках
        payload     = f"month_{user_id}_{uuid.uuid4().hex[:8]}"
    else:
        title       = "Подписка навсегда"
        description = "Безлимитные карточки Авито без срока"
        amount      = PRICE_FOREVER * 100
        payload     = f"forever_{user_id}_{uuid.uuid4().hex[:8]}"

    save_payment(payload, user_id, plan, amount // 100)

    if not PAYMENT_PROVIDER_TOKEN:
        # Режим без реального провайдера — показываем инструкцию
        await query.message.reply_text(
            f"⚙️ *Платёжный провайдер не настроен*\n\n"
            f"Для активации оплаты:\n"
            f"1. Зарегистрируйся на *yookassa.ru*\n"
            f"2. Получи токен в *@BotFather → Payments → ЮKassa*\n"
            f"3. Добавь его в `PAYMENT_PROVIDER_TOKEN`\n\n"
            f"_Выбранный план: {title} — {amount//100} ₽_",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    await query.message.reply_invoice(
        title=title,
        description=description,
        payload=payload,
        provider_token=PAYMENT_PROVIDER_TOKEN,
        currency="RUB",
        prices=[LabeledPrice(title, amount)],
        start_parameter=f"pay_{plan}",
    )

async def pre_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.pre_checkout_query.answer(ok=True)

async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payment = update.message.successful_payment
    payload = payment.invoice_payload
    user_id = update.effective_user.id
    plan = "month" if payload.startswith("month_") else "forever"
    confirm_payment(payload)
    activate_plan(user_id, plan)
    label = "на месяц" if plan == "month" else "навсегда"
    user = get_user(user_id)
    await update.message.reply_text(
        f"🎉 *Оплата прошла успешно!*\n\n"
        f"✅ Подписка *{label}* активирована\n"
        f"📊 Статус: {status_text(user)}\n\n"
        f"Теперь создавай карточки без ограничений! 🚀",
        parse_mode=ParseMode.MARKDOWN
    )

# ─── Callback-кнопки ─────────────────────────
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data
    state = user_states.get(user_id, {})
    ensure_user(user_id, query.from_user.username or query.from_user.first_name)

    if data == "show_help":
        await query.message.reply_text(
            "Отправь фото или опиши товар — создам карточку!\nКоманды: /help /status /subscribe",
            parse_mode=ParseMode.MARKDOWN)
        return

    if data == "my_status":
        user = get_user(user_id)
        text = f"👤 *Твой статус*\n\n{status_text(user)}"
        kb = None if is_subscribed(user) else get_subscribe_keyboard()
        await query.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        return

    if data == "show_plans":
        text = (
            "💳 *Тарифы*\n\n"
            f"🆓 Бесплатно — {FREE_LIMIT} карточки\n"
            f"📅 Месяц — {PRICE_MONTH} ₽\n"
            f"♾️ Навсегда — {PRICE_FOREVER} ₽"
        )
        await query.message.reply_text(text, parse_mode=ParseMode.MARKDOWN,
                                       reply_markup=get_subscribe_keyboard())
        return

    if data == "pay_month":
        await send_invoice(query, "month")
        return

    if data == "pay_forever":
        await send_invoice(query, "forever")
        return

    if data == "new_item":
        await query.message.reply_text("🔁 Отправь описание или фото нового товара!")
        return

    if data == "copy_text" and state.get("last_card"):
        card = state["last_card"]
        copyable = f"{card.get('title','')}\n\n{card.get('description','')}\n\n{card.get('call_to_action','')}"
        await query.message.reply_text(f"📋 *Текст для копирования:*\n\n`{copyable}`",
                                       parse_mode=ParseMode.MARKDOWN)
        return

    if data == "photo_tips" and state.get("last_card"):
        tips = state["last_card"].get("photo_tips", "Советы недоступны")
        await query.message.reply_text(f"📸 *Советы по фото:*\n\n{tips}", parse_mode=ParseMode.MARKDOWN)
        return

    prompts = {
        "edit_price": "Предложи 3 варианта цены: эконом, оптимальная и премиум. Объясни каждый.",
        "edit_desc":  "Перепиши описание более эмоционально и продающе. Сохрани характеристики.",
        "alt_titles": "Придумай 3 варианта заголовка до 50 символов с разными ключевыми словами.",
    }
    if data in prompts and state.get("original_prompt"):
        await query.message.reply_text("⏳ Генерирую...")
        full_prompt = f"Товар: {state['original_prompt']}\n\nЗадача: {prompts[data]}"
        result = await asyncio.to_thread(generate_card, full_prompt,
                                         state.get("image_b64"), state.get("image_mime"))
        if isinstance(result, dict):
            user_states[user_id]["last_card"] = result
            await query.message.reply_text(format_card(result),
                                           parse_mode=ParseMode.MARKDOWN,
                                           reply_markup=get_card_keyboard())
        else:
            await query.message.reply_text(result, parse_mode=ParseMode.MARKDOWN)

# ─────────────────────────────────────────────
#  ЗАПУСК
# ─────────────────────────────────────────────
def main():
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start",     start))
    app.add_handler(CommandHandler("status",    status_command))
    app.add_handler(CommandHandler("subscribe", subscribe_command))
    app.add_handler(CommandHandler("help",      help_command))
    app.add_handler(CommandHandler("tips",      tips_command))

    app.add_handler(PreCheckoutQueryHandler(pre_checkout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
    app.add_handler(MessageHandler(filters.PHOTO,              handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(handle_callback))

    logger.info("AvitoHelperBot запущен!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
