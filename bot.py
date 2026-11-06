import os
import re
import threading
import requests
from io import BytesIO
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

# အောက်က မျက်တောင်နှစ်ခုကြားမှာ သင့် Token ကို ထည့်ပါ
TOKEN = "9248832412:AAH00JDG8JA_wLFCNRQwEQZOOUQJnQZQleE"

app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is alive!"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

def get_tiktok_video(url):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    }

    # နည်းလမ်း ၁ - TikWM (GET method)
    try:
        r1 = requests.get("https://www.tikwm.com/api/", params={"url": url, "hd": 1}, headers=headers, timeout=15)
        if r1.status_code == 200 and r1.text.strip().startswith("{"):
            data = r1.json()
            if data.get("code") == 0 and "data" in data:
                v_url = data["data"].get("play")
                if v_url:
                    return ("https://www.tikwm.com" + v_url) if not v_url.startswith("http") else v_url
    except Exception:
        pass

    # နည်းလမ်း ၂ - TiklyDown API
    try:
        r2 = requests.get("https://api.tiklydown.eu.org/api/download", params={"url": url}, headers=headers, timeout=15)
        if r2.status_code == 200 and r2.text.strip().startswith("{"):
            data = r2.json()
            v_url = data.get("video", {}).get("noWatermark")
            if v_url:
                return v_url
    except Exception:
        pass

    # နည်းလမ်း ၃ - vxTikTok Direct Fallback
    try:
        r3 = requests.get(url, headers=headers, allow_redirects=True, timeout=15)
        vx_url = r3.url.replace("tiktok.com", "vxtiktok.com")
        r_vx = requests.get(vx_url, headers={"User-Agent": "TelegramBot (like TwitterBot)"}, timeout=15)
        match = re.search(r'<meta property="og:video" content="([^"]+)"', r_vx.text)
        if match:
            return match.group(1)
    except Exception:
        pass

    return None

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    match = re.search(r'(https?://[^\s]+tiktok\.com[^\s]*)', text)
    if not match:
        await update.message.reply_text("TikTok link ပို့ပေးပါ။")
        return

    url = match.group(1)
    status_msg = await update.message.reply_text("ဒေါင်းလုဒ်ဆွဲနေပါတယ် ခဏစောင့်ပါ...")

    try:
        video_url = get_tiktok_video(url)
        if not video_url:
            await status_msg.edit_text("ဗီဒီယို ဆွဲမရပါ။ Link မှန်မမှန် ပြန်စစ်ပေးပါ။")
            return

        try:
            # Telegram မှ တိုက်ရိုက်ပို့ကြည့်ခြင်း
            await update.message.reply_video(
                video=video_url,
                caption="Watermark မပါသော ဗီဒီယို ရပါပြီ ✨"
            )
            await status_msg.delete()
        except Exception:
            # တိုက်ရိုက်ပို့မရပါက ဖိုင်ဆွဲချပြီးမှ ပို့ပေးခြင်း
            headers = {"User-Agent": "Mozilla/5.0"}
            vid_res = requests.get(video_url, headers=headers, timeout=30)
            video_file = BytesIO(vid_res.content)
            video_file.name = "tiktok.mp4"
            await update.message.reply_video(
                video=video_file,
                caption="Watermark မပါသော ဗီဒီယို ရပါပြီ ✨"
            )
            await status_msg.delete()
    except Exception as e:
        await status_msg.edit_text(f"အမှားတစ်ခု ဖြစ်ပေါ်နေပါသည်: {e}")

if __name__ == '__main__':
    threading.Thread(target=run_flask, daemon=True).start()
    bot_app = ApplicationBuilder().token(TOKEN).build()
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    bot_app.run_polling(drop_pending_updates=True)
    
