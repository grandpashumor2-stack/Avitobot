import os
import json
import asyncio
import logging
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

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = "8525104782:AAHkKpIBjhYyg9x4bNgEL8A9WKE6RVwnzDA"
GROQ_API_KEY = "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"
PAYMENT_PROVIDER_TOKEN = "390540012:LIVE:96921"
FREE_LIMIT = 5
PRICE_WEEK = 99
PRICE_MONTH = 299
PRICE_FOREVER = 1490

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
    cur.execute("""CREATE TABLE IF NOT EXISTS ads (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, title TEXT, price TEXT,
        category TEXT, description TEXT, card_json TEXT,
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

def reset_free_usage(user_id):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("UPDATE users SET free_used=0 WHERE user_id=?", (user_id,))
    con.commit()
    con.close()

def increment_usage(user_id):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("UPDATE users SET free_used=free_used+1 WHERE user_id=?", (user_id,))
    con.commit()
    con.close()

def save_ad(user_id, card):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""INSERT INTO ads (user_id,title,price,category,description,card_json)
        VALUES (?,?,?,?,?,?)""",
        (user_id, card.get("title",""), card.get("price",""),
         card.get("category",""), card.get("description",""),
         json.dumps(card, ensure_ascii=False)))
    con.commit()
    con.close()

def get_user_ads(user_id, limit=5):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT id,title,price,category,created_at FROM ads WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
                (user_id, limit))
    rows = cur.fetchall()
    con.close()
    return rows

def get_ad_by_id(ad_id):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT card_json FROM ads WHERE id=?", (ad_id,))
    row = cur.fetchone()
    con.close()
    if row:
        return json.loads(row[0])
    return None

def get_stats():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT COUNT(*) FROM users")
    total_users = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM ads")
    total_ads = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM users WHERE plan != 'free'")
    total_subs = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM payments WHERE status='paid'")
    total_payments = cur.fetchone()[0]
    con.close()
    return total_users, total_ads, total_subs, total_payments

def activate_plan(user_id, plan):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    if plan == "week":
        until = (datetime.utcnow()+timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
        cur.execute("UPDATE users SET plan='week',plan_until=? WHERE user_id=?", (until,user_id))
    elif plan == "month":
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
    if user["plan"] in ("week","month") and user["plan_until"]:
        until = datetime.strptime(user["plan_until"], "%Y-%m-%d %H:%M:%S")
        return datetime.utcnow() < until
    return False

def can_use(user):
    if is_subscribed(user):
        return True
    return user["free_used"] < FREE_LIMIT

def status_text(user):
    if user["plan"] == "forever":
        return "♾ Навсегда — безлимит"
    if user["plan"] in ("week","month") and user["plan_until"]:
        until = datetime.strptime(user["plan_until"], "%Y-%m-%d %H:%M:%S")
        if datetime.utcnow() < until:
            days_left = (until-datetime.utcnow()).days
            return f"📅 Подписка до {until.strftime('%d.%m.%Y')} — ещё {days_left} дн."
    left = max(0, FREE_LIMIT-user["free_used"])
    return f"🆓 Бесплатно: осталось {left} из {FREE_LIMIT}"

SYSTEM_PROMPT = """Ты профессиональный копирайтер для Авито. Отвечай только на русском языке.

Создай карточку товара ТОЛЬКО в формате JSON без других слов:
{"title":"заголовок до 50 символов","price":"цена числом","price_hint":"почему такая цена","category":"категория","subcategory":"подкатегория","description":"описание 5-7 предложений","condition":"Новое или Отличное или Хорошее","tags":["тег1","тег2","тег3","тег4","тег5"],"seo_keywords":"ключевые слова","call_to_action":"призыв к действию","photo_tips":"3 совета по фото","warnings":"что указать чтобы избежать споров"}"""

def generate_card(prompt):
    try:
        response = httpx.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "llama-3.3-70b-versatile",
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt}
                ],
                "max_tokens": 1200,
                "temperature": 0.7,
            },
            timeout=30
        )
        data = response.json()
        raw = data["choices"][0]["message"]["content"].strip()
        raw = raw.replace("```json","").replace("```","").strip()
        try:
            return json.loads(raw)
        except:
            if "{" in raw:
                start = raw.find("{")
                end = raw.rfind("}") + 1
                try:
                    return json.loads(raw[start:end])
                except:
                    pass
            return raw
    except Exception as e:
        logger.error(f"Groq error: {e}")
        return "Ошибка генерации. Попробуй ещё раз."

