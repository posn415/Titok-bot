import os
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

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    if "tiktok.com" not in url:
        await update.message.reply_text("TikTok link ပို့ပေးပါ။")
        return

    status_msg = await update.message.reply_text("ဒေါင်းလုဒ်ဆွဲနေပါတယ် ခဏစောင့်ပါ...")

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/javascript, */*; q=0.01",
    }

    try:
        api_url = "https://www.tikwm.com/api/"
        res = requests.post(api_url, data={"url": url, "hd": 1}, headers=headers, timeout=20)
        response = res.json()

        if response.get("code") == 0 and "data" in response:
            video_url = response["data"].get("play")
            if video_url and not video_url.startswith("http"):
                video_url = "https://www.tikwm.com" + video_url

            # ဗီဒီယိုဖိုင်ကို ဒေါင်းလုဒ်ဆွဲပြီး Telegram သို့ ပို့ပေးခြင်း
            vid_res = requests.get(video_url, headers=headers, timeout=30)
            video_file = BytesIO(vid_res.content)
            video_file.name = "tiktok_video.mp4"

            await update.message.reply_video(
                video=video_file,
                caption="Watermark မပါသော ဗီဒီယို ရပါပြီ ✨"
            )
            await status_msg.delete()
        else:
            await status_msg.edit_text("ဗီဒီယို ဆွဲမရပါ။ Link မှန်မမှန် ပြန်စစ်ပေးပါ။")
    except Exception as e:
        await status_msg.edit_text(f"အမှားတစ်ခု ဖြစ်ပေါ်နေပါသည်: {e}")

if __name__ == '__main__':
    threading.Thread(target=run_flask, daemon=True).start()
    bot_app = ApplicationBuilder().token(TOKEN).build()
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    bot_app.run_polling(drop_pending_updates=True)
    
