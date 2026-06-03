импорт os
импорт json
import asyncio
импорт логирования
импорт httpx
импорт sqlite3
импорт uuid
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
from telegram.ext import (
    Приложение, обработчик команд, обработчик сообщений,
    CallbackQueryHandler, ContextTypes, filters,
    PreCheckoutQueryHandler
)

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ.get("BOT_TOKEN", "8525104782:AAHkKpIBjhYyg9x4bNgEL8A9WKE6RVwnzDA")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "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")
PAYMENT_PROVIDER_TOKEN = "390540012:LIVE:96921"
ADMIN_ID = 6466766416
FREE_LIMIT = 5
PRICE_WEEK = 99
ЦЕНА_МЕСЯЦ = 299
PRICE_FOREVER = 1490

user_states = {}
DB_PATH = "avito_bot.db"

def init_db():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""СОЗДАТЬ ТАБЛИЦУ, ЕСЛИ ОНА НЕ СУЩЕСТВУЕТ users (
        user_id INTEGER PRIMARY KEY, username TEXT,
        free_used INTEGER DEFAULT 0, plan TEXT DEFAULT 'free',
        plan_until TEXT DEFAULT NULL, created_at TEXT DEFAULT (datetime('now')))""")
    cur.execute("""СОЗДАТЬ ТАБЛИЦУ, ЕСЛИ ОНА НЕ СУЩЕСТВУЕТ payments (
        payment_id TEXT PRIMARY KEY, user_id INTEGER, plan TEXT,
        сумма INTEGER, статус TEXT DEFAULT 'ожидание',
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
    если строка:
        return {"user_id":row[0],"username":row[1],"free_used":row[2],"plan":row[3],"plan_until":row[4]}
    вернуть None

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
        ЦЕННОСТИ (?,?,?,?,?,?)""",
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
    возвращать строки

def get_ad_by_id(ad_id):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT card_json FROM ads WHERE id=?", (ad_id,))
    row = cur.fetchone()
    con.close()
    если строка:
        return json.loads(row[0])
    вернуть None

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
    если план == "неделя":
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
    если user["plan"] == "forever":
        вернуть True
    if user["plan"] in ("week","month") and user["plan_until"]:
        until = datetime.strptime(user["plan_until"], "%Y-%m-%d %H:%M:%S")
        return datetime.utcnow() < until
    вернуть False

def can_use(user):
    if is_admin(user["user_id"]):
        вернуть True
    если is_subscribed(user):
        вернуть True
    return user["free_used"] < FREE_LIMIT

def status_text(user):
    if is_admin(user["user_id"]):
        return "рџ'' РђХРјРёРЅРёСЃС‚СЂР°С‚РѕСЂ — Н±РµР·Р»РёРјРёС‚"
    если user["plan"] == "forever":
        return "в РќР°РІСЃРμРіРґР° — Р±РµР·Р»РёРјРёС‚"
    if user["plan"] in ("week","month") and user["plan_until"]:
        until = datetime.strptime(user["plan_until"], "%Y-%m-%d %H:%M:%S")
        if datetime.utcnow() < until:
            days_left = (until-datetime.utcnow()).days
            return f"рџ“… РџРѕРґРїРёСЃРєР° Н{until.strftime('%d.%m.%Y')} — РµС‰С' {days_left} Н."
    left = max(0, FREE_LIMIT-user["free_used"])
    return f"рџ†“ Р'РμСЃРїР°С‚РЅРѕ: РѕСЃС‚Р°Р»РѕСЃСЊ {left} ЁР· {FREE_LIMIT}"

SYSTEM_PROMPT = """Добавлено имя файла. Удобный вариант.

Снимок с JSON-файлом Описание:
{"title":"Нет 50 штук","price":"С†РµР° С‡РёСЃР»РѕРј,"price_hint":"Можно с‚Р°РєР°СЏ С†РµРЅР°","category":"РєР°С‚РµРіРѕСЂРёСЏ","subcategory":"РїРѕРґРєР°С‚РµРіРѕСЂРёСЏ","description":"Очень 5-7 РїСЂРµХР»РѕР¶РµРЅРёР№,"condition":"РќРѕРІРѕРµ РёС‚Р»РёС‡РЅРѕРµ РёР»Рё Нынче","tags":["С‚РмкРі1","С‚РмкРі2","С‚РмкРі3","С‚РмкРі4","С‚РмкРі5"],"seo_keywords":"Нет СЃР»РѕРІР°","call_to_action":"Пожалуйста,"photo_tips":"3 снимка С„РѕС‚Рѕ","предупреждения":"С‡С‚Рѕ СѓРєР°Р·Р°С‚СЊ С‡С‚РѕР±С‹ РёР·Р±Р¶Р°С‚СЊ СЃРїРѕСЂРѕРІ"}"""

def generate_card(prompt):
    пытаться:
        response = httpx.post(
            "https://api.groq.com/openai/v1/chat/completions",
            заголовки={
                "Авторизация": f"Предъявитель {GROQ_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "llama-3.3-70b-versatile",
                "сообщения": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt}
                ],
                "max_tokens": 1200,
                "температура": 0,7,
            },
            timeout=30
        )
        data = response.json()
        raw = data["choices"][0]["message"]["content"].strip()
        raw = raw.replace("```json","").replace("```","").strip()
        пытаться:
            return json.loads(raw)
        кроме:
            если "{" в необработанном виде:
                start = raw.find("{")
                end = raw.rfind("}") + 1
                пытаться:
                    return json.loads(raw[start:end])
                кроме:
                    проходить
            возврат необработанного
    за исключением исключения как e:
        logger.error(f"Groq error: {e}")
        return "Получить.