def format_card(data):
    price = data.get("price", "")
    try:
        price_fmt = f"{int(price):,}".replace(",", " ") + " ₽"
    except:
        price_fmt = f"{price} ₽"
    tags_str = "  ".join([f"#{t.replace(' ','_')}" for t in data.get("tags", [])])
    lines = []
    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("✅  КАРТОЧКА ГОТОВА")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("")
    lines.append(f"📌  {data.get('title','').upper()}")
    lines.append("")
    lines.append(f"💰  Цена:  {price_fmt}")
    lines.append(f"      {data.get('price_hint','')}")
    lines.append("")
    lines.append(f"📂  {data.get('category','')}  ›  {data.get('subcategory','')}")
    lines.append(f"🔧  Состояние:  {data.get('condition','')}")
    lines.append("")
    lines.append("─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─")
    lines.append("📝  ОПИСАНИЕ")
    lines.append("─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─")
    lines.append("")
    lines.append(data.get('description',''))
    lines.append("")
    lines.append(f"🔍  {data.get('seo_keywords','')}")
    lines.append("")
    lines.append(f"🏷  {tags_str}")
    lines.append("")
    lines.append(f"💬  {data.get('call_to_action','')}")
    if data.get("warnings"):
        lines.append("")
        lines.append("─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─")
        lines.append(f"⚠️  {data['warnings']}")
    if data.get("photo_tips"):
        lines.append("")
        lines.append("─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─")
        lines.append("📸  СОВЕТЫ ПО ФОТО")
        lines.append(data['photo_tips'])
    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    return "\n".join(lines)

def get_card_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Изменить цену", callback_data="edit_price"),
         InlineKeyboardButton("📝 Другое описание", callback_data="edit_desc")],
        [InlineKeyboardButton("🔤 3 заголовка", callback_data="alt_titles"),
         InlineKeyboardButton("📸 Советы по фото", callback_data="photo_tips")],
        [InlineKeyboardButton("📋 Копировать текст", callback_data="copy_text"),
         InlineKeyboardButton("🔁 Новый товар", callback_data="new_item")],
        [InlineKeyboardButton("📁 Мои объявления", callback_data="my_ads")],
    ])

def get_subscribe_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚡ Неделя — 99 ₽", callback_data="pay_week")],
        [InlineKeyboardButton("📅 Месяц — 299 ₽", callback_data="pay_month")],
        [InlineKeyboardButton("♾ Навсегда — 1 490 ₽", callback_data="pay_forever")],
    ])

def get_category_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📱 Электроника", callback_data="cat_electronics"),
         InlineKeyboardButton("👗 Одежда", callback_data="cat_clothes")],
        [InlineKeyboardButton("🚗 Авто", callback_data="cat_auto"),
         InlineKeyboardButton("🛋 Мебель", callback_data="cat_furniture")],
        [InlineKeyboardButton("🏋 Спорт", callback_data="cat_sport"),
         InlineKeyboardButton("🧸 Детское", callback_data="cat_kids")],
        [InlineKeyboardButton("📦 Другое", callback_data="cat_other")],
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(u.id, u.username or u.first_name)
    reset_free_usage(u.id)
    user = get_user(u.id)
    text = (
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🏪  AVITO HELPER BOT\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Привет, {u.first_name}! 👋\n\n"
        f"Я создаю продающие карточки\n"
        f"для Авито за 10 секунд.\n\n"
        f"📝  Пишу продающие описания\n"
        f"💰  Рекомендую цену\n"
        f"🏷  Подбираю теги и ключевые слова\n"
        f"📁  Сохраняю историю объявлений\n\n"
        f"─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─\n"
        f"Твой статус:  {status_text(user)}\n"
        f"─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─\n\n"
        f"Выбери категорию или просто\n"
        f"опиши товар текстом 👇"
    )
    await update.message.reply_text(text,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🚀 Создать карточку", callback_data="create_card")],
            [InlineKeyboardButton("📁 Мои объявления", callback_data="my_ads"),
             InlineKeyboardButton("👤 Мой статус", callback_data="my_status")],
            [InlineKeyboardButton("💳 Подписка", callback_data="show_plans"),
             InlineKeyboardButton("❓ Помощь", callback_data="show_help")],
        ]))

