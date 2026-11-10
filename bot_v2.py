import os
import re
import json
import time
import asyncio
import logging
import tempfile
import subprocess
import sys
from io import BytesIO
from collections import defaultdict
from datetime import datetime, timezone
import psycopg
from psycopg.rows import dict_row
from flask import Flask, jsonify
from curl_cffi import requests as c_requests
from telegram.error import BadRequest
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
)
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# ---------------------------------------------------------------------
# P TikTok V2 - isolated test deployment
# Required environment variables:
#   BOT_TOKEN, DATABASE_URL
# Optional:
#   ADMIN_ID (default 8556272994), RENDER_URL (test service URL only)
#   MAX_FILE_MB (default 45), CONTACT_USERNAME (default P_digital3)
# ---------------------------------------------------------------------

TOKEN = os.getenv("BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "8556272994"))
RENDER_URL = os.getenv("RENDER_URL", "").strip().rstrip("/")
MAX_FILE_MB = int(os.getenv("MAX_FILE_MB", "45"))
CONTACT_USERNAME = os.getenv("CONTACT_USERNAME", "P_digital3").lstrip("@")
API_URL = "https://tikwm.com/api/"
RATE_LIMITS = {"minute": 3, "hour": 20, "day": 100}
QUALITY_OPTIONS = ("720p", "1080p", "original")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("p-tiktok-v2")
web = Flask(__name__)

if not TOKEN or not DATABASE_URL:
    raise RuntimeError("BOT_TOKEN and DATABASE_URL environment variables are required")

# ---------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------

def db():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=15)

