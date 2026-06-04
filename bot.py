# -*- coding: utf-8 -*-
import os
import json
import asyncio
import logging
import httpx
import sqlite3
import uuid
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
    PreCheckoutQueryHandler
)

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ.get("BOT_TOKEN", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
PAYMENT_PROVIDER_TOKEN = "390540012:LIVE:96921"
ADMIN_ID = 6466766416
FREE_LIMIT = 5
PRICE_WEEK = 99
PRICE_MONTH = 299
PRICE_FOREVER = 1490

user_states = {}
DB_PATH = "avito_bot.db"

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, format, *args):
        pass

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    logger.info("Web server started on port %d" % port)
    server.serve_forever()

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

def is_admin(user_id):
    return user_id == ADMIN_ID

def is_subscribed(user):
    if user["plan"] == "forever":
        return True
    if user["plan"] in ("week","month") and user["plan_until"]:
        until = datetime.strptime(user["plan_until"], "%Y-%m-%d %H:%M:%S")
        return datetime.utcnow() < until
    return False

def can_use(user):
    if is_admin(user["user_id"]):
        return True
    if is_subscribed(user):
        return True
    return user["free_used"] < FREE_LIMIT

def status_text(user):
    if is_admin(user["user_id"]):
        return "\U0001f451 Administrator — unlimited"
    if user["plan"] == "forever":
        return "\u267e Navsegda — unlimited"
    if user["plan"] in ("week","month") and user["plan_until"]:
        until = datetime.strptime(user["plan_until"], "%Y-%m-%d %H:%M:%S")
        if datetime.utcnow() < until:
            days_left = (until-datetime.utcnow()).days
            return "\U0001f4c5 Podpiska do %s — eshyo %d dn." % (until.strftime('%d.%m.%Y'), days_left)
    left = max(0, FREE_LIMIT-user["free_used"])
    return "\U0001f193 Besplatno: ostalos %d iz %d" % (left, FREE_LIMIT)

SYSTEM_PROMPT = """\u0422\u044b \u043f\u0440\u043e\u0444\u0435\u0441\u0441\u0438\u043e\u043d\u0430\u043b\u044c\u043d\u044b\u0439 \u043a\u043e\u043f\u0438\u0440\u0430\u0439\u0442\u0435\u0440 \u0434\u043b\u044f \u0410\u0432\u0438\u0442\u043e. \u041e\u0442\u0432\u0435\u0447\u0430\u0439 \u0442\u043e\u043b\u044c\u043a\u043e \u043d\u0430 \u0440\u0443\u0441\u0441\u043a\u043e\u043c \u044f\u0437\u044b\u043a\u0435.

\u0421\u043e\u0437\u0434\u0430\u0439 \u043a\u0430\u0440\u0442\u043e\u0447\u043a\u0443 \u0442\u043e\u0432\u0430\u0440\u0430 \u0422\u041e\u041b\u042c\u041a\u041e \u0432 \u0444\u043e\u0440\u043c\u0430\u0442\u0435 JSON \u0431\u0435\u0437 \u0434\u0440\u0443\u0433\u0438\u0445 \u0441\u043b\u043e\u0432:
{"title":"\u0437\u0430\u0433\u043e\u043b\u043e\u0432\u043e\u043a \u0434\u043e 50 \u0441\u0438\u043c\u0432\u043e\u043b\u043e\u0432","price":"\u0446\u0435\u043d\u0430 \u0447\u0438\u0441\u043b\u043e\u043c","price_hint":"\u043f\u043e\u0447\u0435\u043c\u0443 \u0442\u0430\u043a\u0430\u044f \u0446\u0435\u043d\u0430","category":"\u043a\u0430\u0442\u0435\u0433\u043e\u0440\u0438\u044f","subcategory":"\u043f\u043e\u0434\u043a\u0430\u0442\u0435\u0433\u043e\u0440\u0438\u044f","description":"\u043e\u043f\u0438\u0441\u0430\u043d\u0438\u0435 5-7 \u043f\u0440\u0435\u0434\u043b\u043e\u0436\u0435\u043d\u0438\u0439","condition":"\u041d\u043e\u0432\u043e\u0435 \u0438\u043b\u0438 \u041e\u0442\u043b\u0438\u0447\u043d\u043e\u0435 \u0438\u043b\u0438 \u0425\u043e\u0440\u043e\u0448\u0435\u0435","tags":["\u0442\u04511","\u0442\u04512","\u0442\u04513","\u0442\u04514","\u0442\u04515"],"seo_keywords":"\u043a\u043b\u044e\u0447\u0435\u0432\u044b\u0435 \u0441\u043b\u043e\u0432\u0430","call_to_action":"\u043f\u0440\u0438\u0437\u044b\u0432 \u043a \u0434\u0435\u0439\u0441\u0442\u0432\u0438\u044e","photo_tips":"3 \u0441\u043e\u0432\u0435\u0442\u0430 \u043f\u043e \u0444\u043e\u0442\u043e","warnings":"\u0447\u0442\u043e \u0443\u043a\u0430\u0437\u0430\u0442\u044c \u0447\u0442\u043e\u0431\u044b \u0438\u0437\u0431\u0435\u0436\u0430\u0442\u044c \u0441\u043f\u043e\u0440\u043e\u0432"}"""