async def myads_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(u.id, u.username or u.first_name)
    ads = get_user_ads(u.id)
    if not ads:
        await update.message.reply_text("📁  МОИ ОБЪЯВЛЕНИЯ\n\nУ тебя пока нет сохранённых объявлений.\n\nОпиши товар и я создам карточку!")
        return
    text = "📁  МОИ ОБЪЯВЛЕНИЯ\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
    buttons = []
    for ad in ads:
        ad_id, title, price, category, created_at = ad
        date = created_at[:10]
        text += f"📌 {title}\n💰 {price} ₽  |  📅 {date}\n\n"
        buttons.append([InlineKeyboardButton(f"📌 {title[:35]}", callback_data=f"show_ad_{ad_id}")])
    buttons.append([InlineKeyboardButton("🚀 Создать новое", callback_data="new_item")])
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons))

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    total_users, total_ads, total_subs, total_payments = get_stats()
    text = (
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊  СТАТИСТИКА БОТА\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👥  Пользователей:       {total_users}\n"
        f"📝  Карточек создано:  {total_ads}\n"
        f"💳  Подписчиков:         {total_subs}\n"
        f"💰  Оплат:                    {total_payments}\n"
        f"\n━━━━━━━━━━━━━━━━━━━━━━"
    )
    await update.message.reply_text(text)

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(u.id, u.username or u.first_name)
    user = get_user(u.id)
    ads = get_user_ads(u.id, limit=100)
    text = (
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤  МОЙ АККАУНТ\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Статус:  {status_text(user)}\n"
        f"Карточек создано:  {len(ads)}\n"
        f"\n━━━━━━━━━━━━━━━━━━━━━━"
    )
    kb = None if is_subscribed(user) else get_subscribe_keyboard()
    await update.message.reply_text(text, reply_markup=kb)

async def subscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💳  ТАРИФЫ\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🆓  Бесплатно\n"
        f"      {FREE_LIMIT} карточек для знакомства\n\n"
        f"⚡  Неделя  —  {PRICE_WEEK} ₽\n"
        f"      Безлимит на 7 дней\n\n"
        f"📅  Месяц  —  {PRICE_MONTH} ₽\n"
        f"      Безлимит на 30 дней\n\n"
        f"♾  Навсегда  —  {PRICE_FOREVER} ₽\n"
        f"      Безлимит без ограничений\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Оплата через ЮКассу 🔒\n"
        f"Карты, СБП, ЮMoney"
    )
    await update.message.reply_text(text, reply_markup=get_subscribe_keyboard())

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"❓  КАК ПОЛЬЗОВАТЬСЯ\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"1️⃣  Выбери категорию товара\n"
        f"2️⃣  Опиши товар текстом\n"
        f"3️⃣  Получи готовую карточку\n"
        f"4️⃣  Используй кнопки для правок\n\n"
        f"─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─\n"
        f"📌  Команды:\n\n"
        f"/myads — мои объявления\n"
        f"/status — мой тариф\n"
        f"/subscribe — подписка\n"
        f"/tips — советы по продажам\n"
        f"/admin — статистика\n"
        f"\n━━━━━━━━━━━━━━━━━━━━━━"
    )
    await update.message.reply_text(text)

async def tips_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💡  СОВЕТЫ ПО ПРОДАЖАМ\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📸  ФОТО\n"
        f"Минимум 5 фото при дневном свете\n"
        f"Покажи дефекты честно\n\n"
        f"📝  ОПИСАНИЕ\n"
        f"Укажи все характеристики\n"
        f"Напиши год покупки и причину продажи\n\n"
        f"💰  ЦЕНА\n"
        f"Оставь 10-15% на торг\n"
        f"Изучи похожие объявления\n\n"
        f"⚡  СКОРОСТЬ\n"
        f"Отвечай быстро\n"
        f"Предлагай доставку\n"
        f"\n━━━━━━━━━━━━━━━━━━━━━━"
    )
    await update.message.reply_text(text)

