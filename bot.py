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
    logger.info(f"Р’РµР±-СЃРµСЂРІРµСЂ Р·Р°РїСѓС‰РµРЅ РЅР° РїРѕСЂС‚Сѓ {port}")
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
        return "рџ‘‘ РђРґРјРёРЅРёСЃС‚СЂР°С‚РѕСЂ вЂ” Р±РµР·Р»РёРјРёС‚"
    if user["plan"] == "forever":
        return "в™ѕ РќР°РІСЃРµРіРґР° вЂ” Р±РµР·Р»РёРјРёС‚"
    if user["plan"] in ("week","month") and user["plan_until"]:
        until = datetime.strptime(user["plan_until"], "%Y-%m-%d %H:%M:%S")
        if datetime.utcnow() < until:
            days_left = (until-datetime.utcnow()).days
            return f"рџ“… РџРѕРґРїРёСЃРєР° РґРѕ {until.strftime('%d.%m.%Y')} вЂ” РµС‰С‘ {days_left} РґРЅ."
    left = max(0, FREE_LIMIT-user["free_used"])
    return f"рџ†“ Р‘РµСЃРїР»Р°С‚РЅРѕ: РѕСЃС‚Р°Р»РѕСЃСЊ {left} РёР· {FREE_LIMIT}"

SYSTEM_PROMPT = """РўС‹ РїСЂРѕС„РµСЃСЃРёРѕРЅР°Р»СЊРЅС‹Р№ РєРѕРїРёСЂР°Р№С‚РµСЂ РґР»СЏ РђРІРёС‚Рѕ. РћС‚РІРµС‡Р°Р№ С‚РѕР»СЊРєРѕ РЅР° СЂСѓСЃСЃРєРѕРј СЏР·С‹РєРµ.

РЎРѕР·РґР°Р№ РєР°СЂС‚РѕС‡РєСѓ С‚РѕРІР°СЂР° РўРћР›Р¬РљРћ РІ С„РѕСЂРјР°С‚Рµ JSON Р±РµР· РґСЂСѓРіРёС… СЃР»РѕРІ:
{"title":"Р·Р°РіРѕР»РѕРІРѕРє РґРѕ 50 СЃРёРјРІРѕР»РѕРІ","price":"С†РµРЅР° С‡РёСЃР»РѕРј","price_hint":"РїРѕС‡РµРјСѓ С‚Р°РєР°СЏ С†РµРЅР°","category":"РєР°С‚РµРіРѕСЂРёСЏ","subcategory":"РїРѕРґРєР°С‚РµРіРѕСЂРёСЏ","description":"РѕРїРёСЃР°РЅРёРµ 5-7 РїСЂРµРґР»РѕР¶РµРЅРёР№","condition":"РќРѕРІРѕРµ РёР»Рё РћС‚Р»РёС‡РЅРѕРµ РёР»Рё РҐРѕСЂРѕС€РµРµ","tags":["С‚РµРі1","С‚РµРі2","С‚РµРі3","С‚РµРі4","С‚РµРі5"],"seo_keywords":"РєР»СЋС‡РµРІС‹Рµ СЃР»РѕРІР°","call_to_action":"РїСЂРёР·С‹РІ Рє РґРµР№СЃС‚РІРёСЋ","photo_tips":"3 СЃРѕРІРµС‚Р° РїРѕ С„РѕС‚Рѕ","warnings":"С‡С‚Рѕ СѓРєР°Р·Р°С‚СЊ С‡С‚РѕР±С‹ РёР·Р±РµР¶Р°С‚СЊ СЃРїРѕСЂРѕРІ"}"""

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
        return "РћС€РёР±РєР° РіРµРЅРµСЂР°С†РёРё. РџРѕРїСЂРѕР±СѓР№ РµС‰С‘ СЂР°Р·."

