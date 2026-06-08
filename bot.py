# -*- coding: utf-8 -*-
import os
import json
import asyncio
import logging
import httpx
import sqlite3
import uuid
import threading
import time
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
SERVICE_URL = os.environ.get("SERVICE_URL", "https://avitobot-518b.onrender.com")

user_states = {}
DB_PATH = "/tmp/avito_bot.db"

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK - Bot is running!")
    def log_message(self, format, *args):
        pass

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    logger.info("Web server started on port %d" % port)
    server.serve_forever()

def self_ping():
    """Пингует себя каждые 4 минуты чтобы не засыпать"""
    while True:
        time.sleep(60)  # 1 минута
        try:
            httpx.get(SERVICE_URL, timeout=10)
            logger.info("Self-ping OK")
        except Exception as e:
            logger.warning("Self-ping failed: %s" % e)

def init_db():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        free_used INTEGER DEFAULT 0,
        plan TEXT DEFAULT 'free',
        plan_until TEXT DEFAULT NULL,
        created_at TEXT DEFAULT (datetime('now')))""")
    cur.execute("""CREATE TABLE IF NOT EXISTS payments (
        payment_id TEXT PRIMARY KEY,
        user_id INTEGER,
        plan TEXT,
        amount INTEGER,
        status TEXT DEFAULT 'pending',
        created_at TEXT DEFAULT (datetime('now')))""")
    cur.execute("""CREATE TABLE IF NOT EXISTS ads (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        title TEXT,
        price TEXT,
        category TEXT,
        description TEXT,
        card_json TEXT,
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
    cur.execute("INSERT OR IGNORE INTO users (user_id, username, free_used) VALUES (?, ?, 0)",
                (user_id, username))
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
        return "\U0001f451 \u0410\u0434\u043c\u0438\u043d\u0438\u0441\u0442\u0440\u0430\u0442\u043e\u0440 \u2014 \u0431\u0435\u0437\u043b\u0438\u043c\u0438\u0442"
    if user["plan"] == "forever":
        return "\u267e \u041d\u0430\u0432\u0441\u0435\u0433\u0434\u0430 \u2014 \u0431\u0435\u0437\u043b\u0438\u043c\u0438\u0442"
    if user["plan"] in ("week","month") and user["plan_until"]:
        until = datetime.strptime(user["plan_until"], "%Y-%m-%d %H:%M:%S")
        if datetime.utcnow() < until:
            days_left = (until-datetime.utcnow()).days
            return "\U0001f4c5 \u041f\u043e\u0434\u043f\u0438\u0441\u043a\u0430 \u0434\u043e %s \u2014 \u0435\u0449\u0451 %d \u0434\u043d." % (until.strftime('%d.%m.%Y'), days_left)
    left = max(0, FREE_LIMIT - user["free_used"])
    return "\U0001f193 \u0411\u0435\u0441\u043f\u043b\u0430\u0442\u043d\u043e: \u043e\u0441\u0442\u0430\u043b\u043e\u0441\u044c %d \u0438\u0437 %d" % (left, FREE_LIMIT)

SYSTEM_PROMPT = (
    "\u0422\u044b \u043f\u0440\u043e\u0444\u0435\u0441\u0441\u0438\u043e\u043d\u0430\u043b\u044c\u043d\u044b\u0439 "
    "\u043a\u043e\u043f\u0438\u0440\u0430\u0439\u0442\u0435\u0440 \u0434\u043b\u044f \u0410\u0432\u0438\u0442\u043e. "
    "\u041e\u0442\u0432\u0435\u0447\u0430\u0439 \u0442\u043e\u043b\u044c\u043a\u043e \u043d\u0430 \u0440\u0443\u0441\u0441\u043a\u043e\u043c \u044f\u0437\u044b\u043a\u0435.\n\n"
    "\u0421\u043e\u0437\u0434\u0430\u0439 \u043a\u0430\u0440\u0442\u043e\u0447\u043a\u0443 \u0422\u041e\u041b\u042c\u041a\u041e \u0432 JSON \u0444\u043e\u0440\u043c\u0430\u0442\u0435:\n"
    '{"title":"\u0437\u0430\u0433\u043e\u043b\u043e\u0432\u043e\u043a \u0434\u043e 50 \u0441\u0438\u043c\u0432\u043e\u043b\u043e\u0432",'
    '"price":"\u0446\u0435\u043d\u0430 \u0447\u0438\u0441\u043b\u043e\u043c",'
    '"price_hint":"\u043f\u043e\u0447\u0435\u043c\u0443 \u0442\u0430\u043a\u0430\u044f \u0446\u0435\u043d\u0430",'
    '"category":"\u043a\u0430\u0442\u0435\u0433\u043e\u0440\u0438\u044f",'
    '"subcategory":"\u043f\u043e\u0434\u043a\u0430\u0442\u0435\u0433\u043e\u0440\u0438\u044f",'
    '"description":"\u043e\u043f\u0438\u0441\u0430\u043d\u0438\u0435 5-7 \u043f\u0440\u0435\u0434\u043b\u043e\u0436\u0435\u043d\u0438\u0439",'
    '"condition":"\u041d\u043e\u0432\u043e\u0435 \u0438\u043b\u0438 \u041e\u0442\u043b\u0438\u0447\u043d\u043e\u0435 \u0438\u043b\u0438 \u0425\u043e\u0440\u043e\u0448\u0435\u0435",'
    '"tags":["\u0442\u04511","\u0442\u04512","\u0442\u04513","\u0442\u04514","\u0442\u04515"],'
    '"seo_keywords":"\u043a\u043b\u044e\u0447\u0435\u0432\u044b\u0435 \u0441\u043b\u043e\u0432\u0430",'
    '"call_to_action":"\u043f\u0440\u0438\u0437\u044b\u0432 \u043a \u0434\u0435\u0439\u0441\u0442\u0432\u0438\u044e",'
    '"photo_tips":"3 \u0441\u043e\u0432\u0435\u0442\u0430 \u043f\u043e \u0444\u043e\u0442\u043e",'
    '"warnings":"\u0447\u0442\u043e \u0443\u043a\u0430\u0437\u0430\u0442\u044c \u0447\u0442\u043e\u0431\u044b \u0438\u0437\u0431\u0435\u0436\u0430\u0442\u044c \u0441\u043f\u043e\u0440\u043e\u0432"}'
)

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
        return "\u041e\u0448\u0438\u0431\u043a\u0430 \u0433\u0435\u043d\u0435\u0440\u0430\u0446\u0438\u0438. \u041f\u043e\u043f\u0440\u043e\u0431\u0443\u0439 \u0435\u0449\u0451 \u0440\u0430\u0437."

def format_card(data):
    price = data.get("price", "")
    try:
        price_fmt = "%s \u20bd" % "{:,}".format(int(price)).replace(",", " ")
    except:
        price_fmt = "%s \u20bd" % price
    tags_str = "  ".join(["#%s" % t.replace(" ","_") for t in data.get("tags", [])])
    sep = "\u2501" * 22
    lines = [
        sep,
        "\u2705  \u041a\u0410\u0420\u0422\u041e\u0427\u041a\u0410 \u0413\u041e\u0422\u041e\u0412\u0410",
        sep, "",
        "\U0001f4cc  %s" % data.get("title","").upper(), "",
        "\U0001f4b0  \u0426\u0435\u043d\u0430:  %s" % price_fmt,
        "      %s" % data.get("price_hint",""), "",
        "\U0001f4c2  %s  \u203a  %s" % (data.get("category",""), data.get("subcategory","")),
        "\U0001f527  \u0421\u043e\u0441\u0442\u043e\u044f\u043d\u0438\u0435:  %s" % data.get("condition",""), "",
        "\u2015" * 11,
        "\U0001f4dd  \u041e\u041f\u0418\u0421\u0410\u041d\u0418\u0415",
        "\u2015" * 11, "",
        data.get("description",""), "",
        "\U0001f50d  %s" % data.get("seo_keywords",""), "",
        "\U0001f3f7  %s" % tags_str, "",
        "\U0001f4ac  %s" % data.get("call_to_action",""),
    ]
    if data.get("warnings"):
        lines += ["", "\u2015"*11, "\u26a0\ufe0f  %s" % data["warnings"]]
    if data.get("photo_tips"):
        lines += ["", "\u2015"*11, "\U0001f4f8  \u0421\u041e\u0412\u0415\u0422\u042b \u041f\u041e \u0424\u041e\u0422\u041e", data["photo_tips"]]
    lines += ["", sep]
    return "\n".join(lines)

def get_card_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("\u270f\ufe0f \u0418\u0437\u043c\u0435\u043d\u0438\u0442\u044c \u0446\u0435\u043d\u0443", callback_data="edit_price"),
         InlineKeyboardButton("\U0001f4dd \u0414\u0440\u0443\u0433\u043e\u0435 \u043e\u043f\u0438\u0441\u0430\u043d\u0438\u0435", callback_data="edit_desc")],
        [InlineKeyboardButton("\U0001f524 3 \u0437\u0430\u0433\u043e\u043b\u043e\u0432\u043a\u0430", callback_data="alt_titles"),
         InlineKeyboardButton("\U0001f4f8 \u0421\u043e\u0432\u0435\u0442\u044b \u043f\u043e \u0444\u043e\u0442\u043e", callback_data="photo_tips")],
        [InlineKeyboardButton("\U0001f4cb \u041a\u043e\u043f\u0438\u0440\u043e\u0432\u0430\u0442\u044c \u0442\u0435\u043a\u0441\u0442", callback_data="copy_text"),
         InlineKeyboardButton("\U0001f501 \u041d\u043e\u0432\u044b\u0439 \u0442\u043e\u0432\u0430\u0440", callback_data="new_item")],
        [InlineKeyboardButton("\U0001f4c1 \u041c\u043e\u0438 \u043e\u0431\u044a\u044f\u0432\u043b\u0435\u043d\u0438\u044f", callback_data="my_ads")],
    ])

