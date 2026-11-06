import os
import re
import json
import time
import asyncio
import threading
import logging
from io import BytesIO
from datetime import timedelta

import psycopg
from flask import Flask
from curl_cffi import requests as c_requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, MessageHandler, CommandHandler, CallbackQueryHandler,
    ContextTypes, filters
)

# ============================================================
# CONFIG
# ============================================================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_ID = 8556272994
RENDER_URL = "https://titok-bot.onrender.com"

if not TOKEN:
    raise RuntimeError("BOT_TOKEN is missing in Render Environment.")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is missing in Render Environment.")

app = Flask(__name__)

# Admin conversation states stored per admin in context.user_data.
S_USER_SEARCH = "user_search"
S_BAN_REASON = "ban_reason"
S_CUSTOM_BAN = "custom_ban"
S_BROADCAST = "broadcast"
S_WELCOME = "welcome"
S_FORCEJOIN = "forcejoin"
S_BUTTON_NAME = "button_name"
S_BUTTON_URL = "button_url"
S_AD_TEXT = "ad_text"
S_AD_BUTTON = "ad_button"
S_AD_URL = "ad_url"
S_AD_PLACEMENT = "ad_placement"

# ============================================================
# DATABASE
# ============================================================
def db():
    return psycopg.connect(DATABASE_URL, connect_timeout=10)


def init_database():
    statements = [
        """CREATE TABLE IF NOT EXISTS users (
            telegram_id BIGINT PRIMARY KEY, username TEXT, first_name TEXT,
            is_banned BOOLEAN DEFAULT FALSE, joined_at TIMESTAMPTZ DEFAULT NOW())""",
        """CREATE TABLE IF NOT EXISTS downloads (
            id BIGSERIAL PRIMARY KEY,
            telegram_id BIGINT REFERENCES users(telegram_id) ON DELETE SET NULL,
            video_url TEXT, downloaded_at TIMESTAMPTZ DEFAULT NOW())""",
        """CREATE TABLE IF NOT EXISTS ban_history (
            id BIGSERIAL PRIMARY KEY, telegram_id BIGINT,
            reason TEXT, expires_at TIMESTAMPTZ, created_at TIMESTAMPTZ DEFAULT NOW())""",
        """CREATE TABLE IF NOT EXISTS bot_settings (
            key TEXT PRIMARY KEY, value TEXT)""",
        """CREATE TABLE IF NOT EXISTS admin_logs (
            id BIGSERIAL PRIMARY KEY, admin_id BIGINT, action TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW())""",
        """CREATE TABLE IF NOT EXISTS bot_buttons (
            id BIGSERIAL PRIMARY KEY, label TEXT NOT NULL, url TEXT NOT NULL,
            enabled BOOLEAN DEFAULT TRUE, sort_order INTEGER DEFAULT 0)""",
        """CREATE TABLE IF NOT EXISTS bot_ads (
            id BIGSERIAL PRIMARY KEY, message TEXT, button_text TEXT, button_url TEXT,
            placement TEXT DEFAULT 'after_download', enabled BOOLEAN DEFAULT FALSE)""",
        """CREATE TABLE IF NOT EXISTS error_logs (
            id BIGSERIAL PRIMARY KEY, message TEXT, created_at TIMESTAMPTZ DEFAULT NOW())""",
    ]
    with db() as conn:
        with conn.cursor() as cur:
            for sql in statements:
                cur.execute(sql)
            cur.execute("INSERT INTO bot_settings(key,value) VALUES('maintenance','off') ON CONFLICT(key) DO NOTHING")
            cur.execute("INSERT INTO bot_settings(key,value) VALUES('welcome','မင်္ဂလာပါ။ TikTok လင့်ခ် ပို့ပေးပါ။') ON CONFLICT(key) DO NOTHING")
            cur.execute("INSERT INTO bot_settings(key,value) VALUES('force_join','') ON CONFLICT(key) DO NOTHING")
    logger.info("Database initialized.")


def setting(key, default=""):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM bot_settings WHERE key=%s", (key,))
            row = cur.fetchone()
            return row[0] if row else default