def format_card(data):
    price = data.get("price", "")
    try:
        price_fmt = f"{int(price):,}".replace(",", " ") + " в‚Ѕ"
    except:
        price_fmt = f"{price} в‚Ѕ"
    tags_str = "  ".join([f"#{t.replace(' ','_')}" for t in data.get("tags", [])])
    lines = []
    lines.append("в”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓ")
    lines.append("вњ…  РљРђР РўРћР§РљРђ Р“РћРўРћР’Рђ")
    lines.append("в”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓ")
    lines.append("")
    lines.append(f"рџ“Њ  {data.get('title','').upper()}")
    lines.append("")
    lines.append(f"рџ’°  Р¦РµРЅР°:  {price_fmt}")
    lines.append(f"      {data.get('price_hint','')}")
    lines.append("")
    lines.append(f"рџ“‚  {data.get('category','')}  вЂє  {data.get('subcategory','')}")
    lines.append(f"рџ”§  РЎРѕСЃС‚РѕСЏРЅРёРµ:  {data.get('condition','')}")
    lines.append("")
    lines.append("в”Ђ в”Ђ в”Ђ в”Ђ в”Ђ в”Ђ в”Ђ в”Ђ в”Ђ в”Ђ в”Ђ")
    lines.append("рџ“ќ  РћРџРРЎРђРќРР•")
    lines.append("в”Ђ в”Ђ в”Ђ в”Ђ в”Ђ в”Ђ в”Ђ в”Ђ в”Ђ в”Ђ в”Ђ")
    lines.append("")
    lines.append(data.get('description',''))
    lines.append("")
    lines.append(f"рџ”Ќ  {data.get('seo_keywords','')}")
    lines.append("")
    lines.append(f"рџЏ·  {tags_str}")
    lines.append("")
    lines.append(f"рџ’¬  {data.get('call_to_action','')}")
    if data.get("warnings"):
        lines.append("")
        lines.append("в”Ђ в”Ђ в”Ђ в”Ђ в”Ђ в”Ђ в”Ђ в”Ђ в”Ђ в”Ђ в”Ђ")
        lines.append(f"вљ пёЏ  {data['warnings']}")
    if data.get("photo_tips"):
        lines.append("")
        lines.append("в”Ђ в”Ђ в”Ђ в”Ђ в”Ђ в”Ђ в”Ђ в”Ђ в”Ђ в”Ђ в”Ђ")
        lines.append("рџ“ё  РЎРћР’Р•РўР« РџРћ Р¤РћРўРћ")
        lines.append(data['photo_tips'])
    lines.append("")
    lines.append("в”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓ")
    return "\n".join(lines)

def get_card_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("вњЏпёЏ РР·РјРµРЅРёС‚СЊ С†РµРЅСѓ", callback_data="edit_price"),
         InlineKeyboardButton("рџ“ќ Р”СЂСѓРіРѕРµ РѕРїРёСЃР°РЅРёРµ", callback_data="edit_desc")],
        [InlineKeyboardButton("рџ”¤ 3 Р·Р°РіРѕР»РѕРІРєР°", callback_data="alt_titles"),
         InlineKeyboardButton("рџ“ё РЎРѕРІРµС‚С‹ РїРѕ С„РѕС‚Рѕ", callback_data="photo_tips")],
        [InlineKeyboardButton("рџ“‹ РљРѕРїРёСЂРѕРІР°С‚СЊ С‚РµРєСЃС‚", callback_data="copy_text"),
         InlineKeyboardButton("рџ”Ѓ РќРѕРІС‹Р№ С‚РѕРІР°СЂ", callback_data="new_item")],
        [InlineKeyboardButton("рџ“Ѓ РњРѕРё РѕР±СЉСЏРІР»РµРЅРёСЏ", callback_data="my_ads")],
    ])

def get_subscribe_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("вљЎ РќРµРґРµР»СЏ вЂ” 99 в‚Ѕ", callback_data="pay_week")],
        [InlineKeyboardButton("рџ“… РњРµСЃСЏС† вЂ” 299 в‚Ѕ", callback_data="pay_month")],
        [InlineKeyboardButton("в™ѕ РќР°РІСЃРµРіРґР° вЂ” 1 490 в‚Ѕ", callback_data="pay_forever")],
    ])