def generate_card(prompt):
    try:
        response = httpx.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": "Bearer %s" % GROQ_API_KEY,
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
        logger.error("Groq error: %s" % e)
        return "Error. Try again."

def format_card(data):
    price = data.get("price", "")
    try:
        price_fmt = "%s \u20bd" % "{:,}".format(int(price)).replace(",", " ")
    except:
        price_fmt = "%s \u20bd" % price
    tags_str = "  ".join(["#%s" % t.replace(" ","_") for t in data.get("tags", [])])
    lines = []
    lines.append("\u2501"*22)
    lines.append("\u2705  KARTOCHKA GOTOVA")
    lines.append("\u2501"*22)
    lines.append("")
    lines.append("\U0001f4cc  %s" % data.get("title","").upper())
    lines.append("")
    lines.append("\U0001f4b0  Tsena:  %s" % price_fmt)
    lines.append("      %s" % data.get("price_hint",""))
    lines.append("")
    lines.append("\U0001f4c2  %s  \u203a  %s" % (data.get("category",""), data.get("subcategory","")))
    lines.append("\U0001f527  Sostoyaniye:  %s" % data.get("condition",""))
    lines.append("")
    lines.append("\U0001f4dd  OPISANIYE")
    lines.append("")
    lines.append(data.get("description",""))
    lines.append("")
    lines.append("\U0001f50d  %s" % data.get("seo_keywords",""))
    lines.append("")
    lines.append("\U0001f3f7  %s" % tags_str)
    lines.append("")
    lines.append("\U0001f4ac  %s" % data.get("call_to_action",""))
    if data.get("warnings"):
        lines.append("")
        lines.append("\u26a0\ufe0f  %s" % data["warnings"])
    if data.get("photo_tips"):
        lines.append("")
        lines.append("\U0001f4f8  SOVETY PO FOTO")
        lines.append(data["photo_tips"])
    lines.append("")
    lines.append("\u2501"*22)
    return "\n".join(lines)

def get_card_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("\u270f\ufe0f Izmenit tsenu", callback_data="edit_price"),
         InlineKeyboardButton("\U0001f4dd Drugoye opisaniye", callback_data="edit_desc")],
        [InlineKeyboardButton("\U0001f524 3 zagolovka", callback_data="alt_titles"),
         InlineKeyboardButton("\U0001f4f8 Sovety po foto", callback_data="photo_tips")],
        [InlineKeyboardButton("\U0001f4cb Skopirovat tekst", callback_data="copy_text"),
         InlineKeyboardButton("\U0001f501 Novyy tovar", callback_data="new_item")],
        [InlineKeyboardButton("\U0001f4c1 Moi obyavleniya", callback_data="my_ads")],
    ])

def get_subscribe_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("\u26a1 Nedelya — 99 rub", callback_data="pay_week")],
        [InlineKeyboardButton("\U0001f4c5 Mesyats — 299 rub", callback_data="pay_month")],
        [InlineKeyboardButton("\u267e Navsegda — 1490 rub", callback_data="pay_forever")],
    ])

def get_category_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("\U0001f4f1 Elektronika", callback_data="cat_electronics"),
         InlineKeyboardButton("\U0001f457 Odezhda", callback_data="cat_clothes")],
        [InlineKeyboardButton("\U0001f697 Avto", callback_data="cat_auto"),
         InlineKeyboardButton("\U0001f6cb Mebel", callback_data="cat_furniture")],
        [InlineKeyboardButton("\U0001f3cb Sport", callback_data="cat_sport"),
         InlineKeyboardButton("\U0001f9f8 Detskoe", callback_data="cat_kids")],
        [InlineKeyboardButton("\U0001f4e6 Drugoye", callback_data="cat_other")],
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(u.id, u.username or u.first_name)
    user = get_user(u.id)
    text = "\u2501"*22 + "\n\U0001f3ea  AVITO HELPER BOT\n" + "\u2501"*22
    text += "\n\nPrivet, %s! \U0001f44b\n\n" % u.first_name
    text += "Ya sozdayu prodayushchiye kartochki dlya Avito za 10 sekund.\n\n"
    text += "Status: %s\n\n" % status_text(user)
    text += "Vyberi kategoriyu ili opishi tovar tekstom \U0001f447"
    await update.message.reply_text(text,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("\U0001f680 Sozdat kartochku", callback_data="create_card")],
            [InlineKeyboardButton("\U0001f4c1 Moi obyavleniya", callback_data="my_ads"),
             InlineKeyboardButton("\U0001f464 Moy status", callback_data="my_status")],
            [InlineKeyboardButton("\U0001f4b3 Podpiska", callback_data="show_plans"),
             InlineKeyboardButton("\u2753 Pomoshch", callback_data="show_help")],
        ]))

