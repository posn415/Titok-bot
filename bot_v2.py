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
# MediaX - official release preparation
# Required environment variables:
#   BOT_TOKEN, DATABASE_URL
# Optional:
#   ADMIN_ID (default 8556272994), RENDER_URL
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
STARTED_AT = time.monotonic()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("mediax")
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
                created_at TIMESTAMPTZ DEFAULT NOW(),
                media_file_id TEXT,
                media_group_id TEXT
            )
        """)
        conn.execute("ALTER TABLE bot_ads ADD COLUMN IF NOT EXISTS media_file_id TEXT")
        conn.execute("ALTER TABLE bot_ads ADD COLUMN IF NOT EXISTS media_group_id TEXT")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS force_join_channels (
                id BIGSERIAL PRIMARY KEY,
                channel TEXT NOT NULL,
                invite_url TEXT NOT NULL,
                enabled BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_admins (
                user_id BIGINT PRIMARY KEY,
                permissions TEXT NOT NULL DEFAULT 'dashboard,users,downloads,logs',
                added_by BIGINT NOT NULL,
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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_feedback (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                username TEXT,
                message TEXT NOT NULL,
                status TEXT DEFAULT 'new',
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
            ('welcome','မင်္ဂလာပါ။ MediaX မှ ကြိုဆိုပါတယ်။ TikTok link ပို့ပြီး Download လုပ်နိုင်ပါတယ်။'),
            ('force_join_enabled','off'),
            ('rate_minute','3'), ('rate_hour','20'), ('rate_day','100')
            ON CONFLICT(key) DO NOTHING
        """)
        conn.execute(
            "UPDATE bot_settings SET value=%s WHERE key='welcome' "
            "AND value=%s",
            (
                "မင်္ဂလာပါ။ MediaX မှ ကြိုဆိုပါတယ်။ TikTok link ပို့ပြီး Download လုပ်နိုင်ပါတယ်။",
                "မင်္ဂလာပါ။deflink ပို့ပြီး Download လုပ်နိုင်ပါတယ်။"
            )
        )

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

def log_admin(action, details="", admin_id=None):
    actor_id = ADMIN_ID if admin_id is None else int(admin_id)
    with db() as conn:
        conn.execute(
            "INSERT INTO admin_logs(admin_id,action,details) VALUES(%s,%s,%s)",
            (actor_id, action, details)
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


def get_force_join_channels():
    with db() as conn:
        return conn.execute(
            "SELECT id,channel,invite_url FROM force_join_channels "
            "WHERE enabled=TRUE ORDER BY id ASC"
        ).fetchall()


def force_join_keyboard(channels):
    rows = [
        [InlineKeyboardButton(f"📢 Join {str(row['channel'])[:35]}", url=row["invite_url"])]
        for row in channels
    ]
    rows.append([InlineKeyboardButton("✅ I've Joined — Verify", callback_data="fj_check")])
    return InlineKeyboardMarkup(rows)


async def check_force_join(uid, context):
    if is_admin(uid):
        return True, []
    if setting("force_join_enabled", "off").lower() != "on":
        return True, []
    channels = get_force_join_channels()
    if not channels:
        return True, []
    missing = []
    for channel in channels:
        try:
            member = await context.bot.get_chat_member(
                chat_id=channel["channel"], user_id=uid
            )
            status = getattr(member, "status", "")
            is_member = status in ("member", "administrator", "creator")
            if status == "restricted":
                is_member = bool(getattr(member, "is_member", False))
            if not is_member:
                missing.append(channel)
        except Exception as exc:
            log.warning(
                "Force-join check failed for %s: %s",
                channel["channel"], str(exc)[:180]
            )
            missing.append(channel)
    return not missing, missing


async def send_force_join_prompt(chat_id, channels, context):
    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            "🔒 MediaX ကို အသုံးပြုရန် အောက်ပါ Channel များကို Join လုပ်ပေးပါ။\n"
            "Join ပြီးပါက I've Joined — Verify ကိုနှိပ်ပါ။"
        ),
        reply_markup=force_join_keyboard(channels)
    )

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
    if is_admin(uid):
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


async def to_thread_with_retry(func, *args, attempts=3, **kwargs):
    """Retry transient worker/network errors with a short exponential backoff."""
    last_error = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            return await asyncio.to_thread(func, *args, **kwargs)
        except Exception as exc:
            last_error = exc
            if attempt >= attempts:
                break
            await asyncio.sleep(min(2 ** (attempt - 1), 4))
    raise last_error


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

ADMIN_PERMISSION_LABELS = {
    "dashboard": "Dashboard",
    "users": "User Management",
    "downloads": "Download Logs",
    "settings": "Settings / Maintenance",
    "broadcast": "Broadcast",
    "buttons": "User Buttons",
    "ads": "Ads Manager",
    "force_join": "Force Join",
    "logs": "Admin / Error Logs",
    "backup": "Backup",
}

def is_owner(uid):
    return int(uid) == ADMIN_ID

def get_admin_permissions(uid):
    if is_owner(uid):
        return set(ADMIN_PERMISSION_LABELS) | {"admins"}
    try:
        with db() as conn:
            row = conn.execute(
                "SELECT permissions FROM bot_admins WHERE user_id=%s",
                (int(uid),)
            ).fetchone()
        if not row:
            return set()
        return {item.strip() for item in (row["permissions"] or "").split(",") if item.strip()}
    except Exception:
        log.exception("Could not read admin permissions for %s", uid)
        return set()

def has_permission(uid, permission):
    if is_owner(uid):
        return True
    if permission == "admins":
        return False
    permissions = get_admin_permissions(uid)
    return "all" in permissions or permission in permissions

def is_admin(uid):
    return is_owner(uid) or bool(get_admin_permissions(uid))

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