def init_db():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                joined_at TIMESTAMPTZ DEFAULT NOW(),
                last_seen TIMESTAMPTZ DEFAULT NOW(),
                is_banned BOOLEAN DEFAULT FALSE,
                ban_until TIMESTAMPTZ,
                ban_reason TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS downloads (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT,
                username TEXT,
                video_url TEXT,
                downloaded_at TIMESTAMPTZ DEFAULT NOW(),
                status TEXT DEFAULT 'success',
                media_type TEXT DEFAULT 'video',
                quality TEXT DEFAULT 'original',
                error TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS request_events (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_buttons (
                id BIGSERIAL PRIMARY KEY,
                label TEXT NOT NULL,
                url TEXT NOT NULL,
                enabled BOOLEAN DEFAULT TRUE
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_ads (
                id BIGSERIAL PRIMARY KEY,
                placement TEXT NOT NULL DEFAULT 'after_download',
                text TEXT NOT NULL,
                enabled BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS admin_logs (
                id BIGSERIAL PRIMARY KEY,
                admin_id BIGINT,
                action TEXT,
                details TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS error_logs (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT,
                error TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS backup_log (
                id BIGSERIAL PRIMARY KEY,
                status TEXT,
                details TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        # Additive migrations: safe for an existing V1 database.
        conn.execute("ALTER TABLE downloads ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'success'")
        conn.execute("ALTER TABLE downloads ADD COLUMN IF NOT EXISTS media_type TEXT DEFAULT 'video'")
        conn.execute("ALTER TABLE downloads ADD COLUMN IF NOT EXISTS quality TEXT DEFAULT 'original'")
        conn.execute("ALTER TABLE downloads ADD COLUMN IF NOT EXISTS error TEXT")
        conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS last_seen TIMESTAMPTZ DEFAULT NOW()")
        conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_banned BOOLEAN DEFAULT FALSE")
        conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS ban_until TIMESTAMPTZ")
        conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS ban_reason TEXT")
        conn.execute("""
            INSERT INTO bot_settings(key,value) VALUES
            ('maintenance','off'),
            ('welcome','မင်္ဂလာပါ။deflink ပို့ပြီး Download လုပ်နိုင်ပါတယ်။'),
            ('rate_minute','3'), ('rate_hour','20'), ('rate_day','100')
            ON CONFLICT(key) DO NOTHING
        """)

def setting(key, default=""):
    with db() as conn:
        row = conn.execute("SELECT value FROM bot_settings WHERE key=%s", (key,)).fetchone()
        return row["value"] if row else default

def set_setting(key, value):
    with db() as conn:
        conn.execute("""
            INSERT INTO bot_settings(key,value) VALUES(%s,%s)
            ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value
        """, (key, str(value)))

def log_admin(action, details=""):
    with db() as conn:
        conn.execute(
            "INSERT INTO admin_logs(admin_id,action,details) VALUES(%s,%s,%s)",
            (ADMIN_ID, action, details)
        )

def record_user(user):
    with db() as conn:
        conn.execute("""
            INSERT INTO users(user_id,username,first_name,last_seen)
            VALUES(%s,%s,%s,NOW())
            ON CONFLICT(user_id) DO UPDATE SET
              username=EXCLUDED.username,
              first_name=EXCLUDED.first_name,
              last_seen=NOW()
        """, (user.id, user.username, user.first_name))

def user_status(uid):
    with db() as conn:
        row = conn.execute(
            "SELECT is_banned,ban_until,ban_reason FROM users WHERE user_id=%s",
            (uid,)
        ).fetchone()
    if not row:
        return False, ""
    if row["is_banned"] and row["ban_until"] and row["ban_until"] < datetime.now(timezone.utc):
        with db() as conn:
            conn.execute(
                "UPDATE users SET is_banned=FALSE,ban_until=NULL,ban_reason=NULL WHERE user_id=%s",
                (uid,)
            )
        return False, ""
    return bool(row["is_banned"]), row["ban_reason"] or ""

def check_rate_limit(uid):
    if uid == ADMIN_ID:
        return True, ""
    with db() as conn:
        row = conn.execute("""
            SELECT
              COUNT(*) FILTER (WHERE created_at > NOW()-INTERVAL '1 minute') AS m,
              COUNT(*) FILTER (WHERE created_at > NOW()-INTERVAL '1 hour') AS h,
              COUNT(*) FILTER (WHERE created_at > NOW()-INTERVAL '1 day') AS d
            FROM request_events WHERE user_id=%s
        """, (uid,)).fetchone()
        m = int(setting("rate_minute", "3"))
        h = int(setting("rate_hour", "20"))
        d = int(setting("rate_day", "100"))
        if row["m"] >= m:
            return False, f"တစ်မိနစ်အတွင်း Request {m} ကြိမ်ပဲ လုပ်နိုင်ပါတယ်။"
        if row["h"] >= h:
            return False, f"တစ်နာရီအတွင်း Request {h} ကြိမ်ပဲ လုပ်နိုင်ပါတယ်။"
        if row["d"] >= d:
            return False, f"တစ်ရက်အတွင်း Request {d} ကြိမ်ပဲ လုပ်နိုင်ပါတယ်။"
        conn.execute("INSERT INTO request_events(user_id) VALUES(%s)", (uid,))
    return True, ""

# ---------------------------------------------------------------------
# TikTok API / media helpers
# ---------------------------------------------------------------------

def get_tiktok_data(url):
    session = c_requests.Session(impersonate="chrome120")

    # 1. TikWM API ကို အရင်စမ်းမယ်
    try:
        response = session.post(
            "https://www.tikwm.com/api/",
            data={"url": url, "hd": 1},
            headers={"Referer": "https://www.tikwm.com/"},
            timeout=25
        )
        response.raise_for_status()
        payload = response.json()

        if payload.get("code") == 0 and payload.get("data"):
            return payload["data"], session

        log.warning("TikWM response: %s", str(payload)[:300])

    except Exception as exc:
        log.warning("TikWM API failed: %s", str(exc)[:200])

    # 2. TikTok Page ကို Browser ပုံစံနဲ့ ဖတ်မယ်
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 10; K) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Mobile Safari/537.36"
            ),
            "Referer": "https://www.tiktok.com/",
            "Accept-Language": "en-US,en;q=0.9",
        }

        response = session.get(
            url,
            headers=headers,
            allow_redirects=True,
            timeout=30
        )
        response.raise_for_status()
        html = response.text

        # Post ID ရှာမယ်
        post_match = re.search(r"/(?:photo|video)/(\d+)", response.url)
        if not post_match:
            post_match = re.search(r"/(?:photo|video)/(\d+)", url)

        post_id = post_match.group(1) if post_match else None

        # TikTok မှာ အသုံးပြုတဲ့ JSON script မျိုးစုံကို စစ်မယ်
        script_ids = [
            "__UNIVERSAL_DATA_FOR_REHYDRATION__",
            "SIGI_STATE",
            "__NEXT_DATA__",
        ]

        page_objects = []

        for script_id in script_ids:
            pattern = (
                r'<script[^>]*id=["\']'
                + re.escape(script_id)
                + r'["\'][^>]*>(.*?)</script>'
            )
            matches = re.findall(pattern, html, re.S | re.I)

            for raw in matches:
                try:
                    page_objects.append(json.loads(raw))
                except Exception:
                    continue

        if not page_objects:
            raise ValueError("TikTok Page JSON Data မတွေ့ပါ။")

        # JSON အတွင်းရှိ သက်ဆိုင်ရာ Post ကို ရှာမယ်
        candidates = []

        def scan(node):
            if isinstance(node, dict):
                node_id = str(
                    node.get("id")
                    or node.get("itemId")
                    or node.get("aweme_id")
                    or ""
                )

                if (
                    "imagePost" in node
                    or "itemStruct" in node
                    or "video" in node
                ):
                    candidates.append((node_id, node))

                for value in node.values():
                    scan(value)

            elif isinstance(node, list):
                for value in node:
                    scan(value)

        for obj in page_objects:
            scan(obj)

        selected = None

        if post_id:
            for node_id, node in candidates:
                if node_id == post_id:
                    selected = node
                    break

        if not selected:
            for node_id, node in candidates:
                if "imagePost" in node:
                    selected = node
                    break

        if not selected and candidates:
            selected = candidates[0][1]

        if not selected:
            raise ValueError("TikTok Post Data မတွေ့ပါ။")

        # itemStruct အတွင်းရှိ Data ကို ထပ်စစ်မယ်
        item = selected.get("itemStruct") or selected
        image_post = item.get("imagePost") or selected.get("imagePost") or {}
        images = image_post.get("images") or []

        photo_urls = []

        def collect_urls(node, found):
            if isinstance(node, dict):
                for key, value in node.items():
                    if key in ("urlList", "url_list"):
                        if isinstance(value, list):
                            found.extend(
                                x for x in value
                                if isinstance(x, str)
                                and x.startswith("http")
                            )
                    else:
                        collect_urls(value, found)

            elif isinstance(node, list):
                for value in node:
                    collect_urls(value, found)

        for image in images:
            found_urls = []
            collect_urls(image, found_urls)

            unique_urls = list(dict.fromkeys(found_urls))

            if unique_urls:
                photo_urls.append({
                    "display_image": {
                        "url_list": unique_urls
                    }
                })

        if photo_urls:
            return {"images": photo_urls}, session

        # Video Post ဖြစ်နေရင် Video Data ပြန်ပေးမယ်
        video = item.get("video") or selected.get("video") or {}

        video_url = (
            video.get("playAddr")
            or video.get("downloadAddr")
            or video.get("play_addr")
        )

        if isinstance(video_url, dict):
            video_url = (
                video_url.get("url_list") or [None]
            )[0]

        if video_url:
            return {"play": video_url}, session

        raise ValueError("Photo URL နှင့် Video URL မတွေ့ပါ။")

    except Exception as exc:
        raise RuntimeError(
            "TikWM API နှင့် TikTok Fallback နှစ်ခုလုံး "
            f"မအောင်မြင်ပါ: {str(exc)[:300]}"
        )



def choose_video_url(data, quality):
    candidates = []
    for key in ("play", "wmplay", "hdplay", "play2"):
        value = data.get(key)
        if value and isinstance(value, str):
            candidates.append(value)

    if not candidates:
        raise ValueError("Video download link မတွေ့ပါ။")

    if quality in ("1080p", "original") and data.get("hdplay"):
        return data["hdplay"]

    return data.get("play") or candidates[0]


def fetch_bytes(session, url):
    headers = {
        "Referer": "https://www.tikwm.com/",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }

    response = session.get(url, headers=headers, timeout=90)

    if response.status_code == 403:
        response = session.get(
            url,
            headers={**headers, "Referer": "https://www.tiktok.com/"},
            timeout=90
        )

    response.raise_for_status()
    data = response.content

    if len(data) > MAX_FILE_MB * 1024 * 1024:
        raise ValueError(
            f"ဖိုင်အရွယ်အစား {MAX_FILE_MB}MB ထက်ကျော်နေပါတယ်။"
        )

    return data


def get_photo_urls(data):
    images = data.get("images") or []
    urls = []
    for item in images:
        if not isinstance(item, dict):
            continue
        display = item.get("display_image")
        url_list = display.get("url_list", []) if isinstance(display, dict) else []
        if url_list and isinstance(url_list[0], str):
            urls.append(url_list[0])
        elif isinstance(item.get("url"), str):
            urls.append(item["url"])
    return urls


def download_tiktok_photos(url):
    with tempfile.TemporaryDirectory() as temp_dir:
        command = [
            sys.executable,
            "-m", "gallery_dl",
            "-D", temp_dir,
            url
        ]

        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=180
        )

        image_extensions = (".jpg", ".jpeg", ".png", ".webp")
        photo_paths = []

        for root, dirs, files in os.walk(temp_dir):
            for filename in files:
                if filename.lower().endswith(image_extensions):
                    photo_paths.append(os.path.join(root, filename))

        photo_paths.sort()

        if result.returncode != 0 or not photo_paths:
            error = (result.stderr or result.stdout or "Photo မတွေ့ပါ။")[-500:]
            raise RuntimeError("Photo download မအောင်မြင်ပါ။ " + error)

        photos = []
        for path in photo_paths:
            with open(path, "rb") as photo_file:
                photos.append(photo_file.read())

        return photos


async def get_tiktok_photos(url):
    return await asyncio.to_thread(download_tiktok_photos, url)


def extract_mp3(video_bytes):
    try:
        import imageio_ffmpeg
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:
        raise RuntimeError("MP3 ပြောင်းရန် ffmpeg မရရှိပါ။") from exc

    with tempfile.TemporaryDirectory() as temp:
        src = os.path.join(temp, "input.mp4")
        dst = os.path.join(temp, "audio.mp3")
        with open(src, "wb") as f:
            f.write(video_bytes)
        result = subprocess.run(
            [ffmpeg, "-y", "-i", src, "-vn", "-acodec", "libmp3lame",
             "-q:a", "3", dst],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120
        )
        if result.returncode != 0 or not os.path.exists(dst):
            raise RuntimeError("ဒီ Video မှ MP3 ထုတ်မရပါ။")
        with open(dst, "rb") as f:
            return f.read()

# ---------------------------------------------------------------------
# Telegram UI
# ---------------------------------------------------------------------

def is_admin(uid):
    return uid == ADMIN_ID

async def main_keyboard():
    rows = [
        [InlineKeyboardButton("📘 Download Guide", callback_data="guide"),
         InlineKeyboardButton("📂 My History", callback_data="history")],
        [InlineKeyboardButton("❓ Help", callback_data="help"),
         InlineKeyboardButton("☎️ Contact Admin", url=f"https://t.me/{CONTACT_USERNAME}")],
    ]
    try:
        with db() as conn:
            buttons = conn.execute(
                "SELECT label,url FROM bot_buttons WHERE enabled=TRUE ORDER BY id ASC"
            ).fetchall()
        for button in buttons:
            label = str(button["label"]).strip()[:64]
            url = str(button["url"]).strip()
            if label and url.startswith(("https://", "http://")):
                rows.append([InlineKeyboardButton(label, url=url)])
    except Exception:
        log.exception("Could not load custom start buttons")
    return InlineKeyboardMarkup(rows)

def admin_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Dashboard", callback_data="adm_stats"),
         InlineKeyboardButton("👥 Users", callback_data="adm_users")],
        [InlineKeyboardButton("📥 Recent Downloads", callback_data="adm_downloads"),
         InlineKeyboardButton("⚙️ Settings", callback_data="adm_settings")],
        [InlineKeyboardButton("📢 Broadcast", callback_data="adm_broadcast_help"),
         InlineKeyboardButton("🚫 Ban / Unban", callback_data="adm_ban_help")],
        [InlineKeyboardButton("🔘 Buttons", callback_data="adm_buttons"),
         InlineKeyboardButton("📣 Ads", callback_data="adm_ads")],
        [InlineKeyboardButton("🧾 Error Logs", callback_data="adm_errors"),
         InlineKeyboardButton("💾 Backup", callback_data="adm_backup")],
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    record_user(user)
    banned, reason = user_status(user.id)
    if banned:
        await update.message.reply_text("အသုံးပြုခွင့် ပိတ်ထားပါတယ်။ " + reason)
        return
    welcome = setting("welcome", "မင်္ဂလာပါ။ TikTok link ပို့ပြီး Download လုပ်နိုင်ပါတယ်။")
    await update.message.reply_text(welcome, reply_markup=await main_keyboard())
    await send_ads(update.effective_chat.id, "welcome", context)
    if is_admin(user.id):
        await update.message.reply_text("Admin Panel", reply_markup=admin_keyboard())

async def send_ads(chat_id, placement, context):
    with db() as conn:
        rows = conn.execute(
            "SELECT text FROM bot_ads WHERE enabled=TRUE AND placement=%s ORDER BY id",
            (placement,)
        ).fetchall()
    for row in rows:
        await context.bot.send_message(chat_id=chat_id, text=row["text"])

async def show_history(query):
    uid = query.from_user.id
    with db() as conn:
        rows = conn.execute("""
            SELECT video_url,downloaded_at,status,media_type
            FROM downloads WHERE user_id=%s ORDER BY id DESC LIMIT 10
        """, (uid,)).fetchall()
    if not rows:
        await query.edit_message_text("Download history မရှိသေးပါ။", reply_markup=await main_keyboard())
        return
    lines = ["📂 နောက်ဆုံး Download ၁၀ ခု"]
    for r in rows:
        stamp = r["downloaded_at"].strftime("%Y-%m-%d %H:%M")
        lines.append(f"• {r['media_type']} | {r['status']} | {stamp}\n{r['video_url']}")
    await query.edit_message_text("\n\n".join(lines), reply_markup=await main_keyboard())

async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    if data == "guide":
        await q.edit_message_text(
            "📘 အသုံးပြုနည်း\n1. TikTok video/photo link ကို Copy လုပ်ပါ။\n"
            "2. ဒီ Bot ထဲ Paste လုပ်ပို့ပါ။\n3. Video ဖြစ်ရင် Quality ရွေးပါ။\n"
            "4. MP3 လိုချင်ရင် MP3 ကိုရွေးပါ။",
            reply_markup=await main_keyboard()
        )
    elif data == "help":
        await q.edit_message_text(
            "TikTok link ပို့ပြီး Download လုပ်နိုင်ပါတယ်။ မရပါက ခဏစောင့်ပြီး ပြန်စမ်းပါ။\n"
            f"အကူအညီ: @{CONTACT_USERNAME}",
            reply_markup=await main_keyboard()
        )
    elif data == "history":
        await show_history(q)
    elif data.startswith("quality:"):
        _, quality, url = data.split(":", 2)
        context.user_data["pending_url"] = url
        context.user_data["pending_quality"] = quality
        await q.edit_message_text(
            f"ရွေးထားသော Quality: {quality}\nဘာလိုချင်ပါသလဲ?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🎬 Video", callback_data="type:video"),
                 InlineKeyboardButton("🎵 MP3", callback_data="type:mp3")]
            ])
        )
    elif data.startswith("type:"):
        kind = data.split(":", 1)[1]
        url = context.user_data.get("pending_url")
        quality = context.user_data.get("pending_quality", "original")
        if not url:
            await q.edit_message_text("Link သက်တမ်းကုန်သွားပါပြီ။ Link ပြန်ပို့ပါ။")
            return
        await q.edit_message_text("⏳ Download လုပ်နေပါတယ်။ ခဏစောင့်ပါ...")
        await process_download(q.message.chat_id, q.from_user.id, q.from_user.username,
                               url, quality, kind, context)
    elif data.startswith("admin:") or data.startswith("adm_"):
        if not is_admin(q.from_user.id):
            await q.edit_message_text("Admin only.")
            return
        await admin_callback(q, context)

