import os
import re
import json
import threading
from io import BytesIO
from flask import Flask
from curl_cffi import requests as c_requests
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

def get_tiktok_video(raw_url):
    debug_logs = []
    session = c_requests.Session(impersonate="chrome120")

    # နည်းလမ်း ၁ - curl_cffi (Chrome TLS Impersonation) ဖြင့် TikWM 403 ကို ကျော်ခြင်း
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
                    if v_url.startswith("/"):
                        v_url = "https://www.tikwm.com" + v_url
                    return v_url, session, None
            else:
                debug_logs.append(f"TikWM: {data.get('msg')}")
        else:
            debug_logs.append(f"TikWM HTTP {r1.status_code}")
    except Exception as e:
        debug_logs.append(f"M1: {str(e)[:25]}")

    # နည်းလမ်း ၂ - TikTok Mobile Web Page မှ ဗီဒီယိုဖိုင်ကို တိုက်ရိုက်ဆွဲထုတ်ခြင်း (API မသုံးပါ)
    try:
        r2 = session.get(raw_url, allow_redirects=True, timeout=15)
        html = r2.text
        match = re.search(r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>', html)
        if match:
            js_data = json.loads(match.group(1))
            item_struct = (
                js_data.get("__DEFAULT_SCOPE__", {})
                .get("webapp.video-detail", {})
                .get("itemInfo", {})
                .get("itemStruct", {})
            )
            video_info = item_struct.get("video", {})
            v_url = video_info.get("playAddr") or video_info.get("downloadAddr")
            if not v_url and video_info.get("bitrateInfo"):
                addrs = video_info["bitrateInfo"][0].get("PlayAddr", {}).get("UrlList", [])
                if addrs:
                    v_url = addrs[0]
            if v_url:
                return v_url, session, None
            else:
                debug_logs.append("M2: No playAddr")
        else:
            debug_logs.append(f"M2: No script ({r2.status_code})")
    except Exception as e:
        debug_logs.append(f"M2: {str(e)[:25]}")

    # နည်းလမ်း ၃ - TikTok Embed Page မှ ဆွဲထုတ်ခြင်း
    try:
        vid_id_match = re.search(r'/video/(\d+)', r2.url if 'r2' in locals() else raw_url)
        if vid_id_match:
            vid_id = vid_id_match.group(1)
            r3 = session.get(f"https://www.tiktok.com/embed/v2/{vid_id}", timeout=15)
            m_play = re.findall(r'"playAddr":"([^"]+)"', r3.text)
            if m_play:
                v_url = m_play[0].encode('utf-8').decode('unicode_escape').replace("\\/", "/")
                return v_url, session, None
    except Exception as e:
        debug_logs.append(f"M3: {str(e)[:25]}")

    return None, None, " | ".join(debug_logs)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    match = re.search(r'(https?://[^\s]+tiktok\.com[^\s]*)', text)
    if not match:
        await update.message.reply_text("TikTok link ပို့ပေးပါ။")
        return

    url = match.group(1)
    status_msg = await update.message.reply_text("ဒေါင်းလုဒ်ဆွဲနေပါတယ် ခဏစောင့်ပါ...")

    try:
        video_url, session, err_detail = get_tiktok_video(url)
        if not video_url:
            await status_msg.edit_text(f"ဗီဒီယို ဆွဲမရပါ။\nအကြောင်းရင်း: {err_detail}")
            return

        # ဗီဒီယိုဖိုင်ကို Session Cookies ဖြင့် ဒေါင်းလုဒ်ဆွဲ၍ Telegram သို့ ပို့ပေးခြင်း
        vid_res = session.get(
            video_url,
            headers={"Referer": "https://www.tiktok.com/"},
            timeout=30
        )
        if vid_res.status_code == 200 and len(vid_res.content) > 10000:
            video_file = BytesIO(vid_res.content)
            video_file.name = "tiktok.mp4"
            await update.message.reply_video(
                video=video_file,
                caption="Watermark မပါသော ဗီဒီယို ရပါပြီ ✨"
            )
            await status_msg.delete()
        else:
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
        