def admin_keyboard(uid=None):
    rows = []
    if uid is None or has_permission(uid, "dashboard"):
        rows.append([InlineKeyboardButton("📊 Overview", callback_data="adm_stats"),
                     InlineKeyboardButton("🖥️ System Monitor", callback_data="adm_system")])
    if uid is None or has_permission(uid, "users"):
        rows.append([InlineKeyboardButton("👥 User Manager", callback_data="adm_users"),
                     InlineKeyboardButton("🚫 Ban / Unban", callback_data="adm_ban_help")])
    if uid is None or has_permission(uid, "downloads"):
        rows.append([InlineKeyboardButton("📥 Recent Downloads", callback_data="adm_downloads")])
    if uid is None or has_permission(uid, "settings"):
        rows.append([InlineKeyboardButton("⚙️ Settings", callback_data="adm_settings")])
    if uid is None or has_permission(uid, "broadcast"):
        rows.append([InlineKeyboardButton("📢 Broadcast", callback_data="adm_broadcast_help")])
    if uid is None or has_permission(uid, "buttons"):
        rows.append([InlineKeyboardButton("🔘 Button Manager", callback_data="adm_buttons")])
    if uid is None or has_permission(uid, "ads"):
        rows.append([InlineKeyboardButton("🖼️ Ads Manager", callback_data="adm_ads")])
    if uid is None or has_permission(uid, "logs"):
        rows.append([InlineKeyboardButton("🧾 Error Logs", callback_data="adm_errors"),
                     InlineKeyboardButton("💬 User Feedback", callback_data="adm_feedback")])
    if uid is None or has_permission(uid, "backup"):
        rows.append([InlineKeyboardButton("💾 Backup", callback_data="adm_backup")])
    if uid is None or has_permission(uid, "force_join"):
        rows.append([InlineKeyboardButton("📢 Force Join Channels", callback_data="adm_forcejoin")])
    if uid is not None and is_owner(uid):
        rows.append([InlineKeyboardButton("🛡️ Admin Management", callback_data="adm_admins")])
    if not rows:
        rows.append([InlineKeyboardButton("Permission မရှိပါ", callback_data="adm_no_permission")])
    return InlineKeyboardMarkup(rows)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    record_user(user)
    banned, reason = user_status(user.id)
    if banned:
        await update.message.reply_text("အသုံးပြုခွင့် ပိတ်ထားပါတယ်။ " + reason)
        return
    joined, missing = await check_force_join(user.id, context)
    if not joined:
        await send_force_join_prompt(update.effective_chat.id, missing, context)
        return
    welcome = setting(
        "welcome",
        "မင်္ဂလာပါ။ MediaX မှ ကြိုဆိုပါတယ်။ TikTok link ပို့ပြီး Download လုပ်နိုင်ပါတယ်။"
    )
    await update.message.reply_text(welcome, reply_markup=await main_keyboard())
    await send_ads(update.effective_chat.id, "welcome", context)
    if is_admin(user.id):
        await update.message.reply_text("MediaX Admin Dashboard", reply_markup=admin_keyboard(user.id))

async def send_ads(chat_id, placement, context):
    with db() as conn:
        rows = conn.execute(
            "SELECT id,text,media_file_id,media_group_id FROM bot_ads "
            "WHERE enabled=TRUE AND placement=%s ORDER BY id",
            (placement,)
        ).fetchall()

    grouped = {}
    for row in rows:
        key = row["media_group_id"] or f"single-{row['id']}"
        grouped.setdefault(key, []).append(row)

    for items in grouped.values():
        photo_rows = [item for item in items if item["media_file_id"]]
        if photo_rows:
            for offset in range(0, len(photo_rows), 10):
                batch = photo_rows[offset:offset + 10]
                if len(batch) > 1:
                    media = [
                        InputMediaPhoto(
                            media=item["media_file_id"],
                            caption=(item["text"] or "") if index == 0 else None
                        )
                        for index, item in enumerate(batch)
                    ]
                    await context.bot.send_media_group(chat_id=chat_id, media=media)
                elif batch:
                    await context.bot.send_photo(
                        chat_id=chat_id,
                        photo=batch[0]["media_file_id"],
                        caption=batch[0]["text"] or None
                    )
        else:
            for item in items:
                if item["text"]:
                    await context.bot.send_message(chat_id=chat_id, text=item["text"])

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
    data = q.data
    if data != "fj_check" and not (data.startswith("admin:") or data.startswith("adm_")):
        await q.answer()
    if data == "fj_check":
        joined, missing = await check_force_join(q.from_user.id, context)
        if not joined:
            await q.answer("Channel အားလုံး Join လုပ်ပြီးမှ Verify လုပ်ပါ။", show_alert=True)
            try:
                await q.edit_message_reply_markup(
                    reply_markup=force_join_keyboard(missing)
                )
            except BadRequest:
                pass
            return
        await q.answer("Membership verified!")
        await q.message.reply_text(
            "✅ အတည်ပြုပြီးပါပြီ။ MediaX ကို အသုံးပြုနိုင်ပါပြီ။",
            reply_markup=await main_keyboard()
        )
        return
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
            await q.answer("Admin only.", show_alert=True)
            return
        await admin_callback(q, context)