async def _process_and_reply(update, user_id, prompt):
    user = get_user(user_id)
    if not can_use(user):
        await update.message.reply_text(
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🚫  ЛИМИТ ИСЧЕРПАН\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Ты использовал все {FREE_LIMIT} бесплатных карточек.\n\n"
            f"Оформи подписку чтобы продолжить 👇",
            reply_markup=get_subscribe_keyboard())
        return
    if not is_subscribed(user):
        increment_usage(user_id)
        user = get_user(user_id)
        left = max(0, FREE_LIMIT - user["free_used"])
        if left > 0:
            await update.message.reply_text(f"⏳  Создаю карточку...\n🆓  Осталось бесплатных: {left}")
        else:
            await update.message.reply_text("⏳  Создаю карточку...\n⚠️  Это последняя бесплатная попытка!")
    else:
        await update.message.reply_text("⏳  Создаю карточку...")
    result = await asyncio.to_thread(generate_card, prompt)
    if isinstance(result, dict):
        save_ad(user_id, result)
        user_states[user_id] = {"last_card": result, "original_prompt": prompt}
        await update.message.reply_text(format_card(result), reply_markup=get_card_keyboard())
    else:
        await update.message.reply_text(str(result))

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(u.id, u.username or u.first_name)
    await _process_and_reply(update, u.id, update.message.text)

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(u.id, u.username or u.first_name)
    caption = update.message.caption or ""
    if caption:
        await _process_and_reply(update, u.id, caption)
    else:
        await update.message.reply_text(
            "📸  Фото получено!\n\n"
            "Опиши товар текстом и я создам карточку.\n"
            "Например: марка, модель, состояние, год.")