def format_card(data):
    цена = data.get("цена", "")
    пытаться:
        price_fmt = f"{int(price):,}".replace(",", " ") + " в‚Ѕ"
    кроме:
        price_fmt = f"{price} в‚Ѕ"
    tags_str = " ".join([f"#{t.replace(' ','_')}" for t in data.get("tags", [])])
    строки = []
    lines.append("в"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓ")
    lines.append("вњ… ААА РўРћР§АА Р"ААВ")
    lines.append("в"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓ")
    lines.append("")
    lines.append(f"рџ“Њ {data.get('title','').upper()}")
    lines.append("")
    lines.append(f"рџ'° Р¦РµРЅР°: {price_fmt}")
    lines.append(f" {data.get('price_hint','')}")
    lines.append("")
    lines.append(f"рџ“‚ {data.get('category','')} вЂє {data.get('subcategory','')}")
    lines.append(f"рџ”§ СВЕДЕНИЕ: {data.get('условие','')}")
    lines.append("")
    lines.append("-»Ђ —»Ђ —»Ђ —»Ђ —»Ђ — — — — — — — — — — — — — — — — — — — — — — — — — — — — — — — — — — — — — — — — — — — — — — —
    lines.append("рџ“ќ РћРџР˜РЎРђРќР˜Р•")
    lines.append("-»Ђ —»Ђ —»Ђ —»Ђ —»Ђ — — — — — — — — — — — — — — — — — — — — — — — — — — — — — — — — — — — — — — — — — — — — — — —
    lines.append("")
    lines.append(data.get('description',''))
    lines.append("")
    lines.append(f"рџ”Ќ {data.get('seo_keywords','')}")
    lines.append("")
    lines.append(f"рџЏ· {tags_str}")
    lines.append("")
    lines.append(f"рџ'¬ {data.get('call_to_action','')}")
    если data.get("warnings"):
        lines.append("")
        lines.append("-»Ђ —»Ђ —»Ђ —»Ђ —»Ђ — — — — — — — — — — — — — — — — — — — — — — — — — — — — — — — — — — — — — — — — — — — — — — —
        lines.append(f"вљ пёЏ {data['warnings']}")
    if data.get("photo_tips"):
        lines.append("")
        lines.append("-»Ђ —»Ђ —»Ђ —»Ђ —»Ђ — — — — — — — — — — — — — — — — — — — — — — — — — — — — — — — — — — — — — — — — — — — — — — —
        lines.append("рџ"ё СЛВР'РўР" РџРћ А")
        lines.append(data['photo_tips'])
    lines.append("")
    lines.append("в"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓ")
    return "\n".join(lines)

def get_card_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("вњЏпёЏ Р˜Р·РјРμРЅРёС‚СЊ С†РμРЅСѓ", callback_data="edit_price"),
         InlineKeyboardButton("рџ“ќ Р”СЂСѓРіРѕРµ РѕРїРёСЃР°РЅРöРµ", callback_data="edit_desc")],
        [InlineKeyboardButton("рџ"¤ 3 шт.", callback_data="alt_titles"),
         InlineKeyboardButton("рџ"ё СЎРѕРІРµС‚С‹ РїРѕ С„РѕС‚Рѕ", callback_data="photo_tips")],
        [InlineKeyboardButton("рџ"‹ Кнопочка", callback_data="copy_text"),
         InlineKeyboardButton("рџ"Ѓ РќРѕРІС‹Р№ С‚РѕРІР°СЂ", callback_data="new_item")],
        [InlineKeyboardButton("рџ"Ѓ РњРѕРё РѕР±СЉСЏРІР"МЦРЅРёСЏ", callback_data="my_ads")],
    ])

def get_subscribe_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("вљЎ РќРμХР^Р»СЏ — 99 в‚Ѕ", callback_data="pay_week")],
        [InlineKeyboardButton("рџ“… РњРμСЃСЏС† — 299 —», callback_data="pay_month")],
        [InlineKeyboardButton("в™ѕ РќР°РІСЃРμРіРґР° — 1 490 в‚Ѕ", callback_data="pay_forever")],
    ])

def get_category_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("рџ“± РР»РμРєС‚СЂРѕРЅРєР°», callback_data="cat_electronics"),
         InlineKeyboardButton("рџ'— РћРґРµР¶РґР°", callback_data="cat_clothes")],
        [InlineKeyboardButton("рџљ— Ац", callback_data="cat_auto"),
         InlineKeyboardButton("рџ›‹ РњРµР±РµР»СЊ", callback_data="cat_furniture")],
        [InlineKeyboardButton("рџЏ‹ РЎРїРѕС‚", callback_data="cat_sport"),
         InlineKeyboardButton("Рџ§ё Р"РμС‚СЃРєРѕР", callback_data="cat_kids")],
        [InlineKeyboardButton("рџ“¦ Р”СЂСѓРіРѕРµ", callback_data="cat_other")],
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(u.id, u.username or u.first_name)
    пользователь = get_user(u.id)
    текст = (
        f"в"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓ\n"
        f"рџЏЄ AVITO HELPER BOT\n"
        f"в"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓ\n\n"
        f"РџСЂРёРІРµС‚, {u.first_name}! рџ'‹\n\n"
        f"РЇ СЃРѕР·НґР°СЋ РїСЂРѕР°СЋС‰РёРµ РєР°СЂС‚РѕС‡РєРё\n"
        f"Нет РђРІРёС‚Рѕ Н° 10 сЃРµРєСѓРґ.\n\n"
        f"рџ“ќ РџРёС€Сѓ РїСЂРѕРґР°СЋС‰РёРµ РѕРїРёСЃР°РЅРёСЏ\n"
        f"рџ'° РµРєРѕРјРµРЅРґСѓСЋ С†РµРЅСѓ\n"
        f"рџЏ· РџРѕРґР±РёСЂР°СЋ С‚РµРіРё Нё РєР»СЋС‡РµРІС‹Рµ СЃР»РѕРІР°\n"
        f"рџ“Ѓ РЎРѕС…СЂР°РЅСЏСЋ РёСЃС‚РѕСЂРёСЋ РѕР±СЉСЏРІР»РµРЅРёР№\n\n"
        f"—»Ђ —»Ђ —»Ђ —»Ђ —»Ђ —»Ђ —»Ђ —»Ђ —Ђ —Ђ —Ђ\n"
        f"Добавка: {status_text(user)}\n"
        f"—»Ђ —»Ђ —»Ђ —Ђ —»Ђ —Ђ —»Ђ —Ђ —Ђ —Ђ —Ђ\n\n"
        f"Р'С‹Р±РµСЂРё РєР°С‚РµРіРѕСЂРёСЋ РёР»Рё РїСЂРѕСЃС‚Рѕ\n"
        f"РѕРїРёС€Рё С‚РѕРІР°СЂ С‚РµРєСЃС‚РѕРј рџ'‡"
    )
    await update.message.reply_text(text,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("подключиться", callback_data="create_card")],
            [InlineKeyboardButton("рџ"Ѓ РњРѕРё РѕР±СЉСЏРІР"МЦРЅРёСЏ", callback_data="my_ads"),
             InlineKeyboardButton("рџ'¤ РњРѕР№ СЃС‚Р°С‚СѓСЃ", callback_data="my_status")],
            [InlineKeyboardButton("рџ'і РџРѕРґРїРёСЃРєР°", callback_data="show_plans"),
             InlineKeyboardButton("вќ“ РџРѕРјРѕС‰СЊ", callback_data="show_help")],
        ]))

async def myads_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(u.id, u.username or u.first_name)
    ads = get_user_ads(u.id)
    если это не реклама:
        await update.message.reply_text("рџ“Ѓ РњРћР˜ РћР'РЄРЇР'Р•РќР˜РЇ\n\nРЈ С‚РµР±СЏ РїРѕРєР° Н‚ СЃРѕС…СЂР°С'РЅРЅС‹С… РѕР±СЉСЏРІР»МЦРЁР№.\n\nРћРїРёС€Рё С‚РѕРІР°СЂ Рё СЏ СЃРѕР·РґР°Р Офигенно!")
        возвращаться
    text = "рџ“Ѓ РњРћР˜ РћР'ЄРЇР'Р•РќР˜РЇ\nв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓ в”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓ\n\n"
    кнопки = []
    для рекламы в рекламе:
        ad_id, title, price, category, created_at = ad
        дата = created_at[:10]
        text += f"рџ“Њ {название}\nрџ'° {цена} ‚Ѕ | рџ“… {date}\n\n"
        buttons.append([InlineKeyboardButton(f"рџ“Њ {title[:35]}", callback_data=f"show_ad_{ad_id}")])
    button.append([InlineKeyboardButton("Новый_элемент", callback_data="new_item")])
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons))

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not is_admin(u.id):
        await update.message.reply_text("Нет ответа на вопрос.")
        возвращаться
    total_users, total_ads, total_subs, total_payments = get_stats()
    текст = (
        f"в"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓ\n"
        f"рџ"Љ РЎАђАР˜РЎРўР˜РљА Р'АРўА\n"
        f"в"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓ\n\n"
        f"рџ'Ґ Список: {total_users}\n"
        f"рџ“ќ Список: {total_ads}\n"
        f"рџ'і РџРѕРґРёСЃС‡РёРєРѕРІ: {total_subs}\n"
        f"рџ'° РћРїР°С‚: {total_pays}\n"
        f"\nв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓ"
    )
    await update.message.reply_text(text)

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(u.id, u.username or u.first_name)
    пользователь = get_user(u.id)
    ads = get_user_ads(u.id, limit=100)
    текст = (
        f"в"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓ\n"
        f"рџ'¤ РњРћР™ АђАРљАРђРЈРќРў\n"
        f"в"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓ\n\n"
        f"С‚Р°С‚СѓСЃ: {status_text(user)}\n"
        f"Добавка: {len(ads)}\n"
        f"\nв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓ"
    )
    kb = None if (is_admin(u.id) or is_subscribed(user)) else get_subscribe_keyboard()
    await update.message.reply_text(text, reply_markup=kb)

async def subscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    текст = (
        f"в"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓ\n"
        f"рџ'і РўАР Р˜Р¤Р"\n"
        f"в"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓ\n\n"
        f"рџ†“ Р'РµСЃРїР°С‚РЅРѕ\n"
        f" {FREE_LIMIT} Свободный доступ\n\n"
        f"вљЎ РќРμХРµР»СЏ — {PRICE_WEEK} —\n"
        f" Р'РμР·Р»РёРјРёС‚ РЅР° 7 Н\n\n"
        f"рџ“… РњРµСЃСЏС† — {PRICE_MONTH} ‚Ѕ\n"
        f" Н'РёРјРёС‚ РЅР° 30 Н\n\n"
        f"вс РќР°РІСЃРμРіРґР° — {PRICE_FOREVER} —\n"
        f" Р'РμР·Р»РёРјРёС‚ ±РµР· РѕРіСЂР°РЅРёС‡РµРЅРёР№\n\n"
        f"в"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓ\n"
        f"РћРїР°С‚Р° С‡РµСЂРµР· Р®РљР°СЃСѓ рџ”'\n"
        f"РљР°С‚С‹, СЛ, Р®Деньги"
    )
    if hasattr(update, 'message') and update.message:
        await update.message.reply_text(text, reply_markup=get_subscribe_keyboard())
    еще:
        await update.message.reply_text(text, reply_markup=get_subscribe_keyboard())

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    текст = (
        f"в"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓ\n"
        f"вќ“ АљАРљАРћР¬Р—АВА¬РЎРЇ\n"
        f"в"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓ\n\n"
        f"1пёЏвѓЈ Р'С‹Р±РµСЂРё РєР°С‚РµРіРѕСЂРёСЋ С‚РѕРІР°СЂР°\n"
        f"2пёЏвѓЈ РћРїРёС€Рё С‚РѕРІР°СЂ С‚РµРєСЃС‚РѕРј\n"
        f"3пёЏвѓЈ РџРѕР»СѓС‡Рё РіРѕС‚РѕРІСѓСЋ РєР°СЂС‚РѕС‡РєСѓ\n"
        f"4пёЏвѓЈ Р˜СЃРїРѕР"СЊР·СѓР№ РєРЅРѕРїРєРё НГР"СЏ РїСЂР°РІРѕРє\n\n"
        f"—»Ђ —»Ђ —»Ђ —»Ђ —»Ђ —»Ђ —»Ђ —»Ђ —Ђ —Ђ —Ђ\n"
        f"рџ“Њ РљРѕРјР°РґС‹:\n\n"
        f"/myads — РјРѕРё РѕР±СЉСЏРІР»РµРЅРёСЏ\n"
        f"/status — Имя С‚Р°СЂРёС„\n"
        f"/subscribe — Регистрация\n"
        f"/tips — СЃРѕРІРμС‚С‹ РїРѕ РїСЂРѕРґР°Р¶Р°Рј\n"
        f"/admin — СЃС‚Р°С‚РёСЃС‚РёРєР° (С‚РѕР»СЊРєРѕ Р°ХРґРјРёРЅ)\n"
        f"\nв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓ"
    )
    await update.message.reply_text(text)

async def tips_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    текст = (
        f"в"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓ\n"
        f"рџ'Ў СЎАР'Р•АўР« РџА РџР РћР»АР–А\n"
        f"в"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓ\n\n"
        f"рџ"ё Н¤РћА\n"
        f"РњРёРЅРёРјСѓРј 5 С„РѕС‚Рѕ РїСЂРё НГРЅРµРІРЅРѕРј СЃРІРµС‚Рµ\n"
        f"РџРѕРєР°Р¶Рё Н\n\n"
        f"рџ“ќ РћРџР˜СРђРќР˜Р•\n"
        f"РЈРєР°Р¶Рё РІСЃРµ С…Р°СЂР°РєС‚РµСЂРёСЃС‚РёРєРё\n"
        f"РќР°РїРёС€Рё РіРѕРґ РїРѕРєСѓРїРєРё РїСЂРёС‡РёРЅСѓ РїСЂРѕР°Р¶Рё\n\n"
        f"рџ'° Р¦Р•АА\n"
        f"Уровень 10-15%%"
        f"Р˜Р·СѓС‡Рё РїРѕС…РѕР¶РёРц РѕР±СЉСЏРІР»РµРЅРёСЏ\n\n"
        f"вљЎ СЎРљРћР РћСРўР¬\n"
        f"РћС‚РІРµС‡Р°Р№ Р±С‹СЃС‚СЂРѕ\n"
        f"РџСЂРµХР№ РґРѕСЃС‚Р°РІРєСѓ\n"
        f"\nв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓ"
    )
    await update.message.reply_text(text)

async def _process_and_reply(update, user_id, prompt):
    пользователь = get_user(user_id)
    if not can_use(user):
        await update.message.reply_text(
            f"в"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓ\n"
            f"рџљ« РџАА˜Рў ​​Р˜СЎР§Р•Р РџАА\n"
            f"в"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓ\n\n"
            f"РўС‹ РёСЃРїРѕР»СЊР·РѕРІР°Р» РІСЃРμ {FREE_LIMIT} Р±РμСЃРїР»Р°С‚РЅС‹С… РєР°СЂС‚РѕС‡РµРє.\n\n"
            f"С™РѕСЂРјРїРѕРґРїРёСЃРєСѓ С‡С‚РѕР±С‹ РїСЂРѕРґРѕР»Р¶РёС‚СЊ рџ'‡",
            reply_markup=get_subscribe_keyboard())
        возвращаться
    if not is_admin(user_id) and not is_subscribed(user):
        increment_usage(user_id)
        пользователь = get_user(user_id)
        left = max(0, FREE_LIMIT - user["free_used"])
        если left > 0:
            await update.message.reply_text(f"вЏі РЎРѕР·НґР°СЋ РєР°СЂС‚РѕС‡РєСѓ...\nрџ†“ РћСЃС‚Р°Р»РѕСЃСЊ Р±РμСЃРїР»Р°С‚РЅС‹С…: {влево}")
        еще:
            await update.message.reply_text("вЏі СЎРѕР·НґР°СЋ РєР°СЂС‚РѕС‡РєСѓ...\nвљ пёЏ РС‚Рѕ РїРѕСЃР»МкРґРЅСЏСЏ Нравится!")
    еще:
        await update.message.reply_text("вЏі Свідняня...")
    result = await asyncio.to_thread(generate_card, prompt)
    if isinstance(result, dict):
        save_ad(user_id, result)
        user_states[user_id] = {"last_card": result, "original_prompt": prompt}
        await update.message.reply_text(format_card(result), reply_markup=get_card_keyboard())
    еще:
        await update.message.reply_text(str(result))

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(u.id, u.username or u.first_name)
    await _process_and_reply(update, u.id, update.message.text)

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(u.id, u.username or u.first_name)
    caption = update.message.caption or ""
    если подпись:
        await _process_and_reply(update, u.id, caption)
    еще:
        await update.message.reply_text(
            "рџ“ё Р¤РѕС‚Рѕ РїРѕР»СѓС‡РµРЅРѕ!\n\n"
            "РћРїРёС€Рё С‚РµРєСЃС‚РѕРј Рё СЏ СЃРѕР·ХР°РєР°СЂС‚РѕС‡РєСѓ.\n"
            "РќР°РїСЂРёРјРμСЂ: РјР°СЂРєР°, РјРѕРґРµР"СЊ, СЃРѕСЃС‚РѕСЏРЅРёРµ, НіРѕРґ.")

async def send_invoice(query, plan):
    user_id = query.from_user.id
    если план == "неделя":
        title = "Должность"
        description = "Р'РμР·Р»РёРјРёС‚РЅС‹РєР°СЂС‚РѕС‡РєРё РЅР° 7 РґРЅРµР№"
        сумма = ЦЕНА_НЕДЕЛЯ * 100
        payload = f"week_{user_id}_{uuid.uuid4().hex[:8]}"
    elif plan == "month":
        title = "РџРѕРґРёСЃРєР° РјРµСЃСЏС†"
        description = "Р'РμР·Р»РёРјРёС‚РЅС‹РєР°СЂС‚РѕС‡РєРё РЅР° 30 РґРЅРµР№"
        сумма = Цена_месяц * 100
        payload = f"month_{user_id}_{uuid.uuid4().hex[:8]}"
    еще:
        title = "Показать полностью"
        description = "Р'РμР·Р»РёРјРёС‚РЅС‹РєР°СЂС‚РѕС‡РєРё Р±РµР· РѕРіСЂР°РЅРёС‡РµРёР№"
        сумма = ЦЕНА_НАВСЕГДА * 100
        payload = f"forever_{user_id}_{uuid.uuid4().hex[:8]}"
    save_payment(payload, user_id, plan, amount // 100)
    await query.message.reply_invoice(
        заголовок=заголовок,
        описание=описание,
        payload=payload,
        provider_token=PAYMENT_PROVIDER_TOKEN,
        валюта="RUB",
        цены=[LabeledPrice(title, amount)],
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
        план = "неделя"
        label = "Нет"
    elif payload.startswith("month_"):
        план = "месяц"
        label = "Нет"
    еще:
        план = "навсегда"
        label = "Нет"
    confirm_payment(payload)
    activate_plan(user_id, plan)
    пользователь = get_user(user_id)
    await update.message.reply_text(
        f"в"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓ\n"
        f"рџЋ‰ РћАРђАРђ РџР НРЁА!\n"
        f"в"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓ\n\n"
        f"в… РџРѕХРїРёСЃРєР° {label} Р°РєС‚РёРІРёСЂРѕР°РЅР°\n\n"
        f"Строка: {status_text(user)}\n\n"
        f"Сер°Р°РІР°Р№ РєР°СЂС‚РѕС‡РєРё Н±РµР·РѕРіСЂР°РЅРёС‡РµРЅРёР№! рџљЂ")

CATEGORY_PROMPTS = {
    "cat_electronics": "Ну, нет. С…Р°СЂР°РєС‚РµСЂРёСЃС‚РёРєРё, СЃРѕСЃС‚РѕСЏРЅРёРµ, РєРѕРјРїР»РµРєС‚Р°С†РёСЏ.",
    "cat_clothes": "Мне нравится. СЃРѕСЃС‚РѕСЏРЅРёРµ.",
    "cat_auto": "Нет ёёё. Объекты: РјР°СЂРєР°, РіРѕРґ, РїСЂРѕР±РцРі, СЃРѕСЃС‚РѕСЏРЅРёРµ.",
    "cat_furniture": "Можно ли это сделать? СЃРѕСЃС‚РѕСЏРЅРёРµ.",
    "cat_sport": "РЎРїРѕСЂС‚ ЕЁ РѕС‚ХС‹С…. РћРїРёС€Рё: РІРёРґ СЃРїРѕСЂС‚Р°, РјРѕРґРµР»СЊ, СЃРѕСЃС‚РѕСЏРЅРёРµ.",
    "cat_kids": "Хорошо. РћРїРёС€Рё: РІРѕР·СЂР°СЃС‚, СЃРѕСЃС‚РѕСЏРЅРёРµ, РєРѕРїР»МкРєС‚Р°С†РёСЏ.",
    "cat_other": "Добавлено имя, С…Р°СЂР°РєС‚РµСЂРёСЃС‚РёРєРё.",
}

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    запрос = обновление.обратный_запрос
    await query.answer()
    user_id = query.from_user.id
    данные = запрос.данные
    state = user_states.get(user_id, {})
    ensure_user(user_id, query.from_user.username or query.from_user.first_name)

    если data == "create_card":
        await query.message.reply_text("Рџ“¦ Р'С‹Р±РµСЂРё РєР°С‚РµРіРѕСЂРёСЋ С‚РѕРІР°СЂР°:", Answer_markup=get_category_keyboard())
    elif data in CATEGORY_PROMPTS:
        hint = CATEGORY_PROMPTS[data]
        user_states[user_id] = {"category_hint": hint}
        await query.message.reply_text(f"вњЏпёЏ {подсказка}\n\nРќР°РїРёС€РѕРїРёСЃР°РЅРёРµ С‚РѕРІР°СЂР° рџ'‡")
    elif data == "show_help":
        await query.message.reply_text("‚РїРёС€Рё С‚РѕРІР°СЂ С‚РµРєСЃС‚РѕРј — СЃРѕР·Н°Р Сервер!\n\n/myads — Имя пользователя\n/status — Сервер \n/admin — СЃС‚Р°С‚РёСЃС‚РёР°")
    elif data == "my_status":
        пользователь = get_user(user_id)
        ads = get_user_ads(user_id, limit=100)
        kb = None if (is_admin(user_id) or is_subscribed(user)) else get_subscribe_keyboard()
        await query.message.reply_text(
            f"в"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓ\nрџ'¤ РњРћР™ Ав"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓ\n\n"
            f"С‚Р°С‚СѓСЃ: {status_text(user)}\nРљР°СЂС‚РѕС‡РµРє СЃРѕР·Н: {len(ads)}\n\nв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓ",
            reply_markup=kb)
    elif data == "my_ads":
        ads = get_user_ads(user_id)
        если это не реклама:
            await query.message.reply_text("рџ“Ѓ РњРћР˜ РћР'РЄРЇР'Р•РќР˜РЇ\n\nРЈ С‚РµР±СЏ РїРѕРєР° Н‚ \n\nРћРїРёС€Рё С‚РѕРІР°СЂ — СЃРѕР·Н°РєР°СЂС‚РѕС‡РєСѓ!")
            возвращаться
        text = "рџ“Ѓ РњРћР˜ РћР'ЄРЇР'Р•РќР˜РЇ\nв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓ в”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓв”Ѓ\n\n"
        кнопки = []
        для рекламы в рекламе:
            ad_id, title, price, category, created_at = ad
            дата = created_at[:10]
            text += f"рџ“Њ {название}\nрџ'° {цена} ‚Ѕ | рџ“… {date}\n\n"
            buttons.append([InlineKeyboardButton(f"рџ“Њ {title[:35]}", callback_data=f"show_ad_{ad_id}")])
        button.append([InlineKeyboardButton("Новый_элемент", callback_data="new_item")])
        await query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons))
    elif data.startswith("show_ad_"):
        ad_id = int(data.replace("show_ad_", ""))
        card = get_ad_by_id(ad_id)
        если карта:
            user_states[user_id] = {"last_card": card, "original_prompt": card.get("title","")}
            await query.message.reply_text(format_card(card), reply_markup=get_card_keyboard())
        еще:
            await query.message.reply_text("Уведомление недоступно.")
    elif data == "show_plans":
        текст = (
            f"в"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓ\n"
            f"рџ'і РўАР Р˜Р¤Р"\n"
            f"в"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓ\n\n"
            f"рџ†“ Р'РµСЃРїР°С‚РЅРѕ\n"
            f" {FREE_LIMIT} Свободный доступ\n\n"
            f"вљЎ РќРμХРµР»СЏ — {PRICE_WEEK} —\n"
            f" Р'РμР·Р»РёРјРёС‚ РЅР° 7 Н\n\n"
            f"рџ“… РњРµСЃСЏС† — {PRICE_MONTH} ‚Ѕ\n"
            f" Н'РёРјРёС‚ РЅР° 30 Н\n\n"
            f"вс РќР°РІСЃРμРіРґР° — {PRICE_FOREVER} —\n"
            f" Р'РμР·Р»РёРјРёС‚ ±РµР· РѕРіСЂР°РЅРёС‡РµРЅРёР№\n\n"
            f"в"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓ\n"
            f"РћРїР°С‚Р° С‡РµСЂРµР· Р®РљР°СЃСѓ рџ”'\n"
            f"РљР°С‚С‹, СЛ, Р®Деньги"
        )
        await query.message.reply_text(text, reply_markup=get_subscribe_keyboard())
    elif data == "pay_week":
        await send_invoice(query, "week")
    elif data == "pay_month":
        await send_invoice(query, "month")
    elif data == "pay_forever":
        await send_invoice(query, "forever")
    elif data == "new_item":
        await query.message.reply_text("Рџ“¦ Р'С‹Р±РµСЂРё РєР°С‚РµРіРѕСЂРёСЋ С‚РѕРІР°СЂР°:", Answer_markup=get_category_keyboard())
    elif data == "copy_text" and state.get("last_card"):
        карта = состояние["последняя_карта"]
        text = f"{card.get('title','')}\n\n{card.get('description','')}\n\n{card.get('call_to_action','')}"
        await query.message.reply_text(f"рџ“‹ Имя С‚РµРєС‚:\n\n{text}")
    elif data == "photo_tips" and state.get("last_card"):
        подсказки = состояние["last_card"].get("photo_tips", "С уважением"
        await query.message.reply_text(f"в"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓ\nрџ"ё СВВП "А" А\nв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓв"Ѓ\n\n{советы}")
    elif data in ["edit_price","edit_desc","alt_titles"] and state.get("original_prompt"):
        подсказки = {
            "edit_price": "Детали 3 штуки: СЌРєРѕРЅРѕРј, Крысы РїСЂРµРјРёСѓРјРћР±СЉСЏСЃРЅРЁ.",
            "edit_desc": "Добавлено значение параметра РїСЂРѕР°СЋС‰Рµ РЎРѕС…СЂР°РЅРё Р°СЂР°РєС‚РµСЂРёСЃС‚РёРєРё.",
            "alt_titles": "Уровень №3 3-й уровень 50-ти лет СЂР°Р·С‹РјРё РєР»СЋС‡РµРІС‹РјРё СЃР»РѕР°РјРё.",
        }
        await query.message.reply_text("ВЏі Р“РµРЅРµСЂРёСЂСѓСЋ РІР°СЂР°РЅС‚С‹...")
        result = await asyncio.to_thread(generate_card,
            f"РўРѕРІР°СЂ: {state['original_prompt']}\n\n{prompts[data]}")
        if isinstance(result, dict):
            save_ad(user_id, result)
            user_states[user_id]["last_card"] = result
            await query.message.reply_text(format_card(result), reply_markup=get_card_keyboard())
        еще:
            await query.message.reply_text(str(result))

async def run_bot():
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
    logger.info("AvitoHelperBot НР°РїСѓС‰РμРЅ!")
    await app.run_polling(drop_pending_updates=True)

если __name__ == "__main__":
    asyncio.run(run_bot())