async def admin_callback(q, context):
    action = q.data
    action_permissions = {
        "adm_stats": "dashboard",
        "adm_system": "dashboard",
        "adm_feedback": "logs",
        "adm_users": "users",
        "adm_downloads": "downloads",
        "adm_settings": "settings",
        "adm_broadcast_help": "broadcast",
        "adm_ban_help": "users",
        "adm_buttons": "buttons",
        "adm_ads": "ads",
        "adm_forcejoin": "force_join",
        "adm_errors": "logs",
        "adm_backup": "backup",
        "adm_admins": "admins",
    }
    required_permission = action_permissions.get(action)
    if required_permission and not has_permission(q.from_user.id, required_permission):
        await q.answer("ဒီလုပ်ဆောင်ချက်အတွက် Permission မရှိပါ။", show_alert=True)
        return
    await q.answer()
    if action == "adm_stats":
        with db() as conn:
            users = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
            today = conn.execute(
                "SELECT COUNT(*) AS n FROM downloads WHERE downloaded_at::date=CURRENT_DATE AND status='success'"
            ).fetchone()["n"]
            week = conn.execute(
                "SELECT COUNT(*) AS n FROM downloads WHERE downloaded_at >= NOW()-INTERVAL '7 days' AND status='success'"
            ).fetchone()["n"]
            month = conn.execute(
                "SELECT COUNT(*) AS n FROM downloads WHERE downloaded_at >= NOW()-INTERVAL '30 days' AND status='success'"
            ).fetchone()["n"]
            total = conn.execute(
                "SELECT COUNT(*) AS n FROM downloads WHERE status='success'"
            ).fetchone()["n"]
        await q.edit_message_text(
            f"📊 MediaX Dashboard\\nUsers: {users}\\nDownloads total: {total}\\nToday: {today}\\nLast 7 days: {week}\\nLast 30 days: {month}",
            reply_markup=admin_keyboard(q.from_user.id)
        )
    elif action == "adm_system":
        try:
            with db() as conn:
                conn.execute("SELECT 1")
            database_state = "OK"
        except Exception as exc:
            database_state = "ERROR: " + str(exc)[:120]
        uptime_seconds = int(time.monotonic() - STARTED_AT)
        hours, remainder = divmod(uptime_seconds, 3600)
        minutes = remainder // 60
        try:
            with open("/proc/self/status", "r", encoding="utf-8") as status_file:
                rss_line = next((line for line in status_file if line.startswith("VmRSS:")), "")
            memory_state = rss_line.replace("VmRSS:", "").strip() or "Unavailable"
        except Exception:
            memory_state = "Unavailable"
        await q.edit_message_text(
            "🖥️ MediaX System Monitor\\n"
            f"Database: {database_state}\\n"
            f"Uptime: {hours}h {minutes}m\\n"
            f"Memory: {memory_state}\\n"
            f"Downloads active: {active_downloads}/{MAX_CONCURRENT_DOWNLOADS}\\n"
            f"Downloads queued: {queued_downloads}",
            reply_markup=admin_keyboard(q.from_user.id)
        )
    elif action == "adm_feedback":
        with db() as conn:
            rows = conn.execute(
                "SELECT id,user_id,username,message,status,created_at FROM user_feedback "
                "ORDER BY id DESC LIMIT 10"
            ).fetchall()
        if rows:
            body = "💬 နောက်ဆုံး Feedback ၁၀ ခု\\n\\n" + "\\n\\n".join(
                f"#{row['id']} | User {row['user_id']} | {row['status']}\\n"
                f"{row['message'][:350]}"
                for row in rows
            )
        else:
            body = "Feedback မရှိသေးပါ။"
        await q.edit_message_text(body, reply_markup=admin_keyboard(q.from_user.id))
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
        await q.edit_message_text(text, reply_markup=admin_keyboard(q.from_user.id))
    elif action == "adm_downloads":
        with db() as conn:
            rows = conn.execute(
                "SELECT user_id,media_type,status,downloaded_at FROM downloads ORDER BY id DESC LIMIT 10"
            ).fetchall()
        text = "📥 Recent Downloads\n" + "\n".join(
            f"{r['user_id']} | {r['media_type']} | {r['status']} | {r['downloaded_at']:%m-%d %H:%M}"
            for r in rows
        )
        await q.edit_message_text(text, reply_markup=admin_keyboard(q.from_user.id))
    elif action == "adm_settings":
        await q.edit_message_text(
            f"Maintenance: {setting('maintenance','off')}\n"
            f"Limits: {setting('rate_minute','3')}/min, {setting('rate_hour','20')}/hour, "
            f"{setting('rate_day','100')}/day\n\n"
            "Commands:\n/maintenance on|off\n/setlimit minute 3\n/setlimit hour 20\n/setlimit day 100",
            reply_markup=admin_keyboard(q.from_user.id)
        )
    elif action == "adm_broadcast_help":
        await q.edit_message_text("Broadcast ပို့ရန် /broadcast စာသား သို့ /broadcastphoto ကို အသုံးပြုပါ။",
                                  reply_markup=admin_keyboard(q.from_user.id))
    elif action == "adm_ban_help":
        await q.edit_message_text(
            "အသုံးပြုနည်း:\n/ban USER_ID အကြောင်းပြချက်\n"
            "/tempban USER_ID မိနစ် အကြောင်းပြချက်\n/unban USER_ID",
            reply_markup=admin_keyboard(q.from_user.id)
        )
    elif action == "adm_buttons":
        with db() as conn:
            rows = conn.execute("SELECT id,label,url,enabled FROM bot_buttons ORDER BY id DESC").fetchall()
        text = "🔘 Buttons\n" + "\n".join(
            f"{r['id']}. {r['label']} — {'ON' if r['enabled'] else 'OFF'} — {r['url']}"
            for r in rows
        )
        text += "\n\nထည့်ရန်: /addbutton နာမည် https://URL\nဖျက်ရန်: /delbutton ID"
        await q.edit_message_text(text or "Buttons မရှိသေးပါ။", reply_markup=admin_keyboard(q.from_user.id))
    elif action == "adm_ads":
        with db() as conn:
            rows = conn.execute(
                "SELECT id,placement,text,enabled,media_file_id,media_group_id "
                "FROM bot_ads ORDER BY id DESC"
            ).fetchall()
        text = "📣 Ads\n" + "\n".join(
            f"{r['id']} [{r['placement']}] {'PHOTO' if r['media_file_id'] else 'TEXT'} "
            f"{'ON' if r['enabled'] else 'OFF'}: {(r['text'] or '')[:65]}"
            for r in rows
        )
        text += "\n\nစာသား Ad: /addad welcome စာသား\n"
        text += "Photo Album: /addad welcome → ပုံများပို့ → /savead caption\n"
        text += "ဖွင့်/ပိတ်: /togglead ID\nဖျက်ရန်: /delad ID"
        await q.edit_message_text(text or "Ads မရှိသေးပါ။", reply_markup=admin_keyboard(q.from_user.id))
    elif action == "adm_forcejoin":
        with db() as conn:
            rows = conn.execute(
                "SELECT id,channel,invite_url,enabled FROM force_join_channels ORDER BY id DESC"
            ).fetchall()
        state = setting("force_join_enabled", "off").upper()
        details = "\n".join(
            f"{row['id']}. {row['channel']} — {'ON' if row['enabled'] else 'OFF'}"
            for row in rows
        ) or "Channel မထည့်ရသေးပါ။"
        text = (
            f"📢 Force Join: {state}\n\n{details}\n\n"
            "Channel ထည့်ရန်: /addchannel @channel https://t.me/channel\n"
            "ဖွင့်/ပိတ်ရန်: /togglechannel ID\n"
            "ဖျက်ရန်: /delchannel ID\n"
            "စနစ်ဖွင့်/ပိတ်ရန်: /forcejoin on သို့ /forcejoin off\n\n"
            "မှတ်ချက်: Membership စစ်ဆေးရန် Bot ကို Channel ထဲတွင် Admin ပေးထားပါ။"
        )
        await q.edit_message_text(text, reply_markup=admin_keyboard(q.from_user.id))
    elif action == "adm_admins":
        if not is_owner(q.from_user.id):
            await q.answer("Main Admin သာ အသုံးပြုနိုင်ပါတယ်။", show_alert=True)
            return
        with db() as conn:
            rows = conn.execute(
                "SELECT user_id,permissions,added_by,created_at FROM bot_admins ORDER BY created_at DESC"
            ).fetchall()
        lines = [
            f"{row['user_id']} — {row['permissions']} (added by {row['added_by']})"
            for row in rows
        ]
        body = (
            "🛡️ Admin Management\n\n"
            + ("\n".join(lines) if lines else "Admin အသစ် မရှိသေးပါ။")
            + "\n\nထည့်ရန်: /addadmin USER_ID permission1,permission2"
            + "\nPermission ပြင်ရန်: /setadminperm USER_ID permission1,permission2"
            + "\nဖျက်ရန်: /deladmin USER_ID"
            + "\nစာရင်း: /admins"
            + "\nID နဲ့ရှာရန်: /findadmin USER_ID"
            + "\n\nPermissions: "
            + ", ".join(ADMIN_PERMISSION_LABELS.keys())
            + " (all = Admin Management မပါသော Permission အားလုံး)"
        )
        await q.edit_message_text(body, reply_markup=admin_keyboard(q.from_user.id))
    elif action == "adm_errors":
        with db() as conn:
            rows = conn.execute("SELECT error,created_at FROM error_logs ORDER BY id DESC LIMIT 8").fetchall()
        text = "🧾 Error Logs\n" + "\n".join(
            f"{r['created_at']:%m-%d %H:%M}: {r['error'][:180]}" for r in rows
        )
        await q.edit_message_text(text or "Error မရှိပါ။", reply_markup=admin_keyboard(q.from_user.id))
    elif action == "adm_backup":
        await create_backup(q.message.chat_id, context, q.from_user.id)
    else:
        await q.edit_message_text("မသိသော command", reply_markup=admin_keyboard(q.from_user.id))