async def admin_callback(q, context):
    action = q.data
    if action == "adm_stats":
        with db() as conn:
            users = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
            today = conn.execute(
                "SELECT COUNT(*) AS n FROM downloads WHERE downloaded_at::date=CURRENT_DATE AND status='success'"
            ).fetchone()["n"]
            total = conn.execute(
                "SELECT COUNT(*) AS n FROM downloads WHERE status='success'"
            ).fetchone()["n"]
        await q.edit_message_text(
            f"📊 Dashboard\nUsers: {users}\nDownloads total: {total}\nToday: {today}",
            reply_markup=admin_keyboard()
        )
    elif action == "adm_users":
        with db() as conn:
            total = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
            active = conn.execute(
                "SELECT COUNT(*) AS n FROM users WHERE last_seen>NOW()-INTERVAL '7 days'"
            ).fetchone()["n"]
            top = conn.execute("""
                SELECT user_id,COUNT(*) AS n FROM downloads
                WHERE status='success' GROUP BY user_id ORDER BY n DESC LIMIT 5
            """).fetchall()
        text = f"👥 Users: {total}\nActive (7 days): {active}\n\nTop download users:\n"
        text += "\n".join(f"{r['user_id']} — {r['n']}" for r in top) or "မရှိသေးပါ"
        await q.edit_message_text(text, reply_markup=admin_keyboard())
    elif action == "adm_downloads":
        with db() as conn:
            rows = conn.execute(
                "SELECT user_id,media_type,status,downloaded_at FROM downloads ORDER BY id DESC LIMIT 10"
            ).fetchall()
        text = "📥 Recent Downloads\n" + "\n".join(
            f"{r['user_id']} | {r['media_type']} | {r['status']} | {r['downloaded_at']:%m-%d %H:%M}"
            for r in rows
        )
        await q.edit_message_text(text, reply_markup=admin_keyboard())
    elif action == "adm_settings":
        await q.edit_message_text(
            f"Maintenance: {setting('maintenance','off')}\n"
            f"Limits: {setting('rate_minute','3')}/min, {setting('rate_hour','20')}/hour, "
            f"{setting('rate_day','100')}/day\n\n"
            "Commands:\n/maintenance on|off\n/setlimit minute 3\n/setlimit hour 20\n/setlimit day 100",
            reply_markup=admin_keyboard()
        )
    elif action == "adm_broadcast_help":
        await q.edit_message_text("Broadcast ပို့ရန် /broadcast စာသား ကို အသုံးပြုပါ။",
                                  reply_markup=admin_keyboard())
    elif action == "adm_ban_help":
        await q.edit_message_text(
            "အသုံးပြုနည်း:\n/ban USER_ID အကြောင်းပြချက်\n"
            "/tempban USER_ID မိနစ် အကြောင်းပြချက်\n/unban USER_ID",
            reply_markup=admin_keyboard()
        )
    elif action == "adm_buttons":
        with db() as conn:
            rows = conn.execute("SELECT id,label,url,enabled FROM bot_buttons ORDER BY id DESC").fetchall()
        text = "🔘 Buttons\n" + "\n".join(
            f"{r['id']}. {r['label']} — {'ON' if r['enabled'] else 'OFF'} — {r['url']}"
            for r in rows
        )
        text += "\n\nထည့်ရန်: /addbutton နာမည် https://URL\nဖျက်ရန်: /delbutton ID"
        await q.edit_message_text(text or "Buttons မရှိသေးပါ။", reply_markup=admin_keyboard())
    elif action == "adm_ads":
        with db() as conn:
            rows = conn.execute("SELECT id,placement,text,enabled FROM bot_ads ORDER BY id DESC").fetchall()
        text = "📣 Ads\n" + "\n".join(
            f"{r['id']} [{r['placement']}] {'ON' if r['enabled'] else 'OFF'}: {r['text'][:80]}"
            for r in rows
        )
        text += "\n\nထည့်ရန်: /addad welcome|after_download စာသား\n"
        text += "ဖွင့်/ပိတ်: /togglead ID\nဖျက်ရန်: /delad ID"
        await q.edit_message_text(text or "Ads မရှိသေးပါ။", reply_markup=admin_keyboard())
    elif action == "adm_errors":
        with db() as conn:
            rows = conn.execute("SELECT error,created_at FROM error_logs ORDER BY id DESC LIMIT 8").fetchall()
        text = "🧾 Error Logs\n" + "\n".join(
            f"{r['created_at']:%m-%d %H:%M}: {r['error'][:180]}" for r in rows
        )
        await q.edit_message_text(text or "Error မရှိပါ။", reply_markup=admin_keyboard())
    elif action == "adm_backup":
        await create_backup(q.message.chat_id, context)
    else:
        await q.edit_message_text("မသိသော command", reply_markup=admin_keyboard())