def get_category_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("рџ“± Р­Р»РµРєС‚СЂРѕРЅРёРєР°", callback_data="cat_electronics"),
         InlineKeyboardButton("рџ‘— РћРґРµР¶РґР°", callback_data="cat_clothes")],
        [InlineKeyboardButton("рџљ— РђРІС‚Рѕ", callback_data="cat_auto"),
         InlineKeyboardButton("рџ›‹ РњРµР±РµР»СЊ", callback_data="cat_furniture")],
        [InlineKeyboardButton("рџЏ‹ РЎРїРѕСЂС‚", callback_data="cat_sport"),
         InlineKeyboardButton("рџ§ё Р”РµС‚СЃРєРѕРµ", callback_data="cat_kids")],
        [InlineKeyboardButton("рџ“¦ Р”СЂСѓРіРѕРµ", callback_data="cat_other")],
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(u.id, u.username or u.first_name)
    user = get_user(u.id)
    text = (
        f"в”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓ\n"
        f"рџЏЄ  AVITO HELPER BOT\n"
        f"в”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓ\n\n"
        f"РџСЂРёРІРµС‚, {u.first_name}! рџ‘‹\n\n"
        f"РЇ СЃРѕР·РґР°СЋ РїСЂРѕРґР°СЋС‰РёРµ РєР°СЂС‚РѕС‡РєРё\n"
        f"РґР»СЏ РђРІРёС‚Рѕ Р·Р° 10 СЃРµРєСѓРЅРґ.\n\n"
        f"рџ“ќ  РџРёС€Сѓ РїСЂРѕРґР°СЋС‰РёРµ РѕРїРёСЃР°РЅРёСЏ\n"
        f"рџ’°  Р РµРєРѕРјРµРЅРґСѓСЋ С†РµРЅСѓ\n"
        f"рџЏ·  РџРѕРґР±РёСЂР°СЋ С‚РµРіРё Рё РєР»СЋС‡РµРІС‹Рµ СЃР»РѕРІР°\n"
        f"рџ“Ѓ  РЎРѕС…СЂР°РЅСЏСЋ РёСЃС‚РѕСЂРёСЋ РѕР±СЉСЏРІР»РµРЅРёР№\n\n"
        f"в”Ђ в”Ђ в”Ђ в”Ђ в”Ђ в”Ђ в”Ђ в”Ђ в”Ђ в”Ђ в”Ђ\n"
        f"РўРІРѕР№ СЃС‚Р°С‚СѓСЃ:  {status_text(user)}\n"
        f"в”Ђ в”Ђ в”Ђ в”Ђ в”Ђ в”Ђ в”Ђ в”Ђ в”Ђ в”Ђ в”Ђ\n\n"
        f"Р’С‹Р±РµСЂРё РєР°С‚РµРіРѕСЂРёСЋ РёР»Рё РїСЂРѕСЃС‚Рѕ\n"
        f"РѕРїРёС€Рё С‚РѕРІР°СЂ С‚РµРєСЃС‚РѕРј рџ‘‡"
    )
    await update.message.reply_text(text,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("рџљЂ РЎРѕР·РґР°С‚СЊ РєР°СЂС‚РѕС‡РєСѓ", callback_data="create_card")],
            [InlineKeyboardButton("рџ“Ѓ РњРѕРё РѕР±СЉСЏРІР»РµРЅРёСЏ", callback_data="my_ads"),
             InlineKeyboardButton("рџ‘¤ РњРѕР№ СЃС‚Р°С‚СѓСЃ", callback_data="my_status")],
            [InlineKeyboardButton("рџ’і РџРѕРґРїРёСЃРєР°", callback_data="show_plans"),
             InlineKeyboardButton("вќ“ РџРѕРјРѕС‰СЊ", callback_data="show_help")],
        ]))

async def myads_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(u.id, u.username or u.first_name)
    ads = get_user_ads(u.id)
    if not ads:
        await update.message.reply_text("рџ“Ѓ  РњРћР РћР‘РЄРЇР’Р›Р•РќРРЇ\n\nРЈ С‚РµР±СЏ РїРѕРєР° РЅРµС‚ СЃРѕС…СЂР°РЅС‘РЅРЅС‹С… РѕР±СЉСЏРІР»РµРЅРёР№.\n\nРћРїРёС€Рё С‚РѕРІР°СЂ Рё СЏ СЃРѕР·РґР°Рј РєР°СЂС‚РѕС‡РєСѓ!")
        return
    text = "рџ“Ѓ  РњРћР РћР‘РЄРЇР’Р›Р•РќРРЇ\nв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓ\n\n"
    buttons = []
    for ad in ads:
        ad_id, title, price, category, created_at = ad
        date = created_at[:10]
        text += f"рџ“Њ {title}\nрџ’° {price} в‚Ѕ  |  рџ“… {date}\n\n"
        buttons.append([InlineKeyboardButton(f"рџ“Њ {title[:35]}", callback_data=f"show_ad_{ad_id}")])
    buttons.append([InlineKeyboardButton("рџљЂ РЎРѕР·РґР°С‚СЊ РЅРѕРІРѕРµ", callback_data="new_item")])
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons))

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not is_admin(u.id):
        await update.message.reply_text("РЈ С‚РµР±СЏ РЅРµС‚ РґРѕСЃС‚СѓРїР° Рє СЌС‚РѕР№ РєРѕРјР°РЅРґРµ.")
        return
    total_users, total_ads, total_subs, total_payments = get_stats()
    text = (
        f"в”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓ\n"
        f"рџ“Љ  РЎРўРђРўРРЎРўРРљРђ Р‘РћРўРђ\n"
        f"в”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓ\n\n"
        f"рџ‘Ґ  РџРѕР»СЊР·РѕРІР°С‚РµР»РµР№:       {total_users}\n"
        f"рџ“ќ  РљР°СЂС‚РѕС‡РµРє СЃРѕР·РґР°РЅРѕ:  {total_ads}\n"
        f"рџ’і  РџРѕРґРїРёСЃС‡РёРєРѕРІ:         {total_subs}\n"
        f"рџ’°  РћРїР»Р°С‚:                    {total_payments}\n"
        f"\nв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓ"
    )
    await update.message.reply_text(text)

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(u.id, u.username or u.first_name)
    user = get_user(u.id)
    ads = get_user_ads(u.id, limit=100)
    text = (
        f"в”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓ\n"
        f"рџ‘¤  РњРћР™ РђРљРљРђРЈРќРў\n"
        f"в”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓ\n\n"
        f"РЎС‚Р°С‚СѓСЃ:  {status_text(user)}\n"
        f"РљР°СЂС‚РѕС‡РµРє СЃРѕР·РґР°РЅРѕ:  {len(ads)}\n"
        f"\nв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓ"
    )
    kb = None if (is_admin(u.id) or is_subscribed(user)) else get_subscribe_keyboard()
    await update.message.reply_text(text, reply_markup=kb)