async def _process_download_now(chat_id, uid, username, url, quality, kind, context):
    joined, missing = await check_force_join(uid, context)
    if not joined:
        await send_force_join_prompt(chat_id, missing, context)
        return

    if setting("maintenance", "off").lower() == "on" and not is_admin(uid):
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
                        await to_thread_with_retry(fetch_bytes, media_session, photo_url)
                    )
            elif "/photo/" in url:
                if kind == "mp3":
                    raise ValueError("Photo slideshow မှ MP3 မထုတ်နိုင်ပါ။")
                photo_bytes = await to_thread_with_retry(download_tiktok_photos, url)

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
                    video_bytes = await to_thread_with_retry(fetch_bytes, media_session, video_url)
                    audio = await asyncio.to_thread(extract_mp3, video_bytes)
                    audio_file = BytesIO(audio)
                    audio_file.name = "tiktok_audio.mp3"
                    await context.bot.send_audio(chat_id=chat_id, audio=audio_file)
                    media_type = "mp3"
                else:
                    try:
                        video_bytes = await to_thread_with_retry(fetch_bytes, media_session, video_url)
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


# In-process download queue. Two jobs can run concurrently; waiting jobs are
# held in memory and will be lost if the service restarts.
MAX_CONCURRENT_DOWNLOADS = max(1, min(4, int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "2"))))
download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
queue_state_lock = asyncio.Lock()
queued_downloads = 0
active_downloads = 0


async def process_download(chat_id, uid, username, url, quality, kind, context):
    global queued_downloads, active_downloads
    async with queue_state_lock:
        position = active_downloads + queued_downloads + 1
        queued_downloads += 1

    progress = None
    acquired = False
    waiting = True
    try:
        if position > MAX_CONCURRENT_DOWNLOADS:
            progress = await context.bot.send_message(
                chat_id,
                f"⏳ Download Queue ထဲ ထည့်ပြီးပါပြီ။\nသင့်ရှေ့မှာ {position - MAX_CONCURRENT_DOWNLOADS} ခုခန့် ရှိပါတယ်။"
            )
        else:
            progress = await context.bot.send_message(
                chat_id, "⏳ Download စတင်ရန် ပြင်ဆင်နေပါတယ်။"
            )

        async with download_semaphore:
            acquired = True
            waiting = False
            async with queue_state_lock:
                queued_downloads = max(0, queued_downloads - 1)
                active_downloads += 1
                active_number = active_downloads

            try:
                await progress.edit_text(
                    f"⏳ Download လုပ်နေပါတယ်။\nလက်ရှိလုပ်ဆောင်နေသော Job: {active_number}/{MAX_CONCURRENT_DOWNLOADS}"
                )
            except Exception:
                pass

            await _process_download_now(
                chat_id, uid, username, url, quality, kind, context
            )
            try:
                await progress.edit_text(
                    "✅ Request ပြီးဆုံးပါပြီ။ Download ရလဒ်ကို အပေါ်မှာကြည့်ပါ။"
                )
            except Exception:
                pass
    finally:
        async with queue_state_lock:
            if acquired:
                active_downloads = max(0, active_downloads - 1)
            elif waiting:
                queued_downloads = max(0, queued_downloads - 1)


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

    joined, missing = await check_force_join(user.id, context)
    if not joined:
        await send_force_join_prompt(update.effective_chat.id, missing, context)
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

async def admin_only(update, context, permission="dashboard"):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text("ဒီ command ကို Admin ပဲ သုံးနိုင်ပါတယ်။")
        return False
    if not has_permission(uid, permission):
        await update.message.reply_text("ဒီလုပ်ဆောင်ချက်အတွက် Permission မရှိပါ။")
        return False
    return True