async def myads_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(u.id, u.username or u.first_name)
    ads = get_user_ads(u.id)
    if not ads:
        await update.message.reply_text("Net obyavleniy. Opishi tovar!")
        return
    text = "\U0001f4c1 MOI OBYAVLENIYA\n\n"
    buttons = []
    for ad in ads:
        ad_id, title, price, category, created_at = ad
        date = created_at[:10]
        text += "\U0001f4cc %s\n\U0001f4b0 %s rub | \U0001f4c5 %s\n\n" % (title, price, date)
        buttons.append([InlineKeyboardButton("\U0001f4cc %s" % title[:35], callback_data="show_ad_%d" % ad_id)])
    buttons.append([InlineKeyboardButton("\U0001f680 Sozdat novoye", callback_data="new_item")])
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons))

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not is_admin(u.id):
        await update.message.reply_text("No access.")
        return
    total_users, total_ads, total_subs, total_payments = get_stats()
    text = "STATISTIKA:\nPolzovateley: %d\nKartochek: %d\nPodpischikov: %d\nOplat: %d" % (
        total_users, total_ads, total_subs, total_payments)
    await update.message.reply_text(text)

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(u.id, u.username or u.first_name)
    user = get_user(u.id)
    ads = get_user_ads(u.id, limit=100)
    text = "Status: %s\nKartochek sozdano: %d" % (status_text(user), len(ads))
    kb = None if (is_admin(u.id) or is_subscribed(user)) else get_subscribe_keyboard()
    await update.message.reply_text(text, reply_markup=kb)

async def subscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = "TARIFY:\nBesplatno: %d kartochek\nNedelya: %d rub\nMesyats: %d rub\nNavsegda: %d rub" % (
        FREE_LIMIT, PRICE_WEEK, PRICE_MONTH, PRICE_FOREVER)
    await update.message.reply_text(text, reply_markup=get_subscribe_keyboard())

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = "KAK POLZOVATSYA:\n1. Vyberi kategoriyu\n2. Opishi tovar\n3. Poluchi kartochku\n\n/myads /status /subscribe /tips"
    await update.message.reply_text(text)

async def tips_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = "SOVETY:\nFoto: minimum 5 pri dnevnom svete\nTsena: ostavь 10-15% na torg\nOtvechay bystro"
    await update.message.reply_text(text)

async def _process_and_reply(update, user_id, prompt):
    user = get_user(user_id)
    if not can_use(user):
        await update.message.reply_text("Limit ischerpan. Oformi podpisku!", reply_markup=get_subscribe_keyboard())
        return
    if not is_admin(user_id) and not is_subscribed(user):
        increment_usage(user_id)
        user = get_user(user_id)
        left = max(0, FREE_LIMIT - user["free_used"])
        await update.message.reply_text("Sozdayu kartochku... Ostalos besplatnykh: %d" % left)
    else:
        await update.message.reply_text("Sozdayu kartochku...")
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
        await update.message.reply_text("Foto polucheno! Opishi tovar tekstom.")

async def send_invoice(query, plan):
    user_id = query.from_user.id
    plans = {
        "week": ("Nedelya", "7 dney bez limita", PRICE_WEEK),
        "month": ("Mesyats", "30 dney bez limita", PRICE_MONTH),
        "forever": ("Navsegda", "Bez ogranicheniy", PRICE_FOREVER),
    }
    title, description, price = plans[plan]
    amount = price * 100
    payload = "%s_%d_%s" % (plan, user_id, uuid.uuid4().hex[:8])
    save_payment(payload, user_id, plan, price)
    await query.message.reply_invoice(
        title=title, description=description, payload=payload,
        provider_token=PAYMENT_PROVIDER_TOKEN, currency="RUB",
        prices=[LabeledPrice(title, amount)],
        need_name=False, need_email=False, need_phone_number=False)

async def pre_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.pre_checkout_query.answer(ok=True)

async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payload = update.message.successful_payment.invoice_payload
    user_id = update.effective_user.id
    plan = payload.split("_")[0]
    confirm_payment(payload)
    activate_plan(user_id, plan)
    await update.message.reply_text("Oplata proshla! Podpiska aktivirovana.")