def get_subscribe_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("\u26a1 \u041d\u0435\u0434\u0435\u043b\u044f \u2014 99 \u20bd", callback_data="pay_week")],
        [InlineKeyboardButton("\U0001f4c5 \u041c\u0435\u0441\u044f\u0446 \u2014 299 \u20bd", callback_data="pay_month")],
        [InlineKeyboardButton("\u267e \u041d\u0430\u0432\u0441\u0435\u0433\u0434\u0430 \u2014 1490 \u20bd", callback_data="pay_forever")],
    ])

def get_category_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("\U0001f4f1 \u042d\u043b\u0435\u043a\u0442\u0440\u043e\u043d\u0438\u043a\u0430", callback_data="cat_electronics"),
         InlineKeyboardButton("\U0001f457 \u041e\u0434\u0435\u0436\u0434\u0430", callback_data="cat_clothes")],
        [InlineKeyboardButton("\U0001f697 \u0410\u0432\u0442\u043e", callback_data="cat_auto"),
         InlineKeyboardButton("\U0001f6cb \u041c\u0435\u0431\u0435\u043b\u044c", callback_data="cat_furniture")],
        [InlineKeyboardButton("\U0001f3cb \u0421\u043f\u043e\u0440\u0442", callback_data="cat_sport"),
         InlineKeyboardButton("\U0001f9f8 \u0414\u0435\u0442\u0441\u043a\u043e\u0435", callback_data="cat_kids")],
        [InlineKeyboardButton("\U0001f4e6 \u0414\u0440\u0443\u0433\u043e\u0435", callback_data="cat_other")],
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(u.id, u.username or u.first_name)
    user = get_user(u.id)
    sep = "\u2501" * 22
    text = (
        "%s\n\U0001f3ea  AVITO HELPER BOT\n%s\n\n"
        "\u041f\u0440\u0438\u0432\u0435\u0442, %s! \U0001f44b\n\n"
        "\u042f \u0441\u043e\u0437\u0434\u0430\u044e \u043f\u0440\u043e\u0434\u0430\u044e\u0449\u0438\u0435 \u043a\u0430\u0440\u0442\u043e\u0447\u043a\u0438 \u0434\u043b\u044f \u0410\u0432\u0438\u0442\u043e \u0437\u0430 10 \u0441\u0435\u043a\u0443\u043d\u0434.\n\n"
        "\U0001f4dd \u041f\u0438\u0448\u0443 \u043f\u0440\u043e\u0434\u0430\u044e\u0449\u0438\u0435 \u043e\u043f\u0438\u0441\u0430\u043d\u0438\u044f\n"
        "\U0001f4b0 \u0420\u0435\u043a\u043e\u043c\u0435\u043d\u0434\u0443\u044e \u0446\u0435\u043d\u0443\n"
        "\U0001f3f7 \u041f\u043e\u0434\u0431\u0438\u0440\u0430\u044e \u0442\u0435\u0433\u0438 \u0438 \u043a\u043b\u044e\u0447\u0435\u0432\u044b\u0435 \u0441\u043b\u043e\u0432\u0430\n\n"
        "\u0421\u0442\u0430\u0442\u0443\u0441: %s\n\n"
        "\u0412\u044b\u0431\u0435\u0440\u0438 \u043a\u0430\u0442\u0435\u0433\u043e\u0440\u0438\u044e \u0438\u043b\u0438 \u043e\u043f\u0438\u0448\u0438 \u0442\u043e\u0432\u0430\u0440 \U0001f447"
    ) % (sep, sep, u.first_name, status_text(user))
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("\U0001f680 \u0421\u043e\u0437\u0434\u0430\u0442\u044c \u043a\u0430\u0440\u0442\u043e\u0447\u043a\u0443", callback_data="create_card")],
        [InlineKeyboardButton("\U0001f4c1 \u041c\u043e\u0438 \u043e\u0431\u044a\u044f\u0432\u043b\u0435\u043d\u0438\u044f", callback_data="my_ads"),
         InlineKeyboardButton("\U0001f464 \u041c\u043e\u0439 \u0441\u0442\u0430\u0442\u0443\u0441", callback_data="my_status")],
        [InlineKeyboardButton("\U0001f4b3 \u041f\u043e\u0434\u043f\u0438\u0441\u043a\u0430", callback_data="show_plans"),
         InlineKeyboardButton("\u2753 \u041f\u043e\u043c\u043e\u0449\u044c", callback_data="show_help")],
    ]))