async def subscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        f"в”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓ\n"
        f"рџ’і  РўРђР РР¤Р«\n"
        f"в”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓ\n\n"
        f"рџ†“  Р‘РµСЃРїР»Р°С‚РЅРѕ\n"
        f"      {FREE_LIMIT} РєР°СЂС‚РѕС‡РµРє РґР»СЏ Р·РЅР°РєРѕРјСЃС‚РІР°\n\n"
        f"вљЎ  РќРµРґРµР»СЏ  вЂ”  {PRICE_WEEK} в‚Ѕ\n"
        f"      Р‘РµР·Р»РёРјРёС‚ РЅР° 7 РґРЅРµР№\n\n"
        f"рџ“…  РњРµСЃСЏС†  вЂ”  {PRICE_MONTH} в‚Ѕ\n"
        f"      Р‘РµР·Р»РёРјРёС‚ РЅР° 30 РґРЅРµР№\n\n"
        f"в™ѕ  РќР°РІСЃРµРіРґР°  вЂ”  {PRICE_FOREVER} в‚Ѕ\n"
        f"      Р‘РµР·Р»РёРјРёС‚ Р±РµР· РѕРіСЂР°РЅРёС‡РµРЅРёР№\n\n"
        f"в”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓ\n"
        f"РћРїР»Р°С‚Р° С‡РµСЂРµР· Р®РљР°СЃСЃСѓ рџ”’\n"
        f"РљР°СЂС‚С‹, РЎР‘Рџ, Р®Money"
    )
    await update.message.reply_text(text, reply_markup=get_subscribe_keyboard())

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        f"в”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓ\n"
        f"вќ“  РљРђРљ РџРћР›Р¬Р—РћР’РђРўР¬РЎРЇ\n"
        f"в”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓ\n\n"
        f"1пёЏвѓЈ  Р’С‹Р±РµСЂРё РєР°С‚РµРіРѕСЂРёСЋ С‚РѕРІР°СЂР°\n"
        f"2пёЏвѓЈ  РћРїРёС€Рё С‚РѕРІР°СЂ С‚РµРєСЃС‚РѕРј\n"
        f"3пёЏвѓЈ  РџРѕР»СѓС‡Рё РіРѕС‚РѕРІСѓСЋ РєР°СЂС‚РѕС‡РєСѓ\n"
        f"4пёЏвѓЈ  РСЃРїРѕР»СЊР·СѓР№ РєРЅРѕРїРєРё РґР»СЏ РїСЂР°РІРѕРє\n\n"
        f"в”Ђ в”Ђ в”Ђ в”Ђ в”Ђ в”Ђ в”Ђ в”Ђ в”Ђ в”Ђ в”Ђ\n"
        f"рџ“Њ  РљРѕРјР°РЅРґС‹:\n\n"
        f"/myads вЂ” РјРѕРё РѕР±СЉСЏРІР»РµРЅРёСЏ\n"
        f"/status вЂ” РјРѕР№ С‚Р°СЂРёС„\n"
        f"/subscribe вЂ” РїРѕРґРїРёСЃРєР°\n"
        f"/tips вЂ” СЃРѕРІРµС‚С‹ РїРѕ РїСЂРѕРґР°Р¶Р°Рј\n"
        f"/admin вЂ” СЃС‚Р°С‚РёСЃС‚РёРєР° (С‚РѕР»СЊРєРѕ Р°РґРјРёРЅ)\n"
        f"\nв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓ"
    )
    await update.message.reply_text(text)

