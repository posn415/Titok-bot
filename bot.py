
import os
import re
import json
import time
import asyncio
import threading
from io import BytesIO

from flask import Flask
from curl_cffi import requests as c_requests
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

# Render Environment မှ Bot Token ယူမည်
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("BOT_TOKEN is missing in Render Environment.")

RENDER_URL = "https://titok-bot.onrender.com"

app = Flask(__name__)


@app.route("/")
def home():
    return "Bot is alive 24/7!"


def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)


def keep_alive_ping():
    while True:
        time.sleep(600)
        try:
            c_requests.get(RENDER_URL, timeout=10)
        except Exception:
            pass


def fetch_video_bytes(raw_url):
    debug_logs = []
    session = c_requests.Session(impersonate="chrome120")
    video_url = None

    # Method 1 - TikWM API
    try:
        response = session.post(
            "https://www.tikwm.com/api/",
            data={"url": raw_url, "hd": 1},
            headers={"Referer": "https://www.tikwm.com/"},
            timeout=15
        )

        if response.status_code == 200 and response.text.strip().startswith("{"):
            data = response.json()

            if data.get("code") == 0 and "data" in data:
                if data["data"].get("images"):
                    return None, None, "ဤပို့စ်သည် ဓာတ်ပုံ (Slide) ပို့စ် ဖြစ်နေပါသည်။"

                video_path = data["data"].get("play") or data["data"].get("hdplay")

                if video_path:
                    video_url = (
                        "https://www.tikwm.com" + video_path
                        if video_path.startswith("/")
                        else video_path
                    )

    except Exception as exc:
        debug_logs.append(f"M1: {str(exc)[:20]}")

    # Method 2 - TikTok Web Page
    if not video_url:
        try:
            response = session.get(
                raw_url,
                allow_redirects=True,
                timeout=15
            )

            match = re.search(
                r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>',
                response.text
            )

            if match:
                js_data = json.loads(match.group(1))

                item_struct = (
                    js_data.get("__DEFAULT_SCOPE__", {})
                    .get("webapp.video-detail", {})
                    .get("itemInfo", {})
                    .get("itemStruct", {})
                )

                video_info = item_struct.get("video", {})

                video_url = (
                    video_info.get("playAddr")
                    or video_info.get("downloadAddr")
                )

        except Exception as exc:
            debug_logs.append(f"M2: {str(exc)[:20]}")

    if not video_url:
        detail = (
            " | ".join(debug_logs)
            if debug_logs
            else "ဗီဒီယို လင့်ခ် ရှာမတွေ့ပါ။"
        )
        return None, None, detail

    # Download video
    try:
        video_response = session.get(
            video_url,
            headers={"Referer": "https://www.tiktok.com/"},
            timeout=25
        )

        if (
            video_response.status_code == 200
            and len(video_response.content) > 10000
        ):
            return video_response.content, video_url, None

    except Exception:
        pass

    return None, video_url, None


async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not update.message or not update.message.text:
        return

    message_text = update.message.text.strip()

    match = re.search(
        r"(https?://[^\s]*tiktok\.com[^\s]*)",
        message_text
    )

    if not match:
        await update.message.reply_text("TikTok link ပို့ပေးပါ။")
        return

    url = match.group(1)

    status_msg = await update.message.reply_text(
        "ဒေါင်းလုဒ်ဆွဲနေပါတယ် ခဏစောင့်ပါ..."
    )

    try:
        video_bytes, video_url, error_detail = await asyncio.to_thread(
            fetch_video_bytes,
            url
        )

        if error_detail:
            await status_msg.edit_text(
                f"ဗီဒီယို ဆွဲမရပါ။\nအကြောင်းရင်း: {error_detail}"
            )
            return

        if video_bytes:
            video_file = BytesIO(video_bytes)
            video_file.name = "tiktok.mp4"

            await update.message.reply_video(
                video=video_file,
                caption="Watermark မပါသော ဗီဒီယို ရပါပြီ ✨"
            )

            await status_msg.delete()

        elif video_url:
            await update.message.reply_video(
                video=video_url,
                caption="Watermark မပါသော ဗီဒီယို ရပါပြီ ✨"
            )

            await status_msg.delete()

        else:
            await status_msg.edit_text(
                "ဗီဒီယို လင့်ခ် ရှာမတွေ့ပါ။"
            )

    except Exception as exc:
        await status_msg.edit_text(
            f"အမှားတစ်ခု ဖြစ်ပေါ်နေပါသည်: {exc}"
        )


if __name__ == "__main__":

    threading.Thread(
        target=run_flask,
        daemon=True
    ).start()

    threading.Thread(
        target=keep_alive_ping,
        daemon=True
    ).start()

    bot_app = ApplicationBuilder().token(TOKEN).build()

    bot_app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_message
        )
    )

    bot_app.run_polling(drop_pending_updates=True)
        