async def myads_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(u.id, u.username or u.first_name)
    ads = get_user_ads(u.id)
    if not ads:
        await update.message.reply_text(
            "\U0001f4c1 \u041c\u041e\u0418 \u041e\u0411\u042a\u042f\u0412\u041b\u0415\u041d\u0418\u042f\n\n"
            "\u0423 \u0442\u0435\u0431\u044f \u043f\u043e\u043a\u0430 \u043d\u0435\u0442 \u043e\u0431\u044a\u044f\u0432\u043b\u0435\u043d\u0438\u0439.\n\n"
            "\u041e\u043f\u0438\u0448\u0438 \u0442\u043e\u0432\u0430\u0440 \u0438 \u044f \u0441\u043e\u0437\u0434\u0430\u043c \u043a\u0430\u0440\u0442\u043e\u0447\u043a\u0443!")
        return
    text = "\U0001f4c1 \u041c\u041e\u0418 \u041e\u0411\u042a\u042f\u0412\u041b\u0415\u041d\u0418\u042f\n\n"
    buttons = []
    for ad in ads:
        ad_id, title, price, category, created_at = ad
        date = created_at[:10]
        text += "\U0001f4cc %s\n\U0001f4b0 %s \u20bd | \U0001f4c5 %s\n\n" % (title, price, date)
        buttons.append([InlineKeyboardButton("\U0001f4cc %s" % title[:35], callback_data="show_ad_%d" % ad_id)])
    buttons.append([InlineKeyboardButton("\U0001f680 \u0421\u043e\u0437\u0434\u0430\u0442\u044c \u043d\u043e\u0432\u043e\u0435", callback_data="new_item")])
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons))

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not is_admin(u.id):
        await update.message.reply_text("\u041d\u0435\u0442 \u0434\u043e\u0441\u0442\u0443\u043f\u0430.")
        return
    total_users, total_ads, total_subs, total_payments = get_stats()
    await update.message.reply_text(
        "\U0001f4ca \u0421\u0422\u0410\u0422\u0418\u0421\u0422\u0418\u041a\u0410\n\n"
        "\U0001f465 \u041f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u0442\u0435\u043b\u0435\u0439: %d\n"
        "\U0001f4dd \u041a\u0430\u0440\u0442\u043e\u0447\u0435\u043a: %d\n"
        "\U0001f4b3 \u041f\u043e\u0434\u043f\u0438\u0441\u0447\u0438\u043a\u043e\u0432: %d\n"
        "\U0001f4b0 \u041e\u043f\u043b\u0430\u0442: %d" % (total_users, total_ads, total_subs, total_payments))

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(u.id, u.username or u.first_name)
    user = get_user(u.id)
    ads = get_user_ads(u.id, limit=100)
    kb = None if (is_admin(u.id) or is_subscribed(user)) else get_subscribe_keyboard()
    await update.message.reply_text(
        "\U0001f464 \u041c\u041e\u0419 \u0410\u041a\u041a\u0410\u0423\u041d\u0422\n\n"
        "\u0421\u0442\u0430\u0442\u0443\u0441: %s\n"
        "\u041a\u0430\u0440\u0442\u043e\u0447\u0435\u043a \u0441\u043e\u0437\u0434\u0430\u043d\u043e: %d" % (status_text(user), len(ads)),
        reply_markup=kb)

