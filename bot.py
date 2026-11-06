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
                        data[table] = [{k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in zip(cols, row)} for row in cur.fetchall
