import os
import threading
import requests
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

# အောက်က YOUR_BOT_TOKEN နေရာမှာ သင့် Token ကို ထည့်ပါ
TOKEN = os.environ.get("BOT_TOKEN", "9248832412:AAH00JDG8JA_wLFCNRQwEQZOOUQJnQZQleE")

app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is alive!"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text
    if "tiktok.com" not in url:
        await update.message.reply_text("TikTok link ပို့ပေးပါ။")
        return

    status_msg = await update.message.reply_text("ဒေါင်းလုဒ်ဆွဲနေပါတယ် ခဏစောင့်ပါ...")

    try:
        api_url = "https://www.tikwm.com/api/"
        response = requests.post(api_url, data={"url": url}).json()

        if response.get("code") == 0:
            video_url = response["data"]["play"]
            await update.message.reply_video(video=video_url, caption="Watermark မပါသော ဗီဒီယို ရပါပြီ ✨")
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
    