async def tips_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        f"в”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓ\n"
        f"рџ’Ў  РЎРћР’Р•РўР« РџРћ РџР РћР”РђР–РђРњ\n"
        f"в”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓ\n\n"
        f"рџ“ё  Р¤РћРўРћ\n"
        f"РњРёРЅРёРјСѓРј 5 С„РѕС‚Рѕ РїСЂРё РґРЅРµРІРЅРѕРј СЃРІРµС‚Рµ\n"
        f"РџРѕРєР°Р¶Рё РґРµС„РµРєС‚С‹ С‡РµСЃС‚РЅРѕ\n\n"
        f"рџ“ќ  РћРџРРЎРђРќРР•\n"
        f"РЈРєР°Р¶Рё РІСЃРµ С…Р°СЂР°РєС‚РµСЂРёСЃС‚РёРєРё\n"
        f"РќР°РїРёС€Рё РіРѕРґ РїРѕРєСѓРїРєРё Рё РїСЂРёС‡РёРЅСѓ РїСЂРѕРґР°Р¶Рё\n\n"
        f"рџ’°  Р¦Р•РќРђ\n"
        f"РћСЃС‚Р°РІСЊ 10-15% РЅР° С‚РѕСЂРі\n"
        f"РР·СѓС‡Рё РїРѕС…РѕР¶РёРµ РѕР±СЉСЏРІР»РµРЅРёСЏ\n\n"
        f"вљЎ  РЎРљРћР РћРЎРўР¬\n"
        f"РћС‚РІРµС‡Р°Р№ Р±С‹СЃС‚СЂРѕ\n"
        f"РџСЂРµРґР»Р°РіР°Р№ РґРѕСЃС‚Р°РІРєСѓ\n"
        f"\nв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓ"
    )
    await update.message.reply_text(text)

async def _process_and_reply(update, user_id, prompt):
    user = get_user(user_id)
    if not can_use(user):
        await update.message.reply_text(
            f"в”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓ\n"
            f"рџљ«  Р›РРњРРў РРЎР§Р•Р РџРђРќ\n"
            f"в”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓ\n\n"
            f"РўС‹ РёСЃРїРѕР»СЊР·РѕРІР°Р» РІСЃРµ {FREE_LIMIT} Р±РµСЃРїР»Р°С‚РЅС‹С… РєР°СЂС‚РѕС‡РµРє.\n\n"
            f"РћС„РѕСЂРјРё РїРѕРґРїРёСЃРєСѓ С‡С‚РѕР±С‹ РїСЂРѕРґРѕР»Р¶РёС‚СЊ рџ‘‡",
            reply_markup=get_subscribe_keyboard())
        return
    if not is_admin(user_id) and not is_subscribed(user):
        increment_usage(user_id)
        user = get_user(user_id)
        left = max(0, FREE_LIMIT - user["free_used"])
        if left > 0:
            await update.message.reply_text(f"вЏі  РЎРѕР·РґР°СЋ РєР°СЂС‚РѕС‡РєСѓ...\nрџ†“  РћСЃС‚Р°Р»РѕСЃСЊ Р±РµСЃРїР»Р°С‚РЅС‹С…: {left}")
        else:
            await update.message.reply_text("вЏі  РЎРѕР·РґР°СЋ РєР°СЂС‚РѕС‡РєСѓ...\nвљ пёЏ  Р­С‚Рѕ РїРѕСЃР»РµРґРЅСЏСЏ Р±РµСЃРїР»Р°С‚РЅР°СЏ РїРѕРїС‹С‚РєР°!")
    else:
        await update.message.reply_text("вЏі  РЎРѕР·РґР°СЋ РєР°СЂС‚РѕС‡РєСѓ...")
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
            "рџ“ё  Р¤РѕС‚Рѕ РїРѕР»СѓС‡РµРЅРѕ!\n\n"
            "РћРїРёС€Рё С‚РѕРІР°СЂ С‚РµРєСЃС‚РѕРј Рё СЏ СЃРѕР·РґР°Рј РєР°СЂС‚РѕС‡РєСѓ.\n"
            "РќР°РїСЂРёРјРµСЂ: РјР°СЂРєР°, РјРѕРґРµР»СЊ, СЃРѕСЃС‚РѕСЏРЅРёРµ, РіРѕРґ.")