async def process_download(chat_id, uid, username, url, quality, kind, context):
    if setting("maintenance", "off").lower() == "on" and uid != ADMIN_ID:
        await context.bot.send_message(chat_id, "Bot maintenance လုပ်နေပါတယ်။ နောက်မှပြန်စမ်းပါ။")
        return

    banned, reason = user_status(uid)
    if banned:
        await context.bot.send_message(chat_id, "အသုံးပြုခွင့် ပိတ်ထားပါတယ်။ " + reason)
        return

    allowed, message = check_rate_limit(uid)
    if not allowed:
        await context.bot.send_message(chat_id, message)
        return

    lock = user_locks.setdefault(uid, asyncio.Lock())
    if lock.locked():
        await context.bot.send_message(chat_id, "Download တစ်ခု လုပ်နေဆဲပါ။ ပြီးမှ ထပ်လုပ်ပါ။")
        return

    async with lock:
        try:
            data = {}
            media_session = None
            photo_bytes = []
            media_type = kind

            try:
                data, media_session = await asyncio.to_thread(get_tiktok_data, url)
            except Exception as exc:
                log.warning("TikTok metadata lookup failed: %s", str(exc)[:200])

            photos = get_photo_urls(data) if data else []

            if photos:
                if kind == "mp3":
                    raise ValueError("Photo slideshow မှ MP3 မထုတ်နိုင်ပါ။")
                for photo_url in photos:
                    photo_bytes.append(
                        await asyncio.to_thread(fetch_bytes, media_session, photo_url)
                    )
            elif "/photo/" in url:
                if kind == "mp3":
                    raise ValueError("Photo slideshow မှ MP3 မထုတ်နိုင်ပါ။")
                photo_bytes = await get_tiktok_photos(url)

            if photo_bytes:
                for index, photo in enumerate(photo_bytes, start=1):
                    photo_file = BytesIO(photo)
                    photo_file.name = f"tiktok_photo_{index}.jpg"
                    if len(photo) > 10 * 1024 * 1024:
                        await context.bot.send_document(chat_id=chat_id, document=photo_file)
                    else:
                        await context.bot.send_photo(chat_id=chat_id, photo=photo_file)
                media_type = "photo"
            else:
                if not data:
                    raise ValueError("TikTok Post Data မတွေ့ပါ။")

                video_url = choose_video_url(data, quality)
                if kind == "mp3":
                    video_bytes = await asyncio.to_thread(fetch_bytes, media_session, video_url)
                    audio = await asyncio.to_thread(extract_mp3, video_bytes)
                    audio_file = BytesIO(audio)
                    audio_file.name = "tiktok_audio.mp3"
                    await context.bot.send_audio(chat_id=chat_id, audio=audio_file)
                    media_type = "mp3"
                else:
                    try:
                        video_bytes = await asyncio.to_thread(fetch_bytes, media_session, video_url)
                        video_file = BytesIO(video_bytes)
                        video_file.name = "tiktok_video.mp4"
                        await context.bot.send_video(
                            chat_id=chat_id,
                            video=video_file,
                            supports_streaming=True,
                            read_timeout=120,
                            write_timeout=120
                        )
                    except Exception as download_error:
                        log.warning(
                            "Direct download failed, trying Telegram URL: %s",
                            str(download_error)[:200]
                        )
                        await context.bot.send_video(
                            chat_id=chat_id,
                            video=video_url,
                            supports_streaming=True,
                            read_timeout=120,
                            write_timeout=120
                        )
                    media_type = "video"

            with db() as conn:
                conn.execute("""
                    INSERT INTO downloads(user_id,username,video_url,status,media_type,quality)
                    VALUES(%s,%s,%s,'success',%s,%s)
                """, (uid, username, url, media_type, quality))

            await context.bot.send_message(
                chat_id,
                "✅ ပြီးပါပြီ။",
                reply_markup=await main_keyboard()
            )
            await send_ads(chat_id, "after_download", context)

        except Exception as exc:
            log.exception("Download failed for %s", uid)
            error = str(exc)[:1000]
            try:
                with db() as conn:
                    conn.execute(
                        "INSERT INTO downloads(user_id,username,video_url,status,media_type,quality,error) "
                        "VALUES(%s,%s,%s,'failed',%s,%s,%s)",
                        (uid, username, url, kind, quality, error)
                    )
                    conn.execute(
                        "INSERT INTO error_logs(user_id,error) VALUES(%s,%s)",
                        (uid, error)
                    )
            except Exception:
                log.exception("Failed to save download error to database")

            await context.bot.send_message(
                chat_id,
                f"❌ Download မအောင်မြင်ပါ။\\n{error[:350]}"
            )
            try:
                await context.bot.send_message(
                    ADMIN_ID,
                    f"⚠️ Download error\\nUser: {uid}\\n{error[:800]}"
                )
            except Exception:
                pass


