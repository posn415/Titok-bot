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

def get_tiktok_video(raw_url):
    debug_logs = []
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Referer": "https://www.tikwm.com/"
    }

    # နည်းလမ်း ၁ - TikWM သို့ မူရင်း Link အတိုအတိုင်း တိုက်ရိုက်ပို့ခြင်း (Redirect မလုပ်ပါ)
    for target_url in [raw_url]:
        try:
            r1 = scraper.post(
                "https://www.tikwm.com/api/",
                data={"url": target_url, "hd": 1},
                headers=headers,
                timeout=15
            )
            if r1.status_code == 200 and r1.text.strip().startswith("{"):
                data = r1.json()
                if data.get("code") == 0 and "data" in data:
                    # အကယ်၍ ဓာတ်ပုံ Slide ပို့စ်ဖြစ်နေလျှင်
                    if data["data"].get("images"):
                        return None, "ဤပို့စ်သည် ဗီဒီယိုမဟုတ်ဘဲ ဓာတ်ပုံ (Slide) ပို့စ် ဖြစ်နေပါသည်။"
                    v_url = data["data"].get("play") or data["data"].get("hdplay") or data["data"].get("wmplay")
                    if v_url:
                        if v_url.startswith("/"):
                            v_url = "https://www.tikwm.com" + v_url
                        return v_url, None
                else:
                    debug_logs.append(f"TikWM: {data.get('msg', 'code!=0')}")
            else:
                debug_logs.append(f"TikWM HTTP {r1.status_code}")
        except Exception as e:
            debug_logs.append(f"TikWM Err: {str(e)[:25]}")

    # နည်းလမ်း ၂ - TiklyDown API (မူရင်း Link အတိုင်း ပို့ခြင်း)
    for ep in ["https://api.tiklydown.eu.org/api/download", "https://api.tiklydown.eu.org/api/download/v3"]:
        try:
            r2 = requests.get(ep, params={"url": raw_url}, headers=headers, timeout=15)
            if r2.status_code == 200 and r2.text.strip().startswith("{"):
                data = r2.json()
                v_url = (
                    data.get("video", {}).get("noWatermark")
                    or data.get("result", {}).get("video")
                )
                if v_url:
                    return v_url, None
        except Exception as e:
            debug_logs.append(f"Tikly Err: {str(e)[:20]}")

    # နည်းလမ်း ၃ - Mobile Header ဖြင့် Link အရှည်ဖြည်ပြီး TikWM ထပ်စမ်းခြင်း
    try:
        mob_headers = {"User-Agent": "com.zhiliaoapp.musically/2023501030 (Linux; U; Android 13; en_US; Pixel 7; Build/TD1A.220804.031; Cronet/58.0.2991.0)"}
        r_redir = requests.head(raw_url, headers=mob_headers, allow_redirects=True, timeout=10)
        resolved_url = r_redir.url.split("?")[0]
        if resolved_url != raw_url and "tiktok.com" in resolved_url:
            r3 = scraper.get("https://www.tikwm.com/api/", params={"url": resolved_url}, headers=headers, timeout=15)
            if r3.status_code == 200 and r3.text.strip().startswith("{"):
                data = r3.json()
                if data.get("code") == 0 and "data" in data:
                    v_url = data["data"].get("play")
                    if v_url:
                        return ("https://www.tikwm.com" + v_url) if v_url.startswith("/") else v_url, None
    except Exception as e:
        debug_logs.append(f"Redir Err: {str(e)[:20]}")

    # နည်းလမ်း ၄ - vxTikTok / tfxktok JSON API
    for domain in ["tfxktok.com", "vxtiktok.com"]:
        try:
            m_url = re.sub(r'(www\.|vm\.|vt\.)?tiktok\.com', domain, raw_url)
            r4 = requests.get(m_url, headers={"User-Agent": "TelegramBot (like TwitterBot)"}, timeout=15)
            match = re.search(r'<meta property="og:video" content="([^"]+)"', r4.text)
            if match:
                return match.group(1), None
        except Exception:
            pass

    return None, " | ".join(debug_logs)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    match = re.search(r'(https?://[^\s]+tiktok\.com[^\s]*)', text)
    if not match:
        await update.message.reply_text("TikTok link ပို့ပေးပါ။")
        return

    url = match.group(1)
    status_msg = await update.message.reply_text("ဒေါင်းလုဒ်ဆွဲနေပါတယ် ခဏစောင့်ပါ...")

    try:
        video_url, err_detail = get_tiktok_video(url)
        if not video_url:
            await status_msg.edit_text(f"ဗီဒီယို ဆွဲမရပါ။\nအကြောင်းရင်း: {err_detail}")
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
    