async def send_invoice(query, plan):
    user_id = query.from_user.id
    if plan == "week":
        title = "РџРѕРґРїРёСЃРєР° РЅР° РЅРµРґРµР»СЋ"
        description = "Р‘РµР·Р»РёРјРёС‚РЅС‹Рµ РєР°СЂС‚РѕС‡РєРё РЅР° 7 РґРЅРµР№"
        amount = PRICE_WEEK * 100
        payload = f"week_{user_id}_{uuid.uuid4().hex[:8]}"
    elif plan == "month":
        title = "РџРѕРґРїРёСЃРєР° РЅР° РјРµСЃСЏС†"
        description = "Р‘РµР·Р»РёРјРёС‚РЅС‹Рµ РєР°СЂС‚РѕС‡РєРё РЅР° 30 РґРЅРµР№"
        amount = PRICE_MONTH * 100
        payload = f"month_{user_id}_{uuid.uuid4().hex[:8]}"
    else:
        title = "РџРѕРґРїРёСЃРєР° РЅР°РІСЃРµРіРґР°"
        description = "Р‘РµР·Р»РёРјРёС‚РЅС‹Рµ РєР°СЂС‚РѕС‡РєРё Р±РµР· РѕРіСЂР°РЅРёС‡РµРЅРёР№"
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
        label = "РЅР° РЅРµРґРµР»СЋ"
    elif payload.startswith("month_"):
        plan = "month"
        label = "РЅР° РјРµСЃСЏС†"
    else:
        plan = "forever"
        label = "РЅР°РІСЃРµРіРґР°"
    confirm_payment(payload)
    activate_plan(user_id, plan)
    user = get_user(user_id)
    await update.message.reply_text(
        f"в”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓ\n"
        f"рџЋ‰  РћРџР›РђРўРђ РџР РћРЁР›Рђ!\n"
        f"в”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓ\n\n"
        f"вњ…  РџРѕРґРїРёСЃРєР° {label} Р°РєС‚РёРІРёСЂРѕРІР°РЅР°\n\n"
        f"РЎС‚Р°С‚СѓСЃ:  {status_text(user)}\n\n"
        f"РЎРѕР·РґР°РІР°Р№ РєР°СЂС‚РѕС‡РєРё Р±РµР· РѕРіСЂР°РЅРёС‡РµРЅРёР№! рџљЂ")