user_locks = {}


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    record_user(user)
    text = (update.message.text or "").strip()

    match = re.search(
        r"https?://(?:www\.)?(?:vm\.tiktok\.com|vt\.tiktok\.com|tiktok\.com)/\S+",
        text
    )

    if not match:
        await update.message.reply_text(
            "TikTok link ကို ပို့ပေးပါ။",
            reply_markup=await main_keyboard()
        )
        return

    url = match.group(0).rstrip(".,)")

    banned, reason = user_status(user.id)
    if banned:
        await update.message.reply_text("အသုံးပြုခွင့် ပိတ်ထားပါတယ်။ " + reason)
        return

    # Resolve short TikTok links to identify photo posts.
    photo_link = "/photo/" in url

    try:
        response = await asyncio.to_thread(
            c_requests.get,
            url,
            allow_redirects=True,
            impersonate="chrome120",
            timeout=30
        )
        resolved_url = str(response.url)

        if "/photo/" in resolved_url:
            url = resolved_url
            photo_link = True
    except Exception as exc:
        log.warning("TikTok URL resolve failed: %s", str(exc)[:200])

    if photo_link:
        await update.message.reply_text("📸 Photo slideshow Download စတင်ပါမယ်။")
        await process_download(
            update.effective_chat.id,
            user.id,
            user.username,
            url,
            "original",
            "video",
            context
        )
        return

    try:
        data, _media_session = await asyncio.to_thread(get_tiktok_data, url)

        if get_photo_urls(data):
            await update.message.reply_text("📸 Photo slideshow Download စတင်ပါမယ်။")
            await process_download(
                update.effective_chat.id,
                user.id,
                user.username,
                url,
                "original",
                "video",
                context
            )
            return

    except Exception:
        pass

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("720p", callback_data=f"quality:720p:{url}")],
        [InlineKeyboardButton("1080p", callback_data=f"quality:1080p:{url}")],
        [InlineKeyboardButton("Original", callback_data=f"quality:original:{url}")]
    ])

    await update.message.reply_text("Video Quality ရွေးပါ။", reply_markup=keyboard)


