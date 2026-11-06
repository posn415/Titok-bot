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

# အောက်က မျက်တောင်နှစ်ခုကြားမှာ သင့် Token ကို ထည့်ပါ
TOKEN = "9248832412:AAH00JDG8JA_wLFCNRQwEQZOOUQJnQZQleE"
RENDER_URL = "https://titok-bot.onrender.com"

app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is alive 24/7!"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

def keep_alive_ping():
    """ဆာဗာ အိပ်မသွားစေရန် ၁၀ မိနစ်တစ်ကြိမ် လှမ်းနှိုးပေးခြင်း"""
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

    # နည်းလမ်း ၁ - TikWM API
    try:
        r1 = session.post(
            "https://www.tikwm.com/api/",
            data={"url": raw_url, "hd": 1},
            headers={"Referer": "https://www.tikwm.com/"},
            timeout=15
        )
        if r1.status_code == 200 and r1.text.strip().startswith("{"):
            data = r1.json()
            if data.get("code") == 0 and "data" in data:
                if data["data"].get("images"):
                    return None, None, "ဤပို့စ်သည် ဗီဒီယိုမဟုတ်ဘဲ ဓာတ်ပုံ (Slide) ပို့စ် ဖြစ်နေပါသည်။"
                v_url = data["data"].get("play") or data["data"].get("hdplay")
                if v_url:
                    video_url = ("https://www.tikwm.com" + v_url) if v_url.startswith("/") else v_url
    except Exception as e:
        debug_logs.append(f"M1: {str(e)[:20]}")

    # နည်းလမ်း ၂ - TikTok Web Page မှ တိုက်ရိုက်ဆွဲထုတ်ခြင်း
    if not video_url:
        try:
            r2 = session.get(raw_url, allow_redirects=True, timeout=15)
            match = re.search(r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>', r2.text)
            if match:
                js_data = json.loads(match.group(1))
                item_struct = (
                    js_data.get("__DEFAULT_SCOPE__", {})
                    .get("webapp.video-detail", {})
                    .get("itemInfo", {})
                    .get("itemStruct", {})
                )
                video_info = item_struct.get("video", {})
                video_url = video_info.get("playAddr") or video_info.get("downloadAddr")
        except Exception as e:
            debug_logs.append(f"M2: {str(e)[:20]}")

    if not video_url:
        return None, None, (" | ".join(debug_logs) if debug_logs else "ဗီဒီယို လင့်ခ် ရှာမတွေ့ပါ။")

    # ဗီဒီယိုဖိုင်ကို Background တွင် တစ်ခါတည်း ဒေါင်းလုဒ်ဆွဲခြင်း
    try:
        vid_res = session.get(video_url, headers={"Referer": "https://www.tiktok.com/"}, timeout=25)
        if vid_res.status_code == 200 and len(vid_res.content) > 10000:
            return vid_res.content, video_url, None
    except Exception:
        pass

    return None, video_url, None

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    match = re.search(r'(https?://[^\s]+tiktok\.com[^\s]*)', text)
    if not match:
        await update.message.reply_text("TikTok link ပို့ပေးပါ။")
        return

    url = match.group(1)
    status_msg = await update.message.reply_text("ဒေါင်းလုဒ်ဆွဲနေပါတယ် ခဏစောင့်ပါ...")

    try:
        # Bot လုံးဝမထစ်စေရန် သီးသန့် Thread ဖြင့် ဒေါင်းလုဒ်ဆွဲခြင်း
        vid_bytes, video_url, err_detail = await asyncio.to_thread(fetch_video_bytes, url)

        if err_detail:
            await status_msg.edit_text(f"ဗီဒီယို ဆွဲမရပါ။\nအကြောင်းရင်း: {err_detail}")
            return

        if vid_bytes:
            video_file = BytesIO(vid_bytes)
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
    except Exception as e:
        await status_msg.edit_text(f"အမှားတစ်ခု ဖြစ်ပေါ်နေပါသည်: {e}")

if __name__ == '__main__':
    threading.Thread(target=run_flask, daemon=True).start()
    threading.Thread(target=keep_alive_ping, daemon=True).start()
    bot_app = ApplicationBuilder().token(TOKEN).build()
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    bot_app.run_polling(drop_pending_updates=True)
    