async def subscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "\U0001f4b3 \u0422\u0410\u0420\u0418\u0424\u042b\n\n"
        "\U0001f193 \u0411\u0435\u0441\u043f\u043b\u0430\u0442\u043d\u043e: %d \u043a\u0430\u0440\u0442\u043e\u0447\u043a\u0438\n\n"
        "\u26a1 \u041d\u0435\u0434\u0435\u043b\u044f \u2014 %d \u20bd\n"
        "\U0001f4c5 \u041c\u0435\u0441\u044f\u0446 \u2014 %d \u20bd\n"
        "\u267e \u041d\u0430\u0432\u0441\u0435\u0433\u0434\u0430 \u2014 %d \u20bd" % (FREE_LIMIT, PRICE_WEEK, PRICE_MONTH, PRICE_FOREVER),
        reply_markup=get_subscribe_keyboard())

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "\u2753 \u041a\u0410\u041a \u041f\u041e\u041b\u042c\u0417\u041e\u0412\u0410\u0422\u042c\u0421\u042f\n\n"
        "1\ufe0f\u20e3 \u0412\u044b\u0431\u0435\u0440\u0438 \u043a\u0430\u0442\u0435\u0433\u043e\u0440\u0438\u044e\n"
        "2\ufe0f\u20e3 \u041e\u043f\u0438\u0448\u0438 \u0442\u043e\u0432\u0430\u0440\n"
        "3\ufe0f\u20e3 \u041f\u043e\u043b\u0443\u0447\u0438 \u043a\u0430\u0440\u0442\u043e\u0447\u043a\u0443\n\n"
        "/myads \u2014 \u043c\u043e\u0438 \u043e\u0431\u044a\u044f\u0432\u043b\u0435\u043d\u0438\u044f\n"
        "/status \u2014 \u043c\u043e\u0439 \u0442\u0430\u0440\u0438\u0444\n"
        "/subscribe \u2014 \u043f\u043e\u0434\u043f\u0438\u0441\u043a\u0430\n"
        "/tips \u2014 \u0441\u043e\u0432\u0435\u0442\u044b\n"
        "/admin \u2014 \u0441\u0442\u0430\u0442\u0438\u0441\u0442\u0438\u043a\u0430")