# ---------------------------------------------------------------------
# Admin commands
# ---------------------------------------------------------------------

async def admin_only(update, context):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("ဒီ command ကို Admin ပဲ သုံးနိုင်ပါတယ်။")
        return False
    return True

async def maintenance_cmd(update, context):
    if not await admin_only(update, context): return
    if not context.args or context.args[0].lower() not in ("on", "off"):
        await update.message.reply_text("အသုံးပြုရန်: /maintenance on သို့ /maintenance off")
        return
    set_setting("maintenance", context.args[0].lower())
    log_admin("maintenance", context.args[0].lower())
    await update.message.reply_text("Maintenance " + context.args[0].upper())

async def setlimit_cmd(update, context):
    if not await admin_only(update, context): return
    if len(context.args) != 2 or context.args[0] not in ("minute", "hour", "day"):
        await update.message.reply_text("အသုံးပြုရန်: /setlimit minute 3")
        return
    try:
        value = int(context.args[1])
        if not 1 <= value <= 10000: raise ValueError()
    except ValueError:
        await update.message.reply_text("ကန့်သတ်ချက်ကို 1 မှ 10000 ကြား ထည့်ပါ။")
        return
    set_setting("rate_" + context.args[0], value)
    log_admin("setlimit", f"{context.args[0]}={value}")
    await update.message.reply_text("Rate limit ပြင်ပြီးပါပြီ။")