CATEGORY_PROMPTS = {
    "cat_electronics": "Р­Р»РµРєС‚СЂРѕРЅРёРєР° Рё РіР°РґР¶РµС‚С‹. РћРїРёС€Рё: РјРѕРґРµР»СЊ, С…Р°СЂР°РєС‚РµСЂРёСЃС‚РёРєРё, СЃРѕСЃС‚РѕСЏРЅРёРµ, РєРѕРјРїР»РµРєС‚Р°С†РёСЏ.",
    "cat_clothes": "РћРґРµР¶РґР° Рё РѕР±СѓРІСЊ. РћРїРёС€Рё: Р±СЂРµРЅРґ, СЂР°Р·РјРµСЂ, С†РІРµС‚, СЃРѕСЃС‚РѕСЏРЅРёРµ.",
    "cat_auto": "РђРІС‚Рѕ Рё РјРѕС‚Рѕ. РћРїРёС€Рё: РјР°СЂРєР°, РіРѕРґ, РїСЂРѕР±РµРі, СЃРѕСЃС‚РѕСЏРЅРёРµ.",
    "cat_furniture": "РњРµР±РµР»СЊ Рё РёРЅС‚РµСЂСЊРµСЂ. РћРїРёС€Рё: СЂР°Р·РјРµСЂС‹, РјР°С‚РµСЂРёР°Р», СЃРѕСЃС‚РѕСЏРЅРёРµ.",
    "cat_sport": "РЎРїРѕСЂС‚ Рё РѕС‚РґС‹С…. РћРїРёС€Рё: РІРёРґ СЃРїРѕСЂС‚Р°, РјРѕРґРµР»СЊ, СЃРѕСЃС‚РѕСЏРЅРёРµ.",
    "cat_kids": "Р”РµС‚СЃРєРёРµ С‚РѕРІР°СЂС‹. РћРїРёС€Рё: РІРѕР·СЂР°СЃС‚, СЃРѕСЃС‚РѕСЏРЅРёРµ, РєРѕРјРїР»РµРєС‚Р°С†РёСЏ.",
    "cat_other": "РћРїРёС€Рё СЃРІРѕР№ С‚РѕРІР°СЂ РїРѕРґСЂРѕР±РЅРѕ: РЅР°Р·РІР°РЅРёРµ, СЃРѕСЃС‚РѕСЏРЅРёРµ, С…Р°СЂР°РєС‚РµСЂРёСЃС‚РёРєРё.",
}

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data
    state = user_states.get(user_id, {})
    ensure_user(user_id, query.from_user.username or query.from_user.first_name)

    if data == "create_card":
        await query.message.reply_text("рџ“¦  Р’С‹Р±РµСЂРё РєР°С‚РµРіРѕСЂРёСЋ С‚РѕРІР°СЂР°:", reply_markup=get_category_keyboard())
    elif data in CATEGORY_PROMPTS:
        hint = CATEGORY_PROMPTS[data]
        user_states[user_id] = {"category_hint": hint}
        await query.message.reply_text(f"вњЏпёЏ  {hint}\n\nРќР°РїРёС€Рё РѕРїРёСЃР°РЅРёРµ С‚РѕРІР°СЂР° рџ‘‡")
    elif data == "show_help":
        await query.message.reply_text("вќ“  РћРїРёС€Рё С‚РѕРІР°СЂ С‚РµРєСЃС‚РѕРј вЂ” СЃРѕР·РґР°Рј РєР°СЂС‚РѕС‡РєСѓ!\n\n/myads вЂ” РјРѕРё РѕР±СЉСЏРІР»РµРЅРёСЏ\n/status вЂ” РјРѕР№ С‚Р°СЂРёС„\n/admin вЂ” СЃС‚Р°С‚РёСЃС‚РёРєР°")
    elif data == "my_status":
        user = get_user(user_id)
        ads = get_user_ads(user_id, limit=100)
        kb = None if (is_admin(user_id) or is_subscribed(user)) else get_subscribe_keyboard()
        await query.message.reply_text(
            f"в”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓ\nрџ‘¤  РњРћР™ РђРљРљРђРЈРќРў\nв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓ\n\n"
            f"РЎС‚Р°С‚СѓСЃ:  {status_text(user)}\nРљР°СЂС‚РѕС‡РµРє СЃРѕР·РґР°РЅРѕ:  {len(ads)}\n\nв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓ",
            reply_markup=kb)
    elif data == "my_ads":
        ads = get_user_ads(user_id)
        if not ads:
            await query.message.reply_text("рџ“Ѓ  РњРћР РћР‘РЄРЇР’Р›Р•РќРРЇ\n\nРЈ С‚РµР±СЏ РїРѕРєР° РЅРµС‚ РѕР±СЉСЏРІР»РµРЅРёР№.\n\nРћРїРёС€Рё С‚РѕРІР°СЂ вЂ” СЃРѕР·РґР°Рј РєР°СЂС‚РѕС‡РєСѓ!")
            return
        text = "рџ“Ѓ  РњРћР РћР‘РЄРЇР’Р›Р•РќРРЇ\nв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓ\n\n"
        buttons = []
        for ad in ads:
            ad_id, title, price, category, created_at = ad
            date = created_at[:10]
            text += f"рџ“Њ {title}\nрџ’° {price} в‚Ѕ  |  рџ“… {date}\n\n"
            buttons.append([InlineKeyboardButton(f"рџ“Њ {title[:35]}", callback_data=f"show_ad_{ad_id}")])
        buttons.append([InlineKeyboardButton("рџљЂ РЎРѕР·РґР°С‚СЊ РЅРѕРІРѕРµ", callback_data="new_item")])
        await query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons))
    elif data.startswith("show_ad_"):
        ad_id = int(data.replace("show_ad_", ""))
        card = get_ad_by_id(ad_id)
        if card:
            user_states[user_id] = {"last_card": card, "original_prompt": card.get("title","")}
            await query.message.reply_text(format_card(card), reply_markup=get_card_keyboard())
        else:
            await query.message.reply_text("РћР±СЉСЏРІР»РµРЅРёРµ РЅРµ РЅР°Р№РґРµРЅРѕ.")
    elif data == "show_plans":
        text = (
            f"в”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓ\nрџ’і  РўРђР РР¤Р«\nв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓ\n\n"
            f"рџ†“  Р‘РµСЃРїР»Р°С‚РЅРѕ\n      {FREE_LIMIT} РєР°СЂС‚РѕС‡РµРє РґР»СЏ Р·РЅР°РєРѕРјСЃС‚РІР°\n\n"
            f"вљЎ  РќРµРґРµР»СЏ  вЂ”  {PRICE_WEEK} в‚Ѕ\n      Р‘РµР·Р»РёРјРёС‚ РЅР° 7 РґРЅРµР№\n\n"
            f"рџ“…  РњРµСЃСЏС†  вЂ”  {PRICE_MONTH} в‚Ѕ\n      Р‘РµР·Р»РёРјРёС‚ РЅР° 30 РґРЅРµР№\n\n"
            f"в™ѕ  РќР°РІСЃРµРіРґР°  вЂ”  {PRICE_FOREVER} в‚Ѕ\n      Р‘РµР·Р»РёРјРёС‚ Р±РµР· РѕРіСЂР°РЅРёС‡РµРЅРёР№\n\n"
            f"в”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓ\nРћРїР»Р°С‚Р° С‡РµСЂРµР· Р®РљР°СЃСЃСѓ рџ”’\nРљР°СЂС‚С‹, РЎР‘Рџ, Р®Money"
        )
        await query.message.reply_text(text, reply_markup=get_subscribe_keyboard())
    elif data == "pay_week":
        await send_invoice(query, "week")
    elif data == "pay_month":
        await send_invoice(query, "month")
    elif data == "pay_forever":
        await send_invoice(query, "forever")
    elif data == "new_item":
        await query.message.reply_text("рџ“¦  Р’С‹Р±РµСЂРё РєР°С‚РµРіРѕСЂРёСЋ С‚РѕРІР°СЂР°:", reply_markup=get_category_keyboard())
    elif data == "copy_text" and state.get("last_card"):
        card = state["last_card"]
        text = f"{card.get('title','')}\n\n{card.get('description','')}\n\n{card.get('call_to_action','')}"
        await query.message.reply_text(f"рџ“‹  РљРѕРїРёСЂСѓР№ С‚РµРєСЃС‚:\n\n{text}")
    elif data == "photo_tips" and state.get("last_card"):
        tips = state["last_card"].get("photo_tips", "РЎРѕРІРµС‚С‹ РЅРµРґРѕСЃС‚СѓРїРЅС‹")
        await query.message.reply_text(f"в”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓ\nрџ“ё  РЎРћР’Р•РўР« РџРћ Р¤РћРўРћ\nв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓ\n\n{tips}")
    elif data in ["edit_price","edit_desc","alt_titles"] and state.get("original_prompt"):
        prompts = {
            "edit_price": "РџСЂРµРґР»РѕР¶Рё 3 РІР°СЂРёР°РЅС‚Р° С†РµРЅС‹: СЌРєРѕРЅРѕРј, РѕРїС‚РёРјР°Р»СЊРЅР°СЏ Рё РїСЂРµРјРёСѓРј. РћР±СЉСЏСЃРЅРё РєР°Р¶РґС‹Р№.",
            "edit_desc": "РџРµСЂРµРїРёС€Рё РѕРїРёСЃР°РЅРёРµ Р±РѕР»РµРµ СЌРјРѕС†РёРѕРЅР°Р»СЊРЅРѕ Рё РїСЂРѕРґР°СЋС‰Рµ. РЎРѕС…СЂР°РЅРё РІСЃРµ С…Р°СЂР°РєС‚РµСЂРёСЃС‚РёРєРё.",
            "alt_titles": "РџСЂРёРґСѓРјР°Р№ 3 РІР°СЂРёР°РЅС‚Р° Р·Р°РіРѕР»РѕРІРєР° РґРѕ 50 СЃРёРјРІРѕР»РѕРІ СЃ СЂР°Р·РЅС‹РјРё РєР»СЋС‡РµРІС‹РјРё СЃР»РѕРІР°РјРё.",
        }
        await query.message.reply_text("вЏі  Р“РµРЅРµСЂРёСЂСѓСЋ РІР°СЂРёР°РЅС‚С‹...")
        result = await asyncio.to_thread(generate_card,
            f"РўРѕРІР°СЂ: {state['original_prompt']}\n\n{prompts[data]}")
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
    logger.info("AvitoHelperBot Р·Р°РїСѓС‰РµРЅ!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    web_thread = threading.Thread(target=run_web_server, daemon=True)
    web_thread.start()
    main()