async def owner_only(update, context):
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("Main Admin သာ Admin Management လုပ်နိုင်ပါတယ်။")
        return False
    return True

def parse_admin_permissions(raw):
    requested = {item.strip().lower() for item in raw.split(",") if item.strip()}
    if "all" in requested:
        return {"all"}
    invalid = requested - set(ADMIN_PERMISSION_LABELS)
    if invalid:
        raise ValueError("မသိသော Permission: " + ", ".join(sorted(invalid)))
    if not requested:
        raise ValueError("Permission အနည်းဆုံး တစ်ခု ရွေးပါ။")
    requested.add("dashboard")
    return requested

async def addadmin_cmd(update, context):
    if not await owner_only(update, context):
        return
    if len(context.args) != 2:
        await update.message.reply_text(
            "အသုံးပြုရန်: /addadmin USER_ID dashboard,users,downloads\n"
            "Permission များ: " + ", ".join(ADMIN_PERMISSION_LABELS)
            + "\nall = Admin Management မပါသော Permission အားလုံး"
        )
        return
    try:
        uid = int(context.args[0])
        if uid <= 0 or uid == ADMIN_ID:
            raise ValueError()
        permissions = parse_admin_permissions(context.args[1])
    except ValueError as exc:
        await update.message.reply_text(str(exc) or "User ID မမှန်ပါ။")
        return
    permission_text = ",".join(sorted(permissions))
    with db() as conn:
        conn.execute(
            "INSERT INTO bot_admins(user_id,permissions,added_by) VALUES(%s,%s,%s) "
            "ON CONFLICT(user_id) DO UPDATE SET permissions=EXCLUDED.permissions,added_by=EXCLUDED.added_by",
            (uid, permission_text, update.effective_user.id)
        )
    log_admin("add_or_update_admin", f"{uid}: {permission_text}", update.effective_user.id)
    await update.message.reply_text(f"Admin {uid} ကို ထည့်/ပြင်ပြီးပါပြီ။ Permission: {permission_text}")

async def setadminperm_cmd(update, context):
    if not await owner_only(update, context):
        return
    if len(context.args) != 2:
        await update.message.reply_text(
            "အသုံးပြုရန်: /setadminperm USER_ID permission1,permission2"
        )
        return
    try:
        uid = int(context.args[0])
        if uid <= 0 or uid == ADMIN_ID:
            raise ValueError()
        permissions = parse_admin_permissions(context.args[1])
    except ValueError as exc:
        await update.message.reply_text(str(exc) or "User ID မမှန်ပါ။")
        return
    permission_text = ",".join(sorted(permissions))
    with db() as conn:
        row = conn.execute(
            "UPDATE bot_admins SET permissions=%s WHERE user_id=%s RETURNING user_id",
            (permission_text, uid)
        ).fetchone()
    if not row:
        await update.message.reply_text("အဲဒီ Admin ID မတွေ့ပါ။")
        return
    log_admin("set_admin_permissions", f"{uid}: {permission_text}", update.effective_user.id)
    await update.message.reply_text(f"Admin {uid} ရဲ့ Permission ပြင်ပြီးပါပြီ။")

async def deladmin_cmd(update, context):
    if not await owner_only(update, context):
        return
    if not context.args:
        await update.message.reply_text("အသုံးပြုရန်: /deladmin USER_ID")
        return
    try:
        uid = int(context.args[0])
        if uid <= 0 or uid == ADMIN_ID:
            raise ValueError()
    except ValueError:
        await update.message.reply_text("ဖျက်လို့မရသော ID သို့မဟုတ် ID မမှန်ပါ။")
        return
    with db() as conn:
        row = conn.execute(
            "DELETE FROM bot_admins WHERE user_id=%s RETURNING user_id",
            (uid,)
        ).fetchone()
    if not row:
        await update.message.reply_text("အဲဒီ Admin ID မတွေ့ပါ။")
        return
    log_admin("remove_admin", str(uid), update.effective_user.id)
    await update.message.reply_text(f"Admin {uid} ကို ဖယ်ရှားပြီးပါပြီ။")

async def admins_cmd(update, context):
    if not await owner_only(update, context):
        return
    with db() as conn:
        rows = conn.execute(
            "SELECT user_id,permissions,added_by,created_at FROM bot_admins ORDER BY created_at DESC"
        ).fetchall()
    if not rows:
        await update.message.reply_text("Admin အသစ် မရှိသေးပါ။")
        return
    lines = [
        f"{row['user_id']} — {row['permissions']} (added by {row['added_by']})"
        for row in rows
    ]
    await update.message.reply_text("🛡️ Admin List\n" + "\n".join(lines))

async def findadmin_cmd(update, context):
    if not await owner_only(update, context):
        return
    if len(context.args) != 1:
        await update.message.reply_text("အသုံးပြုရန်: /findadmin USER_ID")
        return
    try:
        uid = int(context.args[0])
        if uid <= 0:
            raise ValueError()
    except ValueError:
        await update.message.reply_text("User ID မမှန်ပါ။")
        return
    with db() as conn:
        row = conn.execute(
            "SELECT user_id,permissions,added_by,created_at FROM bot_admins WHERE user_id=%s",
            (uid,)
        ).fetchone()
    if not row:
        await update.message.reply_text(f"Admin {uid} ကို မတွေ့ပါ။")
        return
    await update.message.reply_text(
        "🔎 Admin ရှာဖွေမှု\n"
        f"ID: {row['user_id']}\n"
        f"Permission: {row['permissions']}\n"
        f"Added by: {row['added_by']}\n"
        f"Added at: {row['created_at']}"
    )

async def maintenance_cmd(update, context):
    if not await admin_only(update, context, "settings"): return
    if not context.args or context.args[0].lower() not in ("on", "off"):
        await update.message.reply_text("အသုံးပြုရန်: /maintenance on သို့ /maintenance off")
        return
    set_setting("maintenance", context.args[0].lower())
    log_admin("maintenance", context.args[0].lower(), update.effective_user.id)
    await update.message.reply_text("Maintenance " + context.args[0].upper())