async def broadcast_cmd(update, context):
    if not await admin_only(update, context): return
    message = " ".join(context.args).strip()
    if not message:
        await update.message.reply_text("အသုံးပြုရန်: /broadcast သင့်စာသား")
        return
    with db() as conn:
        ids = [r["user_id"] for r in conn.execute("SELECT user_id FROM users WHERE is_banned=FALSE").fetchall()]
    sent = failed = 0
    for uid in ids:
        try:
            await context.bot.send_message(uid, message)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.04)
    log_admin("broadcast", f"sent={sent},failed={failed}")
    await update.message.reply_text(f"Broadcast ပြီးပါပြီ။ Success: {sent}, Failed: {failed}")

async def ban_cmd(update, context):
    if not await admin_only(update, context): return
    if not context.args:
        await update.message.reply_text("အသုံးပြုရန်: /ban USER_ID အကြောင်းပြချက်")
        return
    try: uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("User ID မမှန်ပါ။"); return
    reason = " ".join(context.args[1:]) or "Admin restriction"
    with db() as conn:
        conn.execute("UPDATE users SET is_banned=TRUE,ban_until=NULL,ban_reason=%s WHERE user_id=%s",
                     (reason, uid))
    log_admin("ban", f"{uid}: {reason}")
    await update.message.reply_text(f"{uid} ကို ban လုပ်ပြီးပါပြီ။")

async def tempban_cmd(update, context):
    if not await admin_only(update, context): return
    if len(context.args) < 2:
        await update.message.reply_text("အသုံးပြုရန်: /tempban USER_ID မိနစ် အကြောင်းပြချက်"); return
    try:
        uid, minutes = int(context.args[0]), int(context.args[1])
        if minutes < 1 or minutes > 525600: raise ValueError()
    except ValueError:
        await update.message.reply_text("User ID / မိနစ် မမှန်ပါ။"); return
    reason = " ".join(context.args[2:]) or "Temporary restriction"
    with db() as conn:
        conn.execute("""
            UPDATE users SET is_banned=TRUE,ban_until=NOW()+(%s * INTERVAL '1 minute'),
            ban_reason=%s WHERE user_id=%s
        """, (minutes, reason, uid))
    log_admin("tempban", f"{uid} for {minutes}m")
    await update.message.reply_text(f"{uid} ကို {minutes} မိနစ် ban လုပ်ပြီးပါပြီ။")

async def unban_cmd(update, context):
    if not await admin_only(update, context): return
    if not context.args:
        await update.message.reply_text("အသုံးပြုရန်: /unban USER_ID"); return
    try: uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("User ID မမှန်ပါ။"); return
    with db() as conn:
        conn.execute("UPDATE users SET is_banned=FALSE,ban_until=NULL,ban_reason=NULL WHERE user_id=%s",
                     (uid,))
    log_admin("unban", str(uid))
    await update.message.reply_text(f"{uid} ကို unban လုပ်ပြီးပါပြီ။")

async def addbutton_cmd(update, context):
    if not await admin_only(update, context): return
    if len(context.args) < 2 or not context.args[-1].startswith("https://"):
        await update.message.reply_text("အသုံးပြုရန်: /addbutton Button Name https://URL"); return
    label = " ".join(context.args[:-1])
    url = context.args[-1]
    with db() as conn:
        conn.execute("INSERT INTO bot_buttons(label,url) VALUES(%s,%s)", (label, url))
    log_admin("addbutton", label)
    await update.message.reply_text("Button ထည့်ပြီးပါပြီ။ /start ပြန်နှိပ်ကြည့်ပါ။")

async def delbutton_cmd(update, context):
    if not await admin_only(update, context): return
    if not context.args: await update.message.reply_text("အသုံးပြုရန်: /delbutton ID"); return
    try: bid = int(context.args[0])
    except ValueError: await update.message.reply_text("ID မမှန်ပါ။"); return
    with db() as conn: conn.execute("DELETE FROM bot_buttons WHERE id=%s", (bid,))
    log_admin("delbutton", str(bid))
    await update.message.reply_text("Button ဖျက်ပြီးပါပြီ။")