async def tips_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "\U0001f4a1 \u0421\u041e\u0412\u0415\u0422\u042b \u041f\u041e \u041f\u0420\u041e\u0414\u0410\u0416\u0410\u041c\n\n"
        "\U0001f4f8 \u0424\u043e\u0442\u043e: \u043c\u0438\u043d\u0438\u043c\u0443\u043c 5 \u0444\u043e\u0442\u043e \u043f\u0440\u0438 \u0434\u043d\u0435\u0432\u043d\u043e\u043c \u0441\u0432\u0435\u0442\u0435\n"
        "\U0001f4b0 \u0426\u0435\u043d\u0430: \u043e\u0441\u0442\u0430\u0432\u044c 10-15% \u043d\u0430 \u0442\u043e\u0440\u0433\n"
        "\u26a1 \u041e\u0442\u0432\u0435\u0447\u0430\u0439 \u0431\u044b\u0441\u0442\u0440\u043e \u0438 \u043f\u0440\u0435\u0434\u043b\u0430\u0433\u0430\u0439 \u0434\u043e\u0441\u0442\u0430\u0432\u043a\u0443")

async def _process_and_reply(update, user_id, prompt):
    user = get_user(user_id)
    if not can_use(user):
        await update.message.reply_text(
            "\U0001f6ab \u041b\u0418\u041c\u0418\u0422 \u0418\u0421\u0427\u0415\u0420\u041f\u0410\u041d\n\n"
            "\u0422\u044b \u0438\u0441\u043f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u043b \u0432\u0441\u0435 %d \u0431\u0435\u0441\u043f\u043b\u0430\u0442\u043d\u044b\u0445 \u043a\u0430\u0440\u0442\u043e\u0447\u043a\u0438.\n\n"
            "\u041e\u0444\u043e\u0440\u043c\u0438 \u043f\u043e\u0434\u043f\u0438\u0441\u043a\u0443 \u0447\u0442\u043e\u0431\u044b \u043f\u0440\u043e\u0434\u043e\u043b\u0436\u0438\u0442\u044c \U0001f447" % FREE_LIMIT,
            reply_markup=get_subscribe_keyboard())
        return
    if not is_admin(user_id) and not is_subscribed(user):
        increment_usage(user_id)
        user = get_user(user_id)
        left = max(0, FREE_LIMIT - user["free_used"])
        if left > 0:
            await update.message.reply_text(
                "\u23f3 \u0421\u043e\u0437\u0434\u0430\u044e \u043a\u0430\u0440\u0442\u043e\u0447\u043a\u0443...\n"
                "\U0001f193 \u041e\u0441\u0442\u0430\u043b\u043e\u0441\u044c \u0431\u0435\u0441\u043f\u043b\u0430\u0442\u043d\u044b\u0445: %d" % left)
        else:
            await update.message.reply_text(
                "\u23f3 \u0421\u043e\u0437\u0434\u0430\u044e \u043a\u0430\u0440\u0442\u043e\u0447\u043a\u0443...\n"
                "\u26a0\ufe0f \u042d\u0442\u043e \u043f\u043e\u0441\u043b\u0435\u0434\u043d\u044f\u044f \u0431\u0435\u0441\u043f\u043b\u0430\u0442\u043d\u0430\u044f \u043f\u043e\u043f\u044b\u0442\u043a\u0430!")
    else:
        await update.message.reply_text("\u23f3 \u0421\u043e\u0437\u0434\u0430\u044e \u043a\u0430\u0440\u0442\u043e\u0447\u043a\u0443...")
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
            "\U0001f4f8 \u0424\u043e\u0442\u043e \u043f\u043e\u043b\u0443\u0447\u0435\u043d\u043e!\n\n"
            "\u041e\u043f\u0438\u0448\u0438 \u0442\u043e\u0432\u0430\u0440 \u0442\u0435\u043a\u0441\u0442\u043e\u043c.")