async def setlimit_cmd(update, context):
    if not await admin_only(update, context, "settings"): return
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
    log_admin("setlimit", f"{context.args[0]}={value}", update.effective_user.id)
    await update.message.reply_text("Rate limit ပြင်ပြီးပါပြီ။")

async def broadcast_cmd(update, context):
    if not await admin_only(update, context, "broadcast"): return
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
    log_admin("broadcast", f"sent={sent},failed={failed}", update.effective_user.id)
    await update.message.reply_text(f"Broadcast ပြီးပါပြီ။ Success: {sent}, Failed: {failed}")

async def broadcastphoto_cmd(update, context):
    if not await admin_only(update, context, "broadcast"):
        return
    context.user_data["pending_broadcast_photo"] = True
    await update.message.reply_text(
        "📷 Broadcast ပို့မယ့် Photo ကို ပို့ပါ။ Caption ထည့်နိုင်ပါတယ်။ ပုံတစ်ပုံ ပို့ပြီးတာနဲ့ User အားလုံးဆီ ပို့ပါမယ်။"
    )


async def feedback_cmd(update, context):
    user = update.effective_user
    message = " ".join(context.args).strip()
    if not message:
        await update.message.reply_text("အသုံးပြုရန်: /feedback သင့်အကြံပြုချက် သို့မဟုတ် ပြဿနာ")
        return
    if len(message) > 1500:
        await update.message.reply_text("Feedback ကို စာလုံး ၁၅၀၀ အောက် ထားပေးပါ။")
        return
    with db() as conn:
        conn.execute(
            "INSERT INTO user_feedback(user_id,username,message) VALUES(%s,%s,%s)",
            (user.id, user.username, message)
        )
    await update.message.reply_text("✅ Feedback ပေးပို့ပြီးပါပြီ။ ကျေးဇူးတင်ပါတယ်။")


async def ban_cmd(update, context):
    if not await admin_only(update, context, "users"): return
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
    log_admin("ban", f"{uid}: {reason}", update.effective_user.id)
    await update.message.reply_text(f"{uid} ကို ban လုပ်ပြီးပါပြီ။")

async def tempban_cmd(update, context):
    if not await admin_only(update, context, "users"): return
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
    log_admin("tempban", f"{uid} for {minutes}m", update.effective_user.id)
    await update.message.reply_text(f"{uid} ကို {minutes} မိနစ် ban လုပ်ပြီးပါပြီ။")

async def unban_cmd(update, context):
    if not await admin_only(update, context, "users"): return
    if not context.args:
        await update.message.reply_text("အသုံးပြုရန်: /unban USER_ID"); return
    try: uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("User ID မမှန်ပါ။"); return
    with db() as conn:
        conn.execute("UPDATE users SET is_banned=FALSE,ban_until=NULL,ban_reason=NULL WHERE user_id=%s",
                     (uid,))
    log_admin("unban", str(uid), update.effective_user.id)
    await update.message.reply_text(f"{uid} ကို unban လုပ်ပြီးပါပြီ။")

async def forcejoin_cmd(update, context):
    if not await admin_only(update, context, "force_join"):
        return
    if not context.args or context.args[0].lower() not in ("on", "off"):
        await update.message.reply_text("အသုံးပြုရန်: /forcejoin on သို့ /forcejoin off")
        return
    value = context.args[0].lower()
    set_setting("force_join_enabled", value)
    log_admin("forcejoin", value, update.effective_user.id)
    await update.message.reply_text(f"Force Join {value.upper()} ပြီးပါပြီ။")


async def addchannel_cmd(update, context):
    if not await admin_only(update, context, "force_join"):
        return
    if len(context.args) != 2:
        await update.message.reply_text(
            "အသုံးပြုရန်: /addchannel @channel https://t.me/channel"
        )
        return
    channel, invite_url = context.args
    if not (channel.startswith("@") or channel.startswith("-100")):
        await update.message.reply_text("Channel ကို @username သို့မဟုတ် -100 ID ပုံစံထည့်ပါ။")
        return
    if not invite_url.startswith("https://t.me/"):
        await update.message.reply_text("Invite URL သည် https://t.me/ ဖြင့် စရပါမယ်။")
        return
    try:
        chat = await context.bot.get_chat(channel)
        bot_member = await context.bot.get_chat_member(chat.id, context.bot.id)
        if getattr(bot_member, "status", "") not in ("administrator", "creator"):
            await update.message.reply_text(
                "အရင်ဆုံး Bot ကို Channel ထဲမှာ Admin ပေးပြီးမှ ထပ်ထည့်ပါ။"
            )
            return
    except Exception as exc:
        log.warning("Could not validate Force Join channel %s: %s", channel, str(exc)[:180])
        await update.message.reply_text(
            "Channel ကို စစ်မရပါ။ Channel username/ID မှန်မမှန်စစ်ပြီး Bot ကို Admin ပေးထားပါ။"
        )
        return
    with db() as conn:
        conn.execute(
            "INSERT INTO force_join_channels(channel,invite_url) VALUES(%s,%s)",
            (channel, invite_url)
        )
    log_admin("add_force_join_channel", channel, update.effective_user.id)
    await update.message.reply_text(
        "Channel ထည့်ပြီးပါပြီ။ /forcejoin on လုပ်ပြီး အသုံးပြုနိုင်ပါပြီ။"
    )


async def delchannel_cmd(update, context):
    if not await admin_only(update, context, "force_join"):
        return
    if not context.args:
        await update.message.reply_text("အသုံးပြုရန်: /delchannel ID")
        return
    try:
        channel_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("ID မမှန်ပါ။")
        return
    with db() as conn:
        result = conn.execute(
            "DELETE FROM force_join_channels WHERE id=%s RETURNING id",
            (channel_id,)
        ).fetchone()
    log_admin("delete_force_join_channel", str(channel_id), update.effective_user.id)
    await update.message.reply_text(
        "Channel ဖျက်ပြီးပါပြီ။" if result else "အဲဒီ ID မတွေ့ပါ။"
    )