def set_setting(key, value):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO bot_settings(key,value) VALUES(%s,%s)
                ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value""", (key, str(value)))


def log_admin(action):
    try:
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO admin_logs(admin_id,action) VALUES(%s,%s)", (ADMIN_ID, action[:500]))
    except Exception:
        logger.exception("Could not write admin log")


def log_error(message):
    try:
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO error_logs(message) VALUES(%s)", (str(message)[:3000],))
    except Exception:
        logger.exception("Could not write error log")


def save_user(user):
    try:
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("""INSERT INTO users(telegram_id,username,first_name)
                    VALUES(%s,%s,%s) ON CONFLICT(telegram_id) DO UPDATE SET
                    username=EXCLUDED.username, first_name=EXCLUDED.first_name""",
                    (user.id, user.username, user.first_name))
    except Exception:
        logger.exception("Could not save user")


def save_download(uid, url):
    try:
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO downloads(telegram_id,video_url) VALUES(%s,%s)", (uid, url))
    except Exception:
        logger.exception("Could not save download history")


def user_is_banned(uid):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT is_banned FROM users WHERE telegram_id=%s", (uid,))
            row = cur.fetchone()
            if not row or not row[0]:
                return False
            cur.execute("SELECT expires_at FROM ban_history WHERE telegram_id=%s ORDER BY created_at DESC LIMIT 1", (uid,))
            ban = cur.fetchone()
            if ban and ban[0] is not None:
                cur.execute("SELECT (%s <= NOW())", (ban[0],))
                expired = cur.fetchone()[0]
                if expired:
                    cur.execute("UPDATE users SET is_banned=FALSE WHERE telegram_id=%s", (uid,))
                    return False
            return True

# ============================================================
# FLASK / KEEP ALIVE
# ============================================================
@app.route("/")
def home():
    return "Bot is alive!"


def run_flask():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))


def keep_alive_ping():
    while True:
        time.sleep(600)
        try:
            c_requests.get(RENDER_URL, timeout=10)
        except Exception:
            pass

# ============================================================
# TIKTOK DOWNLOADER (existing functionality retained)
# ============================================================
def fetch_video_bytes(raw_url):
    debug_logs = []
    session = c_requests.Session(impersonate="chrome120")
    video_url = None
    try:
        response = session.post("https://www.tikwm.com/api/", data={"url": raw_url, "hd": 1},
            headers={"Referer": "https://www.tikwm.com/"}, timeout=15)
        if response.status_code == 200 and response.text.strip().startswith("{"):
            data = response.json()
            if data.get("code") == 0 and "data" in data:
                if data["data"].get("images"):
                    return None, None, "ဤပို့စ်သည် ဓာတ်ပုံ (Slide) ပို့စ် ဖြစ်နေပါသည်။"
                video_path = data["data"].get("play") or data["data"].get("hdplay")
                if video_path:
                    video_url = "https://www.tikwm.com" + video_path if video_path.startswith("/") else video_path
    except Exception as exc:
        debug_logs.append(f"M1: {str(exc)[:80]}")
    if not video_url:
        try:
            response = session.get(raw_url, allow_redirects=True, timeout=15)
            match = re.search(r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>', response.text)
            if match:
                data = json.loads(match.group(1))
                item = data.get("__DEFAULT_SCOPE__", {}).get("webapp.video-detail", {}).get("itemInfo", {}).get("itemStruct", {})
                video = item.get("video", {})
                video_url = video.get("playAddr") or video.get("downloadAddr")
        except Exception as exc:
            debug_logs.append(f"M2: {str(exc)[:80]}")
    if not video_url:
        return None, None, " | ".join(debug_logs) if debug_logs else "ဗီဒီယို လင့်ခ် ရှာမတွေ့ပါ။"
    try:
        result = session.get(video_url, headers={"Referer": "https://www.tiktok.com/"}, timeout=25)
        if result.status_code == 200 and len(result.content) > 10000:
            return result.content, video_url, None
    except Exception:
        pass
    return None, video_url, None

# ============================================================
# ADMIN UI
# ============================================================
def is_admin(update):
    return bool(update.effective_user and update.effective_user.id == ADMIN_ID)


def admin_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Dashboard", callback_data="adm:stats"), InlineKeyboardButton("👥 Users", callback_data="adm:users")],
        [InlineKeyboardButton("📢 Broadcast", callback_data="adm:broadcast"), InlineKeyboardButton("🔘 Buttons", callback_data="adm:buttons")],
        [InlineKeyboardButton("📥 Download Logs", callback_data="adm:downloads"), InlineKeyboardButton("⚙️ Settings", callback_data="adm:settings")],
        [InlineKeyboardButton("📣 Ads", callback_data="adm:ads"), InlineKeyboardButton("🧾 Admin Logs", callback_data="adm:logs")],
        [InlineKeyboardButton("💾 Backup", callback_data="adm:backup"), InlineKeyboardButton("🛡 Security", callback_data="adm:security")],
    ])


def back_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Admin Panel", callback_data="adm:home")]])


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    context.user_data.pop("admin_state", None)
    await update.message.reply_text("⚙️ Admin Panel", reply_markup=admin_keyboard())


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user:
        await asyncio.to_thread(save_user, user)
    if user and user.id == ADMIN_ID:
        await update.message.reply_text("မင်္ဂလာပါ Admin။ /admin ကိုနှိပ်ပြီး Panel ဖွင့်ပါ။")
        return
    if setting("maintenance", "off") == "on":
        await update.message.reply_text("Bot ကို ယာယီပြုပြင်ထိန်းသိမ်းနေပါတယ်။ ခဏနေ ပြန်လာပေးပါ။")
        return
    welcome = setting("welcome", "မင်္ဂလာပါ။ TikTok လင့်ခ် ပို့ပေးပါ။")
    await update.message.reply_text(welcome, reply_markup=await user_buttons_markup())
    await send_ad(update, context, "welcome")


async def user_buttons_markup():
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT label,url FROM bot_buttons WHERE enabled=TRUE ORDER BY sort_order,id")
            rows = cur.fetchall()
    if not rows:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, url=url)] for label, url in rows])


async def send_ad(update, context, placement):
    try:
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT message,button_text,button_url FROM bot_ads WHERE enabled=TRUE AND placement=%s ORDER BY id DESC LIMIT 1", (placement,))
                row = cur.fetchone()
        if row and row[0]:
            markup = InlineKeyboardMarkup([[InlineKeyboardButton(row[1], url=row[2])]]) if row[1] and row[2] else None
            await update.effective_message.reply_text(row[0], reply_markup=markup)
    except Exception:
        logger.exception("Ad send failed")


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(update):
        await query.edit_message_text("Access denied.")
        return
    action = query.data.split(":", 1)[1]
    if action == "home":
        await query.edit_message_text("⚙️ Admin Panel", reply_markup=admin_keyboard())
    elif action == "stats":
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM users")
                users = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM users WHERE is_banned=TRUE")
                banned = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM downloads")
                downloads = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM downloads WHERE downloaded_at::date=CURRENT_DATE")
                today = cur.fetchone()[0]
        await query.edit_message_text(f"📊 **Dashboard**\n\n👥 Users: {users}\n🚫 Banned: {banned}\n📥 Total Downloads: {downloads}\n📅 Today: {today}", parse_mode="Markdown", reply_markup=back_keyboard())
    elif action == "users":
        context.user_data["admin_state"] = S_USER_SEARCH
        await query.edit_message_text("အသုံးပြုသူရဲ့ Telegram ID ကို ပို့ပေးပါ။", reply_markup=back_keyboard())
    elif action == "broadcast":
        context.user_data["admin_state"] = S_BROADCAST
        await query.edit_message_text("Broadcast ပို့မယ့် စာ၊ ဓာတ်ပုံ သို့မဟုတ် ဗီဒီယိုကို ပို့ပါ။ Cancel လုပ်ရန် /cancel ကိုနှိပ်ပါ။", reply_markup=back_keyboard())
    elif action == "downloads":
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("""SELECT d.telegram_id,d.video_url,d.downloaded_at FROM downloads d
                    ORDER BY d.downloaded_at DESC LIMIT 15""")
                rows = cur.fetchall()
        lines = [f"{r[0]} | {str(r[2])[:16]}\n{(r[1] or '')[:90]}" for r in rows]
        await query.edit_message_text("📥 **Recent Downloads**\n\n" + ("\n\n".join(lines) if lines else "မှတ်တမ်းမရှိသေးပါ။"), parse_mode="Markdown", reply_markup=back_keyboard())
    elif action == "settings":
        maintenance = setting("maintenance", "off")
        force = setting("force_join", "") or "မသတ်မှတ်ရသေး"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"Maintenance: {maintenance.upper()} (Toggle)", callback_data="adm:maintenance")],
            [InlineKeyboardButton("✏️ Welcome Message", callback_data="adm:welcome")],
            [InlineKeyboardButton("🔗 Force Join Channel", callback_data="adm:forcejoin")],
            [InlineKeyboardButton("⬅️ Admin Panel", callback_data="adm:home")],
        ])
        await query.edit_message_text(f"⚙️ Settings\n\nForce Join: {force}\n\nRender ပေါ်က Bot Restart လုပ်ရန် Manual Restart လိုအပ်ပါတယ်။", reply_markup=kb)
    elif action == "maintenance":
        new_value = "off" if setting("maintenance", "off") == "on" else "on"
        set_setting("maintenance", new_value)
        log_admin(f"Maintenance set to {new_value}")
        await query.edit_message_text(f"Maintenance Mode: {new_value.upper()}", reply_markup=back_keyboard())
    elif action == "welcome":
        context.user_data["admin_state"] = S_WELCOME
        await query.edit_message_text("Welcome Message အသစ်ကို ပို့ပေးပါ။", reply_markup=back_keyboard())
    elif action == "forcejoin":
        context.user_data["admin_state"] = S_FORCEJOIN
        await query.edit_message_text("Force Join Channel ကို @channelname သို့မဟုတ် blank ပိတ်ရန် `off` လို့ပို့ပါ။ Channel မှာ Bot ကို Admin ပေးထားပါ။", parse_mode="Markdown", reply_markup=back_keyboard())
    elif action == "buttons":
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id,label,url,enabled FROM bot_buttons ORDER BY sort_order,id")
                rows = cur.fetchall()
        lines = [f"{r[0]}. {'🟢' if r[3] else '🔴'} {r[1]} — {r[2]}" for r in rows]
        kb = [[InlineKeyboardButton("➕ Add Button", callback_data="adm:addbutton")]]
        for r in rows:
            kb.append([InlineKeyboardButton(f"Toggle: {r[1]}", callback_data=f"adm:btoggle:{r[0]}"), InlineKeyboardButton(f"Delete #{r[0]}", callback_data=f"adm:bdelete:{r[0]}")])
        kb.append([InlineKeyboardButton("⬅️ Admin Panel", callback_data="adm:home")])
        await query.edit_message_text("🔘 **Button Management**\n\n" + ("\n".join(lines) if lines else "Button မရှိသေးပါ။"), parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))
    elif action == "addbutton":
        context.user_data["admin_state"] = S_BUTTON_NAME
        await query.edit_message_text("Button မှာပြမယ့် နာမည်ကို ပို့ပါ။")
    elif action.startswith("btoggle:"):
        bid = int(action.split(":")[1])
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE bot_buttons SET enabled=NOT enabled WHERE id=%s", (bid,))
        log_admin(f"Toggled button {bid}")
        await query.edit_message_text("Button အခြေအနေ ပြောင်းပြီးပါပြီ။ /admin မှ Buttons ကို ပြန်ဖွင့်ကြည့်ပါ။", reply_markup=back_keyboard())
    elif action.startswith("bdelete:"):
        bid = int(action.split(":")[1])
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM bot_buttons WHERE id=%s", (bid,))
        log_admin(f"Deleted button {bid}")
        await query.edit_message_text("Button ဖျက်ပြီးပါပြီ။", reply_markup=back_keyboard())
    elif action == "ads":
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id,message,enabled,placement FROM bot_ads ORDER BY id DESC LIMIT 10")
                rows = cur.fetchall()
        lines = [f"#{r[0]} {'🟢' if r[2] else '🔴'} [{r[3]}] {(r[1] or '')[:70]}" for r in rows]
        kb = [[InlineKeyboardButton("➕ Create Ad", callback_data="adm:addad")]]
        for r in rows:
            kb.append([InlineKeyboardButton(f"Toggle Ad #{r[0]}", callback_data=f"adm:ad_toggle:{r[0]}")])
        kb.append([InlineKeyboardButton("⬅️ Admin Panel", callback_data="adm:home")])
        await query.edit_message_text("📣 **Ads Management**\n\n" + ("\n".join(lines) if lines else "Ad မရှိသေးပါ။"), parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))
    elif action == "addad":
        context.user_data["admin_state"] = S_AD_TEXT
        await query.edit_message_text("Ad စာသားကို ပို့ပါ။")
    elif action.startswith("ad_toggle:"):
        aid = int(action.split(":")[1])
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE bot_ads SET enabled=NOT enabled WHERE id=%s", (aid,))
        log_admin(f"Toggled ad {aid}")
        await query.edit_message_text("Ad အခြေအနေ ပြောင်းပြီးပါပြီ။", reply_markup=back_keyboard())
    elif action == "logs":
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT action,created_at FROM admin_logs ORDER BY id DESC LIMIT 15")
                rows = cur.fetchall()
        body = "\n".join(f"{str(r[1])[:16]} — {r[0]}" for r in rows) or "Log မရှိသေးပါ။"
        await query.edit_message_text("🧾 **Admin Activity Logs**\n\n" + body, parse_mode="Markdown", reply_markup=back_keyboard())
    elif action == "backup":
        await query.edit_message_text("Backup ဖန်တီးနေပါတယ်...", reply_markup=back_keyboard())
        try:
            data = {}
            tables = ["users", "downloads", "ban_history", "bot_settings", "admin_logs", "bot_buttons", "bot_ads", "error_logs"]
            with db() as conn:
                with conn.cursor() as cur:
                    for table in tables:
                        cur.execute(f"SELECT * FROM {table}")
                        cols = [d.name for d in cur.description]
                        data[table] = [{k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in zip(cols, row)} for row in cur.fetchall()]
            file = BytesIO(json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))
            file.name = "tiktok_bot_backup.json"
            await context.bot.send_document(chat_id=ADMIN_ID, document=file, caption="Database Backup JSON")
            log_admin("Created database backup")
        except Exception as exc:
            log_error(exc)
            await context.bot.send_message(ADMIN_ID, f"Backup မအောင်မြင်ပါ: {str(exc)[:300]}")
    elif action == "security":
        await query.edit_message_text(f"🛡 Admin Security\n\nAuthorized Admin ID: `{ADMIN_ID}`\nAdmin Panel ကို ဒီ ID တစ်ခုတည်းသာ အသုံးပြုနိုင်ပါတယ်။\n\nBot token ကို ဘယ်သူ့ကိုမှ မပေးပါနဲ့။", parse_mode="Markdown", reply_markup=back_keyboard())

# ============================================================
# ADMIN TEXT / MEDIA INPUTS
# ============================================================
async def process_admin_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get("admin_state")
    msg = update.effective_message
    if not state:
        return False
    if not is_admin(update):
        context.user_data.pop("admin_state", None)
        return True

    if state == S_USER_SEARCH:
        raw = (msg.text or "").strip()
        if not raw.isdigit():
            await msg.reply_text("မှန်ကန်တဲ့ Telegram ID နံပါတ် ပို့ပါ။")
            return True
        uid = int(raw)
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT telegram_id,username,first_name,is_banned,joined_at FROM users WHERE telegram_id=%s", (uid,))
                user = cur.fetchone()
                cur.execute("SELECT COUNT(*) FROM downloads WHERE telegram_id=%s", (uid,))
                count = cur.fetchone()[0]
                cur.execute("SELECT video_url,downloaded_at FROM downloads WHERE telegram_id=%s ORDER BY id DESC LIMIT 5", (uid,))
                history = cur.fetchall()
        if not user:
            await msg.reply_text("ဒီ ID ကို Database ထဲမှာ မတွေ့ပါ။", reply_markup=back_keyboard())
        else:
            hist = "\n".join(f"{str(x[1])[:16]} {x[0]}" for x in history) or "မရှိသေးပါ။"
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🚫 Ban", callback_data=f"adm:ban:{uid}"), InlineKeyboardButton("✅ Unban", callback_data=f"adm:unban:{uid}")],
                [InlineKeyboardButton("⬅️ Admin Panel", callback_data="adm:home")],
            ])
            await msg.reply_text(f"👤 User Info\nID: {user[0]}\nUsername: @{user[1] or '-'}\nName: {user[2] or '-'}\nBanned: {user[3]}\nJoined: {user[4]}\nDownloads: {count}\n\nRecent:\n{hist}", reply_markup=kb)
        context.user_data.pop("admin_state", None)
        return True

    if state == S_BAN_REASON:
        uid = context.user_data.get("target_uid")
        reason = (msg.text or "Admin action").strip()
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE users SET is_banned=TRUE WHERE telegram_id=%s", (uid,))
                cur.execute("INSERT INTO ban_history(telegram_id,reason,expires_at) VALUES(%s,%s,NULL)", (uid, reason))
        log_admin(f"Banned user {uid}: {reason}")
        await msg.reply_text(f"User {uid} ကို Permanent Ban လုပ်ပြီးပါပြီ။", reply_markup=back_keyboard())
        context.user_data.pop("admin_state", None)
        return True

    if state == S_CUSTOM_BAN:
        uid = context.user_data.get("target_uid")
        raw = (msg.text or "").strip().lower()
        match = re.fullmatch(r"(\d+)\s*(m|h|d)", raw)
        if not match:
            await msg.reply_text("ဥပမာ 30m, 6h, 2d ပုံစံနဲ့ ပို့ပါ။")
            return True
        amount, unit = int(match.group(1)), match.group(2)
        if amount < 1 or amount > 365:
            await msg.reply_text("1 မှ 365 အတွင်း ထည့်ပါ။")
            return True
        delta = timedelta(minutes=amount) if unit == "m" else timedelta(hours=amount) if unit == "h" else timedelta(days=amount)
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE users SET is_banned=TRUE WHERE telegram_id=%s", (uid,))
                cur.execute("INSERT INTO ban_history(telegram_id,reason,expires_at) VALUES(%s,%s,NOW()+%s)", (uid, f"Temporary ban {raw}", delta))
        log_admin(f"Temporary ban {uid} for {raw}")
        await msg.reply_text(f"User {uid} ကို {raw} အတွက် Ban လုပ်ပြီးပါပြီ။", reply_markup=back_keyboard())
        context.user_data.pop("admin_state", None)
        return True

    if state == S_BROADCAST:
        context.user_data.pop("admin_state", None)
        await msg.reply_text("Broadcast စတင်ပါပြီ။ ပြီးဆုံးလျှင် ရလဒ်ပို့ပေးပါမယ်။")
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT telegram_id FROM users WHERE is_banned=FALSE")
                ids = [r[0] for r in cur.fetchall()]
        success = failed = 0
        for uid in ids:
            try:
                await context.bot.copy_message(chat_id=uid, from_chat_id=msg.chat_id, message_id=msg.message_id)
                success += 1
            except Exception:
                failed += 1
            await asyncio.sleep(0.04)
        log_admin(f"Broadcast completed: success={success}, failed={failed}")
        await context.bot.send_message(ADMIN_ID, f"📢 Broadcast ပြီးပါပြီ။\nSuccess: {success}\nFailed: {failed}")
        return True

    if state == S_WELCOME:
        if not msg.text:
            await msg.reply_text("စာသားပဲ ပို့ပေးပါ။")
            return True
        set_setting("welcome", msg.text[:4000])
        log_admin("Updated welcome message")
        context.user_data.pop("admin_state", None)
        await msg.reply_text("Welcome Message ပြောင်းပြီးပါပြီ။", reply_markup=back_keyboard())
        return True

    if state == S_FORCEJOIN:
        value = (msg.text or "").strip()
        set_setting("force_join", "" if value.lower() == "off" else value)
        log_admin(f"Force join set: {value}")
        context.user_data.pop("admin_state", None)
        await msg.reply_text("Force Join setting သိမ်းပြီးပါပြီ။", reply_markup=back_keyboard())
        return True

    if state == S_BUTTON_NAME:
        context.user_data["new_button_name"] = (msg.text or "").strip()[:50]
        context.user_data["admin_state"] = S_BUTTON_URL
        await msg.reply_text("Button နှိပ်ရင် သွားမယ့် https:// URL ကို ပို့ပါ။")
        return True

    if state == S_BUTTON_URL:
        url = (msg.text or "").strip()
        if not url.startswith("https://"):
            await msg.reply_text("https:// နဲ့စတဲ့ URL ပို့ပါ။")
            return True
        label = context.user_data.get("new_button_name", "Link")
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO bot_buttons(label,url,sort_order) VALUES(%s,%s,COALESCE((SELECT MAX(sort_order)+1 FROM bot_buttons),0))", (label, url))
        log_admin(f"Added button {label}")
        context.user_data.pop("admin_state", None)
        await msg.reply_text("Button အသစ် ထည့်ပြီးပါပြီ။", reply_markup=back_keyboard())
        return True

    if state == S_AD_TEXT:
        if not msg.text:
            await msg.reply_text("Ad စာသားကို ပို့ပါ။")
            return True
        context.user_data["ad_message"] = msg.text[:3000]
        context.user_data["admin_state"] = S_AD_BUTTON
        await msg.reply_text("Button စာသား ပို့ပါ။ Button မလိုရင် `none` လို့ပို့ပါ။")
        return True

    if state == S_AD_BUTTON:
        value = (msg.text or "").strip()
        context.user_data["ad_button"] = "" if value.lower() == "none" else value[:50]
        if not context.user_data["ad_button"]:
            context.user_data["admin_state"] = S_AD_PLACEMENT
            await msg.reply_text("Placement ကို welcome သို့မဟုတ် after_download လို့ပို့ပါ။")
        else:
            context.user_data["admin_state"] = S_AD_URL
            await msg.reply_text("Button URL (https://...) ပို့ပါ။")
        return True

    if state == S_AD_URL:
        url = (msg.text or "").strip()
        if not url.startswith("https://"):
            await msg.reply_text("https:// နဲ့စတဲ့ URL ပို့ပါ။")
            return True
        context.user_data["ad_url"] = url
        context.user_data["admin_state"] = S_AD_PLACEMENT
        await msg.reply_text("Placement ကို welcome သို့မဟုတ် after_download လို့ပို့ပါ။")
        return True

    if state == S_AD_PLACEMENT:
        placement = (msg.text or "").strip()
        if placement not in ("welcome", "after_download"):
            await msg.reply_text("welcome သို့မဟုတ် after_download ပဲ ရွေးပါ။")
            return True
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO bot_ads(message,button_text,button_url,placement,enabled) VALUES(%s,%s,%s,%s,FALSE)",
                    (context.user_data.get("ad_message"), context.user_data.get("ad_button") or None, context.user_data.get("ad_url") or None, placement))
        log_admin(f"Created ad at {placement}")
        context.user_data.pop("admin_state", None)
        await msg.reply_text("Ad ကို သိမ်းပြီးပါပြီ။ Ads ထဲကနေ Enable လုပ်ပါ။", reply_markup=back_keyboard())
        return True

    return False

# ============================================================
# BAN CALLBACKS / CANCEL / BACKUP RESTORE
# ============================================================
async def extra_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(update):
        await q.answer("Access denied", show_alert=True)
        return
    parts = q.data.split(":")
    if parts[0] != "adm":
        return
    if parts[1] == "ban":
        uid = int(parts[2])
        context.user_data["target_uid"] = uid
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("1 Hour", callback_data=f"adm:temp:{uid}:1h"), InlineKeyboardButton("6 Hours", callback_data=f"adm:temp:{uid}:6h")],
            [InlineKeyboardButton("1 Day", callback_data=f"adm:temp:{uid}:1d"), InlineKeyboardButton("7 Days", callback_data=f"adm:temp:{uid}:7d")],
            [InlineKeyboardButton("Custom", callback_data=f"adm:custom:{uid}"), InlineKeyboardButton("Permanent", callback_data=f"adm:perm:{uid}")],
            [InlineKeyboardButton("⬅️ Back", callback_data="adm:home")],
        ])
        await q.answer()
        await q.edit_message_text(f"User {uid} ကို ဘယ်လောက်ကြာ Ban လုပ်မလဲ?", reply_markup=kb)
    elif parts[1] == "unban":
        uid = int(parts[2])
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE users SET is_banned=FALSE WHERE telegram_id=%s", (uid,))
                cur.execute("INSERT INTO ban_history(telegram_id,reason,expires_at) VALUES(%s,'Unbanned by admin',NOW())", (uid,))
        log_admin(f"Unbanned user {uid}")
        await q.answer("Unbanned")
        await q.edit_message_text(f"User {uid} ကို Unban လုပ်ပြီးပါပြီ။", reply_markup=back_keyboard())
    elif parts[1] == "temp":
        uid, duration = int(parts[2]), parts[3]
        n, unit = int(duration[:-1]), duration[-1]
        delta = timedelta(hours=n) if unit == "h" else timedelta(days=n)
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE users SET is_banned=TRUE WHERE telegram_id=%s", (uid,))
                cur.execute("INSERT INTO ban_history(telegram_id,reason,expires_at) VALUES(%s,%s,NOW()+%s)", (uid, f"Temporary ban {duration}", delta))
        log_admin(f"Temporary ban {uid} for {duration}")
        await q.answer("Banned")
        await q.edit_message_text(f"User {uid} ကို {duration} အတွက် Ban လုပ်ပြီးပါပြီ။", reply_markup=back_keyboard())
    elif parts[1] == "perm":
        uid = int(parts[2])
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE users SET is_banned=TRUE WHERE telegram_id=%s", (uid,))
                cur.execute("INSERT INTO ban_history(telegram_id,reason,expires_at) VALUES(%s,'Permanent ban',NULL)", (uid,))
        log_admin(f"Permanent ban {uid}")
        await q.answer("Permanently banned")
        await q.edit_message_text(f"User {uid} ကို Permanent Ban လုပ်ပြီးပါပြီ။", reply_markup=back_keyboard())
    elif parts[1] == "custom":
        context.user_data["target_uid"] = int(parts[2])
        context.user_data["admin_state"] = S_CUSTOM_BAN
        await q.answer()
        await q.edit_message_text("Custom duration ပို့ပါ။ ဥပမာ 30m, 12h, 3d")
    elif parts[1] == "btoggle":
        bid = int(parts[2])
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE bot_buttons SET enabled=NOT enabled WHERE id=%s", (bid,))
        log_admin(f"Toggled button {bid}")
        await q.answer("Button အခြေအနေ ပြောင်းပြီးပါပြီ။")
        await q.edit_message_text("Button အခြေအနေ ပြောင်းပြီးပါပြီ။", reply_markup=back_keyboard())
    elif parts[1] == "bdelete":
        bid = int(parts[2])
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM bot_buttons WHERE id=%s", (bid,))
        log_admin(f"Deleted button {bid}")
        await q.answer("Button ဖျက်ပြီးပါပြီ။")
        await q.edit_message_text("Button ဖျက်ပြီးပါပြီ။", reply_markup=back_keyboard())
    elif parts[1] == "ad_toggle":
        aid = int(parts[2])
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE bot_ads SET enabled=NOT enabled WHERE id=%s", (aid,))
        log_admin(f"Toggled ad {aid}")
        await q.answer("Ad အခြေအနေ ပြောင်းပြီးပါပြီ။")
        await q.edit_message_text("Ad အခြေအနေ ပြောင်းပြီးပါပြီ။", reply_markup=back_keyboard())


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    context.user_data.pop("admin_state", None)
    await update.message.reply_text("လုပ်ဆောင်မှု ပယ်ဖျက်ပြီးပါပြီ။", reply_markup=admin_keyboard())


async def restore_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update) or not update.message.document:
        return
    doc = update.message.document
    if not doc.file_name or not doc.file_name.endswith(".json"):
        await update.message.reply_text("JSON backup ဖိုင်ပဲ လက်ခံပါတယ်။")
        return
    tg_file = await context.bot.get_file(doc.file_id)
    content = await tg_file.download_as_bytearray()
    try:
        data = json.loads(bytes(content).decode("utf-8"))
        allowed = {"users", "downloads", "ban_history", "bot_settings", "admin_logs", "bot_buttons", "bot_ads", "error_logs"}
        if not isinstance(data, dict) or not set(data).issubset(allowed):
            raise ValueError("Invalid backup structure")
        with db() as conn:
            with conn.cursor() as cur:
                for table, rows in data.items():
                    if not isinstance(rows, list):
                        raise ValueError("Invalid table data")
                    for row in rows:
                        if not isinstance(row, dict):
                            raise ValueError("Invalid row")
                        cols = list(row.keys())
                        if not cols or any(not re.fullmatch(r"[a-z_]+", c) for c in cols):
                            raise ValueError("Invalid columns")
                        vals = [row[c] for c in cols]
                        placeholders = ",".join(["%s"] * len(cols))
                        col_sql = ",".join(cols)
                        cur.execute(f"INSERT INTO {table} ({col_sql}) VALUES ({placeholders}) ON CONFLICT DO NOTHING", vals)
        log_admin("Restored database backup (merge mode)")
        await update.message.reply_text("Backup data ကို Merge Restore လုပ်ပြီးပါပြီ။ ရှိပြီးသား data များကို မဖျက်ပါ။")
    except Exception as exc:
        log_error(exc)
        await update.message.reply_text(f"Restore မအောင်မြင်ပါ: {str(exc)[:300]}")

# ============================================================
# NORMAL MESSAGE HANDLER
# ============================================================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg:
        return
    if is_admin(update) and context.user_data.get("admin_state"):
        handled = await process_admin_input(update, context)
        if handled:
            return
    if not msg.text:
        return
    user = update.effective_user
    if not user:
        return
    await asyncio.to_thread(save_user, user)
    if user.id != ADMIN_ID:
        try:
            if await asyncio.to_thread(user_is_banned, user.id):
                await msg.reply_text("သင့်အကောင့်ကို Bot အသုံးပြုခွင့် ပိတ်ထားပါတယ်။")
                return
        except Exception as exc:
            logger.exception("Ban check failed")
            log_error(exc)
        if setting("maintenance", "off") == "on":
            await msg.reply_text("Bot ကို ယာယီပြုပြင်ထိန်းသိမ်းနေပါတယ်။")
            return
        channel = setting("force_join", "").strip()
        if channel:
            try:
                member = await context.bot.get_chat_member(chat_id=channel, user_id=user.id)
                if member.status not in ("member", "administrator", "creator"):
                    await msg.reply_text(f"Bot အသုံးပြုရန် {channel} ကို အရင် Join လုပ်ပါ။")
                    return
            except Exception:
                await msg.reply_text("Force Join စစ်ဆေးမရပါ။ Channel username မှန်ကန်မှုနှင့် Bot Admin ဖြစ်မှုကို စစ်ပါ။")
                return
    match = re.search(r"(https?://[^\s]*tiktok\.com[^\s]*)", msg.text.strip())
    if not match:
        await msg.reply_text("TikTok link ပို့ပေးပါ။")
        return
    url = match.group(1).rstrip(".,)")
    status = await msg.reply_text("ဒေါင်းလုဒ်ဆွဲနေပါတယ် ခဏစောင့်ပါ...")
    try:
        video_bytes, video_url, error = await asyncio.to_thread(fetch_video_bytes, url)
        if error:
            await status.edit_text(f"ဗီဒီယို ဆွဲမရပါ။\nအကြောင်းရင်း: {error}")
            return
        if video_bytes:
            f = BytesIO(video_bytes)
            f.name = "tiktok.mp4"
            await msg.reply_video(video=f, caption="Watermark မပါသော ဗီဒီယို ရပါပြီ ✨")
            await asyncio.to_thread(save_download, user.id, url)
            await status.delete()
            await send_ad(update, context, "after_download")
        elif video_url:
            await msg.reply_video(video=video_url, caption="Watermark မပါသော ဗီဒီယို ရပါပြီ ✨")
            await asyncio.to_thread(save_download, user.id, url)
            await status.delete()
            await send_ad(update, context, "after_download")
        else:
            await status.edit_text("ဗီဒီယို လင့်ခ် ရှာမတွေ့ပါ။")
    except Exception as exc:
        logger.exception("Message handling failed")
        log_error(exc)
        await status.edit_text("အမှားတစ်ခု ဖြစ်ပေါ်နေပါသည်။ ခဏနေ ပြန်စမ်းပါ။")

# ============================================================
# START
# ============================================================
if __name__ == "__main__":
    init_database()
    threading.Thread(target=run_flask, daemon=True).start()
    threading.Thread(target=keep_alive_ping, daemon=True).start()
    bot_app = ApplicationBuilder().token(TOKEN).build()
    bot_app.add_handler(CommandHandler("start", start_command))
    bot_app.add_handler(CommandHandler("admin", admin_command))
    bot_app.add_handler(CommandHandler("cancel", cancel_command))
    bot_app.add_handler(CallbackQueryHandler(admin_callback, pattern=r"^adm:(home|stats|users|broadcast|downloads|settings|maintenance|welcome|forcejoin|buttons|addbutton|ads|addad|logs|backup|security)$"))
    bot_app.add_handler(CallbackQueryHandler(extra_callback, pattern=r"^adm:(ban|unban|temp|perm|custom|btoggle|bdelete|ad_toggle):"))
    bot_app.add_handler(MessageHandler(filters.Document.ALL, restore_document))
    bot_app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))
    logger.info("Bot is starting...")
    bot_app.run_polling(drop_pending_updates=True)
