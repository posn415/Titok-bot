import os
import asyncio
from threading import Thread
from flask import Flask
import yt_dlp
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# Server ၏ Environment Variable မှ Token ကို ဖတ်ယူခြင်း
BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")

# Render တွင် 24/7 run နိုင်ရန် Flask Web Server တည်ဆောက်ခြင်း
web_app = Flask(__name__)

@web_app.route('/')
def home():
    return "Bot is alive and running!"

def run_web():
    port = int(os.environ.get("PORT", 8080))
    web_app.run(host="0.0.0.0", port=port)

def keep_alive():
    t = Thread(target=run_web)
    t.daemon = True
    t.start()

def download_tiktok_video(url: str, output_path: str) -> str:
    ydl_opts = {
        'outtmpl': output_path,
        'format': 'best[ext=mp4]/best',
        'quiet': True,
        'no_warnings': True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])
    return output_path

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "မင်္ဂလာပါ! TikTok video link ပို့ပေးပါ။ Logo မပါတဲ့ video ပြန်ပို့ပေးပါမယ်။"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()

    if "tiktok.com" not in url:
        await update.message.reply_text("ကျေးဇူးပြု၍ မှန်ကန်သော TikTok link ပို့ပေးပါ။")
        return

    status_msg = await update.message.reply_text("Video ဒေါင်းလုဒ်ဆွဲနေပါသည်၊ ခဏစောင့်ပါ...")
    file_path = f"tiktok_{update.message.chat_id}_{update.message.message_id}.mp4"

    try:
        await asyncio.to_thread(download_tiktok_video, url, file_path)

        with open(file_path, 'rb') as video_file:
            await update.message.reply_video(
                video=video_file,
                caption="Watermark မပါသော Video ရပါပြီ။"
            )
        await status_msg.delete()

    except Exception as e:
        await status_msg.edit_text("Video ဒေါင်းလုဒ်ဆွဲရာတွင် အမှားအယွင်းရှိနေပါသည်။")
        print(f"Error: {e}")

    finally:
        if os.path.exists(file_path):
            os.remove(file_path)

def main():
    keep_alive()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Bot စတင် အလုပ်လုပ်နေပါပြီ...")
    app.run_polling()

if __name__ == "__main__":
    main()
  