async def togglechannel_cmd(update, context):
    if not await admin_only(update, context, "force_join"):
        return
    if not context.args:
        await update.message.reply_text("အသုံးပြုရန်: /togglechannel ID")
        return
    try:
        channel_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("ID မမှန်ပါ။")
        return
    with db() as conn:
        row = conn.execute(
            "UPDATE force_join_channels SET enabled=NOT enabled "
            "WHERE id=%s RETURNING enabled",
            (channel_id,)
        ).fetchone()
    if not row:
        await update.message.reply_text("အဲဒီ Channel ID မတွေ့ပါ။")
        return
    log_admin("toggle_force_join_channel", f"{channel_id}: {row['enabled']}", update.effective_user.id)
    await update.message.reply_text(
        "Channel ကို " + ("ON" if row["enabled"] else "OFF") + " ပြောင်းပြီးပါပြီ။"
    )


async def addbutton_cmd(update, context):
    if not await admin_only(update, context, "buttons"): return
    if len(context.args) < 2 or not context.args[-1].startswith("https://"):
        await update.message.reply_text("အသုံးပြုရန်: /addbutton Button Name https://URL"); return
    label = " ".join(context.args[:-1])
    url = context.args[-1]
    with db() as conn:
        conn.execute("INSERT INTO bot_buttons(label,url) VALUES(%s,%s)", (label, url))
    log_admin("addbutton", label, update.effective_user.id)
    await update.message.reply_text("Button ထည့်ပြီးပါပြီ။ /start ပြန်နှိပ်ကြည့်ပါ။")

async def delbutton_cmd(update, context):
    if not await admin_only(update, context, "buttons"): return
    if not context.args: await update.message.reply_text("အသုံးပြုရန်: /delbutton ID"); return
    try: bid = int(context.args[0])
    except ValueError: await update.message.reply_text("ID မမှန်ပါ။"); return
    with db() as conn: conn.execute("DELETE FROM bot_buttons WHERE id=%s", (bid,))
    log_admin("delbutton", str(bid), update.effective_user.id)
    await update.message.reply_text("Button ဖျက်ပြီးပါပြီ။")

async def addad_cmd(update, context):
    if not await admin_only(update, context, "ads"):
        return
    if not context.args:
        await update.message.reply_text(
            "စာသား Ad: /addad welcome စာသား\n"
            "Photo Album: /addad welcome ပြီးရင် ပုံတွေ ပို့ပြီး /savead [caption] နှိပ်ပါ။"
        )
        return
    placement = context.args[0].strip().lower()
    if placement not in ("welcome", "after_download"):
        await update.message.reply_text("Placement ကို welcome သို့ after_download သတ်မှတ်ပါ။")
        return
    message = " ".join(context.args[1:]).strip()
    if not message:
        context.user_data["pending_ad_placement"] = placement
        context.user_data["pending_ad_photos"] = []
        await update.message.reply_text(
            f"📸 {placement} Photo Ad အတွက် ပုံတွေကို တစ်ပုံချင်း သို့မဟုတ် Album အနေနဲ့ ပို့ပါ။\n"
            "ပြီးရင် /savead [caption] ကိုနှိပ်ပါ။ မလုပ်တော့ရင် /cancelad နှိပ်ပါ။"
        )
        return
    with db() as conn:
        conn.execute(
            "INSERT INTO bot_ads(placement,text) VALUES(%s,%s)",
            (placement, message)
        )
    log_admin("addad_text", placement, update.effective_user.id)
    await update.message.reply_text("စာသား Ad ထည့်ပြီးပါပြီ။")


async def handle_ad_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("pending_broadcast_photo"):
        if not has_permission(update.effective_user.id, "broadcast"):
            context.user_data.pop("pending_broadcast_photo", None)
            return
        file_id = update.message.photo[-1].file_id
        caption = update.message.caption or ""
        with db() as conn:
            ids = [row["user_id"] for row in conn.execute(
                "SELECT user_id FROM users WHERE is_banned=FALSE"
            ).fetchall()]
        sent = failed = 0
        for uid in ids:
            try:
                await context.bot.send_photo(uid, photo=file_id, caption=caption[:1024] or None)
                sent += 1
            except Exception:
                failed += 1
            await asyncio.sleep(0.04)
        context.user_data.pop("pending_broadcast_photo", None)
        log_admin("broadcast_photo", f"sent={sent},failed={failed}", update.effective_user.id)
        await update.message.reply_text(
            f"📷 Photo Broadcast ပြီးပါပြီ။ Success: {sent}, Failed: {failed}"
        )
        return

    if not has_permission(update.effective_user.id, "ads"):
        return
    placement = context.user_data.get("pending_ad_placement")
    if not placement:
        await update.message.reply_text(
            "Photo Ad ထည့်ရန် အရင် /addad welcome သို့ /addad after_download လုပ်ပါ။"
        )
        return
    photos = context.user_data.setdefault("pending_ad_photos", [])
    photos.append(update.message.photo[-1].file_id)
    if update.message.caption:
        context.user_data["pending_ad_caption"] = update.message.caption.strip()
    await update.message.reply_text(
        f"✅ ပုံ {len(photos)} ပုံ သိမ်းဆည်းရန် အဆင်သင့်ဖြစ်ပါပြီ။\n"
        "နောက်ထပ်ပုံပို့နိုင်သလို ပြီးရင် /savead [caption] နှိပ်ပါ။"
    )


async def savead_cmd(update, context):
    if not await admin_only(update, context, "ads"):
        return
    placement = context.user_data.get("pending_ad_placement")
    photos = context.user_data.get("pending_ad_photos", [])
    if not placement or not photos:
        await update.message.reply_text("သိမ်းရန် Photo မရှိသေးပါ။ /addad placement နဲ့ စပါ။")
        return
    caption = " ".join(context.args).strip() or context.user_data.get("pending_ad_caption", "")
    group_id = f"mediax-{update.effective_user.id}-{int(time.time())}"
    with db() as conn:
        for file_id in photos:
            conn.execute(
                "INSERT INTO bot_ads(placement,text,media_file_id,media_group_id) "
                "VALUES(%s,%s,%s,%s)",
                (placement, caption, file_id, group_id)
            )
    log_admin("addad_photos", f"{placement}: {len(photos)} photos", update.effective_user.id)
    context.user_data.pop("pending_ad_placement", None)
    context.user_data.pop("pending_ad_photos", None)
    context.user_data.pop("pending_ad_caption", None)
    await update.message.reply_text(f"✅ Photo Ad {len(photos)} ပုံ သိမ်းပြီးပါပြီ။")