async def send_invoice(query, plan):
    user_id = query.from_user.id
    plans = {
        "week": ("\u041d\u0435\u0434\u0435\u043b\u044f", "7 \u0434\u043d\u0435\u0439 \u0431\u0435\u0437 \u043b\u0438\u043c\u0438\u0442\u0430", PRICE_WEEK),
        "month": ("\u041c\u0435\u0441\u044f\u0446", "30 \u0434\u043d\u0435\u0439 \u0431\u0435\u0437 \u043b\u0438\u043c\u0438\u0442\u0430", PRICE_MONTH),
        "forever": ("\u041d\u0430\u0432\u0441\u0435\u0433\u0434\u0430", "\u0411\u0435\u0437 \u043e\u0433\u0440\u0430\u043d\u0438\u0447\u0435\u043d\u0438\u0439", PRICE_FOREVER),
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
    user = get_user(user_id)
    await update.message.reply_text(
        "\U0001f389 \u041e\u041f\u041b\u0410\u0422\u0410 \u041f\u0420\u041e\u0428\u041b\u0410!\n\n"
        "\u2705 \u041f\u043e\u0434\u043f\u0438\u0441\u043a\u0430 \u0430\u043a\u0442\u0438\u0432\u0438\u0440\u043e\u0432\u0430\u043d\u0430\n\n"
        "\u0421\u0442\u0430\u0442\u0443\u0441: %s\n\n"
        "\u0421\u043e\u0437\u0434\u0430\u0432\u0430\u0439 \u043a\u0430\u0440\u0442\u043e\u0447\u043a\u0438 \u0431\u0435\u0437 \u043e\u0433\u0440\u0430\u043d\u0438\u0447\u0435\u043d\u0438\u0439! \U0001f680" % status_text(user))

CATEGORY_PROMPTS = {
    "cat_electronics": "\u042d\u043b\u0435\u043a\u0442\u0440\u043e\u043d\u0438\u043a\u0430. \u041e\u043f\u0438\u0448\u0438: \u043c\u043e\u0434\u0435\u043b\u044c, \u0445\u0430\u0440\u0430\u043a\u0442\u0435\u0440\u0438\u0441\u0442\u0438\u043a\u0438, \u0441\u043e\u0441\u0442\u043e\u044f\u043d\u0438\u0435, \u043a\u043e\u043c\u043f\u043b\u0435\u043a\u0442\u0430\u0446\u0438\u044f.",
    "cat_clothes": "\u041e\u0434\u0435\u0436\u0434\u0430. \u041e\u043f\u0438\u0448\u0438: \u0431\u0440\u0435\u043d\u0434, \u0440\u0430\u0437\u043c\u0435\u0440, \u0446\u0432\u0435\u0442, \u0441\u043e\u0441\u0442\u043e\u044f\u043d\u0438\u0435.",
    "cat_auto": "\u0410\u0432\u0442\u043e. \u041e\u043f\u0438\u0448\u0438: \u043c\u0430\u0440\u043a\u0430, \u0433\u043e\u0434, \u043f\u0440\u043e\u0431\u0435\u0433, \u0441\u043e\u0441\u0442\u043e\u044f\u043d\u0438\u0435.",
    "cat_furniture": "\u041c\u0435\u0431\u0435\u043b\u044c. \u041e\u043f\u0438\u0448\u0438: \u0440\u0430\u0437\u043c\u0435\u0440\u044b, \u043c\u0430\u0442\u0435\u0440\u0438\u0430\u043b, \u0441\u043e\u0441\u0442\u043e\u044f\u043d\u0438\u0435.",
    "cat_sport": "\u0421\u043f\u043e\u0440\u0442. \u041e\u043f\u0438\u0448\u0438: \u0432\u0438\u0434 \u0441\u043f\u043e\u0440\u0442\u0430, \u043c\u043e\u0434\u0435\u043b\u044c, \u0441\u043e\u0441\u0442\u043e\u044f\u043d\u0438\u0435.",
    "cat_kids": "\u0414\u0435\u0442\u0441\u043a\u0438\u0435 \u0442\u043e\u0432\u0430\u0440\u044b. \u041e\u043f\u0438\u0448\u0438: \u0432\u043e\u0437\u0440\u0430\u0441\u0442, \u0441\u043e\u0441\u0442\u043e\u044f\u043d\u0438\u0435, \u043a\u043e\u043c\u043f\u043b\u0435\u043a\u0442\u0430\u0446\u0438\u044f.",
    "cat_other": "\u041e\u043f\u0438\u0448\u0438 \u0442\u043e\u0432\u0430\u0440 \u043f\u043e\u0434\u0440\u043e\u0431\u043d\u043e: \u043d\u0430\u0437\u0432\u0430\u043d\u0438\u0435, \u0441\u043e\u0441\u0442\u043e\u044f\u043d\u0438\u0435, \u0445\u0430\u0440\u0430\u043a\u0442\u0435\u0440\u0438\u0441\u0442\u0438\u043a\u0438.",
}

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data
    state = user_states.get(user_id, {})
    ensure_user(user_id, query.from_user.username or query.from_user.first_name)

    if data == "create_card":
        await query.message.reply_text(
            "\U0001f4e6 \u0412\u044b\u0431\u0435\u0440\u0438 \u043a\u0430\u0442\u0435\u0433\u043e\u0440\u0438\u044e \u0442\u043e\u0432\u0430\u0440\u0430:",
            reply_markup=get_category_keyboard())
    elif data in CATEGORY_PROMPTS:
        hint = CATEGORY_PROMPTS[data]
        user_states[user_id] = {"category_hint": hint}
        await query.message.reply_text(
            "\u270f\ufe0f %s\n\n\u041d\u0430\u043f\u0438\u0448\u0438 \u043e\u043f\u0438\u0441\u0430\u043d\u0438\u0435 \u0442\u043e\u0432\u0430\u0440\u0430 \U0001f447" % hint)
    elif data == "show_help":
        await query.message.reply_text(
            "\u2753 \u041e\u043f\u0438\u0448\u0438 \u0442\u043e\u0432\u0430\u0440 \u2014 \u0441\u043e\u0437\u0434\u0430\u043c \u043a\u0430\u0440\u0442\u043e\u0447\u043a\u0443!\n\n"
            "/myads /status /subscribe /admin")
    elif data == "my_status":
        user = get_user(user_id)
        ads = get_user_ads(user_id, limit=100)
        kb = None if (is_admin(user_id) or is_subscribed(user)) else get_subscribe_keyboard()
        await query.message.reply_text(
            "\U0001f464 \u041c\u041e\u0419 \u0410\u041a\u041a\u0410\u0423\u041d\u0422\n\n"
            "\u0421\u0442\u0430\u0442\u0443\u0441: %s\n\u041a\u0430\u0440\u0442\u043e\u0447\u0435\u043a: %d" % (status_text(user), len(ads)),
            reply_markup=kb)
    elif data == "my_ads":
        ads = get_user_ads(user_id)
        if not ads:
            await query.message.reply_text(
                "\U0001f4c1 \u041d\u0435\u0442 \u043e\u0431\u044a\u044f\u0432\u043b\u0435\u043d\u0438\u0439. \u041e\u043f\u0438\u0448\u0438 \u0442\u043e\u0432\u0430\u0440!")
            return
        text = "\U0001f4c1 \u041c\u041e\u0418 \u041e\u0411\u042a\u042f\u0412\u041b\u0415\u041d\u0418\u042f\n\n"
        buttons = []
        for ad in ads:
            ad_id, title, price, category, created_at = ad
            text += "\U0001f4cc %s \u2014 %s \u20bd\n" % (title, price)
            buttons.append([InlineKeyboardButton("\U0001f4cc %s" % title[:35], callback_data="show_ad_%d" % ad_id)])
        buttons.append([InlineKeyboardButton("\U0001f680 \u0421\u043e\u0437\u0434\u0430\u0442\u044c \u043d\u043e\u0432\u043e\u0435", callback_data="new_item")])
        await query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons))
    elif data.startswith("show_ad_"):
        ad_id = int(data.replace("show_ad_", ""))
        card = get_ad_by_id(ad_id)
        if card:
            user_states[user_id] = {"last_card": card, "original_prompt": card.get("title","")}
            await query.message.reply_text(format_card(card), reply_markup=get_card_keyboard())
        else:
            await query.message.reply_text("\u041e\u0431\u044a\u044f\u0432\u043b\u0435\u043d\u0438\u0435 \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d\u043e.")
    elif data == "show_plans":
        await query.message.reply_text(
            "\U0001f4b3 \u0422\u0410\u0420\u0418\u0424\u042b\n\n"
            "\U0001f193 \u0411\u0435\u0441\u043f\u043b\u0430\u0442\u043d\u043e: %d \u043a\u0430\u0440\u0442\u043e\u0447\u0435\u043a\n\n"
            "\u26a1 \u041d\u0435\u0434\u0435\u043b\u044f \u2014 %d \u20bd\n"
            "\U0001f4c5 \u041c\u0435\u0441\u044f\u0446 \u2014 %d \u20bd\n"
            "\u267e \u041d\u0430\u0432\u0441\u0435\u0433\u0434\u0430 \u2014 %d \u20bd" % (FREE_LIMIT, PRICE_WEEK, PRICE_MONTH, PRICE_FOREVER),
            reply_markup=get_subscribe_keyboard())
    elif data == "pay_week":
        await send_invoice(query, "week")
    elif data == "pay_month":
        await send_invoice(query, "month")
    elif data == "pay_forever":
        await send_invoice(query, "forever")
    elif data == "new_item":
        await query.message.reply_text(
            "\U0001f4e6 \u0412\u044b\u0431\u0435\u0440\u0438 \u043a\u0430\u0442\u0435\u0433\u043e\u0440\u0438\u044e:",
            reply_markup=get_category_keyboard())
    elif data == "copy_text" and state.get("last_card"):
        card = state["last_card"]
        text = "%s\n\n%s\n\n%s" % (card.get("title",""), card.get("description",""), card.get("call_to_action",""))
        await query.message.reply_text(
            "\U0001f4cb \u041a\u043e\u043f\u0438\u0440\u0443\u0439 \u0442\u0435\u043a\u0441\u0442:\n\n%s" % text)
    elif data == "photo_tips" and state.get("last_card"):
        tips = state["last_card"].get("photo_tips", "\u0421\u043e\u0432\u0435\u0442\u044b \u043d\u0435\u0434\u043e\u0441\u0442\u0443\u043f\u043d\u044b")
        await query.message.reply_text(
            "\U0001f4f8 \u0421\u041e\u0412\u0415\u0422\u042b \u041f\u041e \u0424\u041e\u0422\u041e:\n\n%s" % tips)
    elif data in ["edit_price","edit_desc","alt_titles"] and state.get("original_prompt"):
        prompts = {
            "edit_price": "\u041f\u0440\u0435\u0434\u043b\u043e\u0436\u0438 3 \u0432\u0430\u0440\u0438\u0430\u043d\u0442\u0430 \u0446\u0435\u043d\u044b: \u044d\u043a\u043e\u043d\u043e\u043c, \u043e\u043f\u0442\u0438\u043c\u0430\u043b\u044c\u043d\u0430\u044f, \u043f\u0440\u0435\u043c\u0438\u0443\u043c.",
            "edit_desc": "\u041f\u0435\u0440\u0435\u043f\u0438\u0448\u0438 \u043e\u043f\u0438\u0441\u0430\u043d\u0438\u0435 \u044d\u043c\u043e\u0446\u0438\u043e\u043d\u0430\u043b\u044c\u043d\u0435\u0435. \u0421\u043e\u0445\u0440\u0430\u043d\u0438 \u0445\u0430\u0440\u0430\u043a\u0442\u0435\u0440\u0438\u0441\u0442\u0438\u043a\u0438.",
            "alt_titles": "\u041f\u0440\u0438\u0434\u0443\u043c\u0430\u0439 3 \u0432\u0430\u0440\u0438\u0430\u043d\u0442\u0430 \u0437\u0430\u0433\u043e\u043b\u043e\u0432\u043a\u0430 \u0434\u043e 50 \u0441\u0438\u043c\u0432\u043e\u043b\u043e\u0432.",
        }
        await query.message.reply_text("\u23f3 \u0413\u0435\u043d\u0435\u0440\u0438\u0440\u0443\u044e \u0432\u0430\u0440\u0438\u0430\u043d\u0442\u044b...")
        result = await asyncio.to_thread(generate_card,
            "\u0422\u043e\u0432\u0430\u0440: %s\n\n%s" % (state["original_prompt"], prompts[data]))
        if isinstance(result, dict):
            save_ad(user_id, result)
            user_states[user_id]["last_card"] = result
            await query.message.reply_text(format_card(result), reply_markup=get_card_keyboard())
        else:
            await query.message.reply_text(str(result))

def main():
    # Сбрасываем webhook автоматически при старте
    try:
        httpx.get(
            "https://api.telegram.org/bot%s/deleteWebhook?drop_pending_updates=true" % TELEGRAM_TOKEN,
            timeout=10)
        logger.info("Webhook deleted OK")
    except Exception as e:
        logger.warning("Could not delete webhook: %s" % e)

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
    # Запускаем веб-сервер в отдельном потоке
    web_thread = threading.Thread(target=run_web_server, daemon=True)
    web_thread.start()
    # Запускаем самопинг в отдельном потоке
    ping_thread = threading.Thread(target=self_ping, daemon=True)
    ping_thread.start()
    # Запускаем бота
    main()