async def send_invoice(query, plan):
    user_id = query.from_user.id
    if plan == "week":
        title = "Подписка на неделю"
        description = "Безлимитные карточки на 7 дней"
        amount = PRICE_WEEK * 100
        payload = f"week_{user_id}_{uuid.uuid4().hex[:8]}"
    elif plan == "month":
        title = "Подписка на месяц"
        description = "Безлимитные карточки на 30 дней"
        amount = PRICE_MONTH * 100
        payload = f"month_{user_id}_{uuid.uuid4().hex[:8]}"
    else:
        title = "Подписка навсегда"
        description = "Безлимитные карточки без ограничений"
        amount = PRICE_FOREVER * 100
        payload = f"forever_{user_id}_{uuid.uuid4().hex[:8]}"
    save_payment(payload, user_id, plan, amount // 100)
    await query.message.reply_invoice(
        title=title,
        description=description,
        payload=payload,
        provider_token=PAYMENT_PROVIDER_TOKEN,
        currency="RUB",
        prices=[LabeledPrice(title, amount)],
        need_name=False,
        need_email=False,
        need_phone_number=False,
    )

async def pre_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.pre_checkout_query.answer(ok=True)

async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payload = update.message.successful_payment.invoice_payload
    user_id = update.effective_user.id
    if payload.startswith("week_"):
        plan = "week"
        label = "на неделю"
    elif payload.startswith("month_"):
        plan = "month"
        label = "на месяц"
    else:
        plan = "forever"
        label = "навсегда"
    confirm_payment(payload)
    activate_plan(user_id, plan)
    user = get_user(user_id)
    await update.message.reply_text(
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎉  ОПЛАТА ПРОШЛА!\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"✅  Подписка {label} активирована\n\n"
        f"Статус:  {status_text(user)}\n\n"
        f"Создавай карточки без ограничений! 🚀")

CATEGORY_PROMPTS = {
    "cat_electronics": "Электроника и гаджеты. Опиши: модель, характеристики, состояние, комплектация.",
    "cat_clothes": "Одежда и обувь. Опиши: бренд, размер, цвет, состояние.",
    "cat_auto": "Авто и мото. Опиши: марка, год, пробег, состояние.",
    "cat_furniture": "Мебель и интерьер. Опиши: размеры, материал, состояние.",
    "cat_sport": "Спорт и отдых. Опиши: вид спорта, модель, состояние.",
    "cat_kids": "Детские товары. Опиши: возраст, состояние, комплектация.",
    "cat_other": "Опиши свой товар подробно: название, состояние, характеристики.",
}

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data
    state = user_states.get(user_id, {})
    ensure_user(user_id, query.from_user.username or query.from_user.first_name)

    if data == "create_card":
        await query.message.reply_text("📦  Выбери категорию товара:", reply_markup=get_category_keyboard())
    elif data in CATEGORY_PROMPTS:
        hint = CATEGORY_PROMPTS[data]
        user_states[user_id] = {"category_hint": hint}
        await query.message.reply_text(f"✏️  {hint}\n\nНапиши описание товара 👇")
    elif data == "show_help":
        await query.message.reply_text("❓  Опиши товар текстом — создам карточку!\n\n/myads — мои объявления\n/status — мой тариф\n/admin — статистика")
    elif data == "my_status":
        user = get_user(user_id)
        ads = get_user_ads(user_id, limit=100)
        kb = None if is_subscribed(user) else get_subscribe_keyboard()
        await query.message.reply_text(
            f"━━━━━━━━━━━━━━━━━━━━━━\n👤  МОЙ АККАУНТ\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Статус:  {status_text(user)}\nКарточек создано:  {len(ads)}\n\n━━━━━━━━━━━━━━━━━━━━━━",
            reply_markup=kb)
    elif data == "my_ads":
        ads = get_user_ads(user_id)
        if not ads:
            await query.message.reply_text("📁  МОИ ОБЪЯВЛЕНИЯ\n\nУ тебя пока нет объявлений.\n\nОпиши товар — создам карточку!")
            return
        text = "📁  МОИ ОБЪЯВЛЕНИЯ\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
        buttons = []
        for ad in ads:
            ad_id, title, price, category, created_at = ad
            date = created_at[:10]
            text += f"📌 {title}\n💰 {price} ₽  |  📅 {date}\n\n"
            buttons.append([InlineKeyboardButton(f"📌 {title[:35]}", callback_data=f"show_ad_{ad_id}")])
        buttons.append([InlineKeyboardButton("🚀 Создать новое", callback_data="new_item")])
        await query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons))
    elif data.startswith("show_ad_"):
        ad_id = int(data.replace("show_ad_", ""))
        card = get_ad_by_id(ad_id)
        if card:
            user_states[user_id] = {"last_card": card, "original_prompt": card.get("title","")}
            await query.message.reply_text(format_card(card), reply_markup=get_card_keyboard())
        else:
            await query.message.reply_text("Объявление не найдено.")
    elif data == "show_plans":
        await subscribe_command(query, context)
    elif data == "pay_week":
        await send_invoice(query, "week")
    elif data == "pay_month":
        await send_invoice(query, "month")
    elif data == "pay_forever":
        await send_invoice(query, "forever")
    elif data == "new_item":
        await query.message.reply_text("📦  Выбери категорию товара:", reply_markup=get_category_keyboard())
    elif data == "copy_text" and state.get("last_card"):
        card = state["last_card"]
        text = f"{card.get('title','')}\n\n{card.get('description','')}\n\n{card.get('call_to_action','')}"
        await query.message.reply_text(f"📋  Копируй текст:\n\n{text}")
    elif data == "photo_tips" and state.get("last_card"):
        tips = state["last_card"].get("photo_tips", "Советы недоступны")
        await query.message.reply_text(f"━━━━━━━━━━━━━━━━━━━━━━\n📸  СОВЕТЫ ПО ФОТО\n━━━━━━━━━━━━━━━━━━━━━━\n\n{tips}")
    elif data in ["edit_price","edit_desc","alt_titles"] and state.get("original_prompt"):
        prompts = {
            "edit_price": "Предложи 3 варианта цены: эконом, оптимальная и премиум. Объясни каждый.",
            "edit_desc": "Перепиши описание более эмоционально и продающе. Сохрани все характеристики.",
            "alt_titles": "Придумай 3 варианта заголовка до 50 символов с разными ключевыми словами.",
        }
        await query.message.reply_text("⏳  Генерирую варианты...")
        result = await asyncio.to_thread(generate_card,
            f"Товар: {state['original_prompt']}\n\n{prompts[data]}")
        if isinstance(result, dict):
            save_ad(user_id, result)
            user_states[user_id]["last_card"] = result
            await query.message.reply_text(format_card(result), reply_markup=get_card_keyboard())
        else:
            await query.message.reply_text(str(result))

def main():
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("myads", myads_command))
    app.add_handler(CommandHandler("admin", admin_command))
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
