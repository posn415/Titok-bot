import os
import re
import threading
import requests
import cloudscraper
from io import BytesIO
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

# အောက်က မျက်တောင်နှစ်ခုကြားမှာ သင့် Token ကို ထည့်ပါ
TOKEN = "9248832412:AAH00JDG8JA_wLFCNRQwEQZOOUQJnQZQleE"

app = Flask(__name__)
scraper = cloudscraper.create_scraper(browser={'browser': 'chrome', 'platform': 'windows', 'mobile': False})

@app.route('/')
def home():
    return "Bot is alive!"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

def get_tiktok_video(url):
    # Link အတိုဖြစ်နေပါက Link အရှည်နှင့် Video ID ရအောင် အရင်ပြောင်းခြင်း
    clean_url = url
    video_id = None
    try:
        resp = scraper.get(url, allow_redirects=True, timeout=10)
        clean_url = resp.url.split("?")[0]
    except Exception:
        pass

    id_match = re.search(r'/video/(\d+)', clean_url)
    if id_match:
        video_id = id_match.group(1)

    # နည်းလမ်း ၁ - TikWM (Cloudscraper ဖြင့် Cloudflare ကျော်ခြင်း)
    try:
        r1 = scraper.post(
            "https://www.tikwm.com/api/",
            data={"url": clean_url, "count": 12, "cursor": 0, "web": 1, "hd": 1},
            timeout=15
        )
        if r1.status_code == 200:
            data = r1.json()
            if data.get("code") == 0 and "data" in data:
                v_url = data["data"].get("hdplay") or data["data"].get("play")
                if v_url:
                    return ("https://www.tikwm.com" + v_url) if v_url.startswith("/") else v_url
    except Exception:
        pass

    # နည်းလမ်း ၂ - TiklyDown v3 API
    for api_ep in ["https://api.tiklydown.eu.org/api/download", "https://api.tiklydown.eu.org/api/download/v3"]:
        try:
            r2 = scraper.get(api_ep, params={"url": clean_url}, timeout=15)
            if r2.status_code == 200:
                data = r2.json()
                v_url = (
                    data.get("video", {}).get("noWatermark")
                    or data.get("result", {}).get("video")
                )
                if v_url:
                    return v_url
        except Exception:
            pass

    # နည်းလမ်း ၃ - TikTok Official Embed Page မှ တိုက်ရိုက်ဆွဲယူခြင်း
    if video_id:
        try:
            embed_url = f"https://www.tiktok.com/embed/v2/{video_id}"
            r3 = scraper.get(embed_url, timeout=15)
            matches = re.findall(r'"playAddr":"([^"]+)"', r3.text)
            if matches:
                return matches[0].encode('utf-8').decode('unicode_escape').replace("\\/", "/")
        except Exception:
            pass

    # နည်းလမ်း ၄ - OffTikTok / TnkTok Mirror မှ ဆွဲယူခြင်း
    for domain in ["tfxktok.com", "vxtiktok.com"]:
        try:
            m_url = re.sub(r'(www\.|vm\.|vt\.)?tiktok\.com', domain, clean_url)
            r4 = requests.get(m_url, headers={"User-Agent": "TelegramBot (like TwitterBot)"}, timeout=15)
            match = re.search(r'<meta property="og:video" content="([^"]+)"', r4.text)
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
            await status_msg.edit_text("ဗီဒီယို ဆွဲမရပါ။ (ဓာတ်ပုံ Slide ပို့စ် မဟုတ်ကြောင်း သို့မဟုတ် Private အကောင့် မဟုတ်ကြောင်း စစ်ဆေးပါ)")
            return

        try:
            vid_res = scraper.get(video_url, timeout=30)
            if vid_res.status_code == 200 and len(vid_res.content) > 10000:
                video_file = BytesIO(vid_res.content)
                video_file.name = "tiktok.mp4"
                await update.message.reply_video(
                    video=video_file,
                    caption="Watermark မပါသော ဗီဒီယို ရပါပြီ ✨"
                )
                await status_msg.delete()
                return
        except Exception:
            pass

        await update.message.reply_video(
            video=video_url,
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
    