CATEGORY_PROMPTS = {
    "cat_electronics": "Elektronika. Opishi: model, kharakteristiki, sostoyaniye.",
    "cat_clothes": "Odezhda. Opishi: brend, razmer, tsvet, sostoyaniye.",
    "cat_auto": "Avto. Opishi: marka, god, probeg, sostoyaniye.",
    "cat_furniture": "Mebel. Opishi: razmery, material, sostoyaniye.",
    "cat_sport": "Sport. Opishi: vid sporta, model, sostoyaniye.",
    "cat_kids": "Detskiye tovary. Opishi: vozrast, sostoyaniye.",
    "cat_other": "Opishi tovar podrobno.",
}

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data
    state = user_states.get(user_id, {})
    ensure_user(user_id, query.from_user.username or query.from_user.first_name)

    if data == "create_card":
        await query.message.reply_text("Vyberi kategoriyu:", reply_markup=get_category_keyboard())
    elif data in CATEGORY_PROMPTS:
        hint = CATEGORY_PROMPTS[data]
        user_states[user_id] = {"category_hint": hint}
        await query.message.reply_text("%s\n\nOpishi tovar:" % hint)
    elif data == "show_help":
        await query.message.reply_text("Opishi tovar — sozdam kartochku!\n/myads /status /subscribe")
    elif data == "my_status":
        user = get_user(user_id)
        ads = get_user_ads(user_id, limit=100)
        kb = None if (is_admin(user_id) or is_subscribed(user)) else get_subscribe_keyboard()
        await query.message.reply_text("Status: %s\nKartochek: %d" % (status_text(user), len(ads)), reply_markup=kb)
    elif data == "my_ads":
        ads = get_user_ads(user_id)
        if not ads:
            await query.message.reply_text("Net obyavleniy. Opishi tovar!")
            return
        text = "MOI OBYAVLENIYA:\n\n"
        buttons = []
        for ad in ads:
            ad_id, title, price, category, created_at = ad
            text += "%s — %s rub\n" % (title, price)
            buttons.append([InlineKeyboardButton(title[:35], callback_data="show_ad_%d" % ad_id)])
        buttons.append([InlineKeyboardButton("Sozdat novoye", callback_data="new_item")])
        await query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons))
    elif data.startswith("show_ad_"):
        ad_id = int(data.replace("show_ad_", ""))
        card = get_ad_by_id(ad_id)
        if card:
            user_states[user_id] = {"last_card": card, "original_prompt": card.get("title","")}
            await query.message.reply_text(format_card(card), reply_markup=get_card_keyboard())
        else:
            await query.message.reply_text("Obyavleniye ne naydeno.")
    elif data == "show_plans":
        await query.message.reply_text(
            "TARIFY:\nBesplatno: %d kartochek\nNedelya: %d rub\nMesyats: %d rub\nNavsegda: %d rub" % (
                FREE_LIMIT, PRICE_WEEK, PRICE_MONTH, PRICE_FOREVER),
            reply_markup=get_subscribe_keyboard())
    elif data == "pay_week":
        await send_invoice(query, "week")
    elif data == "pay_month":
        await send_invoice(query, "month")
    elif data == "pay_forever":
        await send_invoice(query, "forever")
    elif data == "new_item":
        await query.message.reply_text("Vyberi kategoriyu:", reply_markup=get_category_keyboard())
    elif data == "copy_text" and state.get("last_card"):
        card = state["last_card"]
        text = "%s\n\n%s\n\n%s" % (card.get("title",""), card.get("description",""), card.get("call_to_action",""))
        await query.message.reply_text("Kopiruй tekst:\n\n%s" % text)
    elif data == "photo_tips" and state.get("last_card"):
        tips = state["last_card"].get("photo_tips", "Sovety nedostupny")
        await query.message.reply_text("SOVETY PO FOTO:\n\n%s" % tips)
    elif data in ["edit_price","edit_desc","alt_titles"] and state.get("original_prompt"):
        prompts = {
            "edit_price": "Predlozhi 3 varianta tseny: ekonom, optimalnaya, premium.",
            "edit_desc": "Pereishi opisaniye emotsionalnee. Sokhrani kharakteristiki.",
            "alt_titles": "Pridumaй 3 varianta zagolovka do 50 simvolov.",
        }
        await query.message.reply_text("Generiruyu varianty...")
        result = await asyncio.to_thread(generate_card,
            "Tovar: %s\n\n%s" % (state["original_prompt"], prompts[data]))
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
    logger.info("Bot started!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    web_thread = threading.Thread(target=run_web_server, daemon=True)
    web_thread.start()
    main()