async def cancelad_cmd(update, context):
    if not await admin_only(update, context, "ads"):
        return
    context.user_data.pop("pending_ad_placement", None)
    context.user_data.pop("pending_ad_photos", None)
    context.user_data.pop("pending_ad_caption", None)
    await update.message.reply_text("Photo Ad ပြင်ဆင်မှု ပယ်ဖျက်ပြီးပါပြီ။")

async def togglead_cmd(update, context):
    if not await admin_only(update, context, "ads"): return
    if not context.args: await update.message.reply_text("အသုံးပြုရန်: /togglead ID"); return
    try: aid = int(context.args[0])
    except ValueError: await update.message.reply_text("ID မမှန်ပါ။"); return
    with db() as conn:
        row = conn.execute(
            "SELECT media_group_id FROM bot_ads WHERE id=%s", (aid,)
        ).fetchone()
        if row and row["media_group_id"]:
            conn.execute(
                "UPDATE bot_ads SET enabled=NOT enabled WHERE media_group_id=%s",
                (row["media_group_id"],)
            )
        elif row:
            conn.execute("UPDATE bot_ads SET enabled=NOT enabled WHERE id=%s", (aid,))
    log_admin("togglead", str(aid), update.effective_user.id)
    await update.message.reply_text("Ad အခြေအနေ ပြောင်းပြီးပါပြီ။")

async def delad_cmd(update, context):
    if not await admin_only(update, context, "ads"): return
    if not context.args: await update.message.reply_text("အသုံးပြုရန်: /delad ID"); return
    try: aid = int(context.args[0])
    except ValueError: await update.message.reply_text("ID မမှန်ပါ။"); return
    with db() as conn:
        row = conn.execute(
            "SELECT media_group_id FROM bot_ads WHERE id=%s", (aid,)
        ).fetchone()
        if row and row["media_group_id"]:
            conn.execute(
                "DELETE FROM bot_ads WHERE media_group_id=%s",
                (row["media_group_id"],)
            )
        elif row:
            conn.execute("DELETE FROM bot_ads WHERE id=%s", (aid,))
    log_admin("delad", str(aid), update.effective_user.id)
    await update.message.reply_text("Ad ဖျက်ပြီးပါပြီ။")

async def create_backup(chat_id, context, admin_id=None):
    tables = ["users", "downloads", "bot_settings", "bot_buttons", "bot_ads",
              "force_join_channels", "admin_logs", "error_logs", "request_events", "backup_log"]
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
        file.name = f"mediax_backup_{datetime.now():%Y%m%d_%H%M%S}.json"
        await context.bot.send_document(chat_id, file, caption="MediaX Database Backup (JSON)")
        with db() as conn:
            conn.execute("INSERT INTO backup_log(status,details) VALUES('success','Manual backup sent')")
        log_admin("backup", "manual success", admin_id)
    except Exception as exc:
        with db() as conn:
            conn.execute("INSERT INTO backup_log(status,details) VALUES('failed',%s)", (str(exc)[:1000],))
        await context.bot.send_message(chat_id, f"Backup မအောင်မြင်ပါ: {str(exc)[:300]}")

async def backup_cmd(update, context):
    if not await admin_only(update, context, "backup"): return
    await create_backup(update.effective_chat.id, context, update.effective_user.id)

async def admin_command(update, context):
    if not await admin_only(update, context, "dashboard"): return
    await update.message.reply_text(
        "MediaX Admin Dashboard",
        reply_markup=admin_keyboard(update.effective_user.id)
    )

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
        return jsonify({"status": "ok", "service": "MediaX", "database": "ok"})
    except Exception:
        return jsonify({"status": "degraded", "database": "error"}), 503

def run_web():
    port = int(os.getenv("PORT", "10000"))
    web.run(host="0.0.0.0", port=port, use_reloader=False)

async def post_init(app: Application):
    init_db()
    log.info("MediaX bot started. Admin ID=%s", ADMIN_ID)

def main():
    import threading
    threading.Thread(target=run_web, daemon=True).start()
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("addadmin", addadmin_cmd))
    app.add_handler(CommandHandler("setadminperm", setadminperm_cmd))
    app.add_handler(CommandHandler("deladmin", deladmin_cmd))
    app.add_handler(CommandHandler("admins", admins_cmd))
    app.add_handler(CommandHandler("findadmin", findadmin_cmd))
    app.add_handler(CommandHandler("maintenance", maintenance_cmd))
    app.add_handler(CommandHandler("setlimit", setlimit_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))
    app.add_handler(CommandHandler("broadcastphoto", broadcastphoto_cmd))
    app.add_handler(CommandHandler("feedback", feedback_cmd))
    app.add_handler(CommandHandler("ban", ban_cmd))
    app.add_handler(CommandHandler("tempban", tempban_cmd))
    app.add_handler(CommandHandler("unban", unban_cmd))
    app.add_handler(CommandHandler("forcejoin", forcejoin_cmd))
    app.add_handler(CommandHandler("addchannel", addchannel_cmd))
    app.add_handler(CommandHandler("delchannel", delchannel_cmd))
    app.add_handler(CommandHandler("togglechannel", togglechannel_cmd))
    app.add_handler(CommandHandler("addbutton", addbutton_cmd))
    app.add_handler(CommandHandler("delbutton", delbutton_cmd))
    app.add_handler(CommandHandler("addad", addad_cmd))
    app.add_handler(CommandHandler("savead", savead_cmd))
    app.add_handler(CommandHandler("cancelad", cancelad_cmd))
    app.add_handler(MessageHandler(filters.PHOTO & filters.ChatType.PRIVATE, handle_ad_photo))
    app.add_handler(CommandHandler("togglead", togglead_cmd))
    app.add_handler(CommandHandler("delad", delad_cmd))
    app.add_handler(CommandHandler("backup", backup_cmd))
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(error_handler)
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