async def addad_cmd(update, context):
    if not await admin_only(update, context): return
    raw = " ".join(context.args)
    if " " not in raw:
        await update.message.reply_text("အသုံးပြုရန်: /addad welcome စာသား သို့ /addad after_download စာသား"); return
    placement, message = raw.split(" ", 1)
    placement = placement.strip().lower()
    if placement not in ("welcome", "after_download") or not message.strip():
        await update.message.reply_text("Placement ကို welcome သို့ after_download သတ်မှတ်ပါ။"); return
    with db() as conn:
        conn.execute("INSERT INTO bot_ads(placement,text) VALUES(%s,%s)", (placement, message.strip()))
    log_admin("addad", placement)
    await update.message.reply_text("Ad ထည့်ပြီးပါပြီ။")

async def togglead_cmd(update, context):
    if not await admin_only(update, context): return
    if not context.args: await update.message.reply_text("အသုံးပြုရန်: /togglead ID"); return
    try: aid = int(context.args[0])
    except ValueError: await update.message.reply_text("ID မမှန်ပါ။"); return
    with db() as conn:
        conn.execute("UPDATE bot_ads SET enabled=NOT enabled WHERE id=%s", (aid,))
    log_admin("togglead", str(aid))
    await update.message.reply_text("Ad အခြေအနေ ပြောင်းပြီးပါပြီ။")

async def delad_cmd(update, context):
    if not await admin_only(update, context): return
    if not context.args: await update.message.reply_text("အသုံးပြုရန်: /delad ID"); return
    try: aid = int(context.args[0])
    except ValueError: await update.message.reply_text("ID မမှန်ပါ။"); return
    with db() as conn: conn.execute("DELETE FROM bot_ads WHERE id=%s", (aid,))
    log_admin("delad", str(aid))
    await update.message.reply_text("Ad ဖျက်ပြီးပါပြီ။")

async def create_backup(chat_id, context):
    tables = ["users", "downloads", "bot_settings", "bot_buttons", "bot_ads",
              "admin_logs", "error_logs", "request_events", "backup_log"]
    backup = {"created_at": datetime.now(timezone.utc).isoformat(), "tables": {}}
    try:
        with db() as conn:
            for table in tables:
                rows = conn.execute(f"SELECT * FROM {table}").fetchall()
                backup["tables"][table] = [
                    {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in row.items()}
                    for row in rows
                ]
        raw = json.dumps(backup, ensure_ascii=False, indent=2, default=str).encode("utf-8")
        file = BytesIO(raw)
        file.name = f"p_tiktok_backup_{datetime.now():%Y%m%d_%H%M%S}.json"
        await context.bot.send_document(chat_id, file, caption="Database backup (JSON)")
        with db() as conn:
            conn.execute("INSERT INTO backup_log(status,details) VALUES('success','Manual backup sent')")
        log_admin("backup", "manual success")
    except Exception as exc:
        with db() as conn:
            conn.execute("INSERT INTO backup_log(status,details) VALUES('failed',%s)", (str(exc)[:1000],))
        await context.bot.send_message(chat_id, f"Backup မအောင်မြင်ပါ: {str(exc)[:300]}")

async def backup_cmd(update, context):
    if not await admin_only(update, context): return
    await create_backup(update.effective_chat.id, context)

async def admin_command(update, context):
    if not await admin_only(update, context): return
    await update.message.reply_text("Admin Panel", reply_markup=admin_keyboard())

async def error_handler(update, context):
    # Telegram raises this harmless error when an edit repeats identical text/markup.
    if isinstance(context.error, BadRequest) and "message is not modified" in str(context.error).lower():
        log.info("Ignored harmless Telegram edit: message is not modified")
        return

    log.exception("Unhandled bot error", exc_info=context.error)
    try:
        with db() as conn:
            conn.execute("INSERT INTO error_logs(error) VALUES(%s)", (str(context.error)[:1000],))
        await context.bot.send_message(ADMIN_ID, f"⚠️ Bot error: {str(context.error)[:800]}")
    except Exception:
        pass

# ---------------------------------------------------------------------
# Health endpoint and startup
# ---------------------------------------------------------------------

@web.get("/")
def health():
    try:
        with db() as conn:
            conn.execute("SELECT 1")
        return jsonify({"status": "ok", "service": "P TikTok V2", "database": "ok"})
    except Exception:
        return jsonify({"status": "degraded", "database": "error"}), 503

def run_web():
    port = int(os.getenv("PORT", "10000"))
    web.run(host="0.0.0.0", port=port, use_reloader=False)

async def post_init(app: Application):
    init_db()
    log.info("V2 bot started. Admin ID=%s", ADMIN_ID)

def main():
    import threading
    threading.Thread(target=run_web, daemon=True).start()
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("maintenance", maintenance_cmd))
    app.add_handler(CommandHandler("setlimit", setlimit_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))
    app.add_handler(CommandHandler("ban", ban_cmd))
    app.add_handler(CommandHandler("tempban", tempban_cmd))
    app.add_handler(CommandHandler("unban", unban_cmd))
    app.add_handler(CommandHandler("addbutton", addbutton_cmd))
    app.add_handler(CommandHandler("delbutton", delbutton_cmd))
    app.add_handler(CommandHandler("addad", addad_cmd))
    app.add_handler(CommandHandler("togglead", togglead_cmd))
    app.add_handler(CommandHandler("delad", delad_cmd))
    app.add_handler(CommandHandler("backup", backup_cmd))
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(error_handler)
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
