import html
import json
import os
import re
import threading
import time
import binascii
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse

import requests
import urllib3
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from Crypto.Cipher import AES

# تعطيل تحذيرات SSL
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BOT_TOKEN = "7808630939:AAEY0_q6vnkKlMRjvXNmEXwK1G80hv0vghY"
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "1013251619")
DATA_FILE = os.environ.get("DATA_FILE", "series.json")
CHECK_INTERVAL_SECONDS = int(os.environ.get("CHECK_INTERVAL_SECONDS", "180"))
SOURCE_DOMAINS = ["b2.shahidtv.net", "b1.shahidtv.net", "b3.shahidtv.net"]

API_URL = "https://arabfleex.live/api_bot.php"
SECRET_KEY = "ArabFleex_2024_SecRet"
MAX_WAIT_HOURS = 6

bot = telebot.TeleBot(BOT_TOKEN)

scan_lock = threading.Lock()
started_at = datetime.now(timezone.utc)
last_scan_at = None
scan_cycles = 0
total_added = 0
last_scan_result = "لم يبدأ فحص بعد"

def get_infinity_session(url):
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    })
    try:
        res = session.get(url, timeout=15, verify=False)
        if "toNumbers" in res.text and "slowAES.decrypt" in res.text:
            a_match = re.search(r'a=toNumbers\("([a-f0-9]+)"\)', res.text)
            b_match = re.search(r'b=toNumbers\("([a-f0-9]+)"\)', res.text)
            c_match = re.search(r'c=toNumbers\("([a-f0-9]+)"\)', res.text)
            
            if a_match and b_match and c_match:
                key = binascii.unhexlify(a_match.group(1))
                iv = binascii.unhexlify(b_match.group(1))
                cipher = AES.new(key, AES.MODE_CBC, iv)
                decrypted = cipher.decrypt(binascii.unhexlify(c_match.group(1)))
                cookie_val = binascii.hexlify(decrypted).decode('utf-8')
                
                parsed_url = urlparse(url)
                session.cookies.set('__test', cookie_val, domain=parsed_url.netloc, path='/')
    except Exception as e:
        pass
    return session

def load_series_data():
    if not os.path.exists(DATA_FILE): return {}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
        return data if isinstance(data, dict) else {}
    except: return {}

def save_series_data(data):
    parent = os.path.dirname(DATA_FILE)
    if parent: os.makedirs(parent, exist_ok=True)
    with open(f"{DATA_FILE}.tmp", "w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
    os.replace(f"{DATA_FILE}.tmp", DATA_FILE)

def candidate_urls_series(slug, season, episode, region):
    regions = list(dict.fromkeys([region, "EG", "LB", "SA", "SY", "MA", "TR"]))
    qualities = ["360p", "480p", "720p", "1080p"]
    episode_codes = [f"EP{episode:03d}", f"EP{episode:02d}"]
    suffixes = {q: [f"-{q}-v3.mp4", f"-{q}-v2.mp4", f"-{q}.mp4", f"-{q}-v1.mp4", f"-{q}-v4.mp4"] for q in qualities}
    for quality in qualities:
        for domain in SOURCE_DOMAINS:
            for item_region in regions:
                for ep_code in episode_codes:
                    for suffix in suffixes[quality]:
                        yield quality, f"https://{domain}/files/{item_region}/{slug}/{slug}-S{season:02d}-{ep_code}{suffix}"

def candidate_urls_wrestling(slug, date_str):
    qualities = ["360p", "480p", "720p", "1080p"]
    suffixes = {q: [f"-{q}-v3.mp4", f"-{q}-v2.mp4", f"-{q}.mp4", f"-{q}-v1.mp4", f"-{q}-v4.mp4"] for q in qualities}
    for quality in qualities:
        for domain in SOURCE_DOMAINS:
            for suffix in suffixes[quality]:
                yield quality, f"https://{domain}/files/wrestling/{slug}/{slug}-{date_str}{suffix}"

def check_link(url):
    try:
        response = requests.head(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5, verify=False)
        content_type = response.headers.get("Content-Type", "").lower()
        content_length = response.headers.get("Content-Length")
        if response.status_code != 200 or ("video/" not in content_type and "octet-stream" not in content_type): return False
        if content_length and content_length.isdigit() and int(content_length) < 100_000: return False
        return True
    except: return False

def send_to_site(series_id, title, episode_number, links_string):
    payload = {
        "secret_key": SECRET_KEY,
        "action": "insert",
        "series_id": series_id,
        "title": title,
        "episode_number": episode_number,
        "links_string": links_string
    }
    try:
        session = get_infinity_session(API_URL)
        res = session.post(API_URL, data=payload, timeout=20, verify=False)
        if "INSERTED" in res.text: return "تم الإضافة بنجاح ✅"
        if "UPDATED" in res.text: return "تم تحديث الجودات ✅"
        return f"خطأ: {res.text}"
    except Exception as e:
        return f"فشل الاتصال: {str(e)}"

def scan_item(slug, info):
    item_type = info.get("type", "series")
    links = {}

    if item_type == "wrestling":
        last_date_str = info.get("last_date", "2026-01-01")
        target_ep = int(info.get("last_ep", 0)) + 1
        date_obj = datetime.strptime(last_date_str, "%Y-%m-%d")
        next_date_str = (date_obj + timedelta(days=7)).strftime("%Y-%m-%d")
        for q, url in candidate_urls_wrestling(slug, next_date_str):
            if q not in links and check_link(url): links[q] = url
        display_title = next_date_str.replace("-", ".")
        target_id = f"{target_ep}_{next_date_str}"
    else:
        target_ep = int(info.get("last_ep", 0)) + 1
        season = int(info.get("season", 1))
        for q, url in candidate_urls_series(slug, season, target_ep, str(info.get("region", "EG")).upper()):
            if q not in links and check_link(url): links[q] = url
        display_title = f"الحلقة {target_ep}"
        target_id = str(target_ep)
        next_date_str = None

    if not links: return False

    series_id = info.get("series_id")
    found_keys = list(links.keys())
    formatted_links = [f"{q.replace('p', '')}|{links[q]}" for q in ["360p", "480p", "720p", "1080p"] if q in links]
    links_string = ",".join(formatted_links)
    
    current_track_id = info.get("track_id")
    state_changed = False

    # 1. اكتشاف مبدئي (لأول مرة نلاقي الحلقة)
    if current_track_id != target_id:
        api_status = send_to_site(series_id, display_title, target_ep, links_string) if series_id else "No ID"
        msg = f"🎬 <b>اكتشاف مبدئي:</b> {info.get('title', slug)}\n📺 <b>{display_title}</b>\n📶 <b>الجودات:</b> {len(links)}/4 ({', '.join(found_keys)})\n🌐 <b>الموقع:</b> {api_status}"
        bot.send_message(ADMIN_CHAT_ID, msg, parse_mode="HTML")
        
        if len(links) >= 4:
            info["last_ep"] = target_ep
            if item_type == "wrestling": info["last_date"] = next_date_str
            info.pop("track_id", None); info.pop("track_start", None); info.pop("track_qualities", None)
        else:
            info["track_id"] = target_id
            info["track_start"] = time.time()
            info["track_qualities"] = found_keys
        state_changed = True

    # 2. إحنا بنتابع الحلقة دي ومستنيين جودات زيادة
    else:
        tracked_qualities = info.get("track_qualities", [])
        new_qualities = [q for q in found_keys if q not in tracked_qualities]
        
        if new_qualities:
            api_status = send_to_site(series_id, display_title, target_ep, links_string) if series_id else "No ID"
            msg = f"🔄 <b>تحديث جودات:</b> {info.get('title', slug)}\n📺 <b>{display_title}</b>\n🆕 <b>تم إضافة:</b> {', '.join(new_qualities)}\n🌐 <b>الموقع:</b> {api_status}"
            bot.send_message(ADMIN_CHAT_ID, msg, parse_mode="HTML")
            info["track_qualities"] = found_keys
            state_changed = True
            
        # فحص الإغلاق (6 ساعات أو اكتمال الجودات)
        time_elapsed = time.time() - info.get("track_start", 0)
        if len(links) >= 4 or time_elapsed > (MAX_WAIT_HOURS * 3600):
            info["last_ep"] = target_ep
            if item_type == "wrestling": info["last_date"] = next_date_str
            info.pop("track_id", None); info.pop("track_start", None); info.pop("track_qualities", None)
            state_changed = True

    return state_changed

def scan_all_series_once():
    global last_scan_at, scan_cycles, total_added
    if not scan_lock.acquire(blocking=False): return []
    try:
        scan_cycles += 1
        last_scan_at = datetime.now(timezone.utc)
        data = load_series_data()
        results = []
        for slug, info in list(data.items()):
            if scan_item(slug, info):
                total_added += 1
                save_series_data(data)
                results.append(f"تحديث: {info.get('title', slug)}")
            time.sleep(2)
        return results or ["لا جديد"]
    except Exception as e:
        print(e)
    finally:
        scan_lock.release()

def auto_checker_loop():
    while True:
        scan_all_series_once()
        time.sleep(CHECK_INTERVAL_SECONDS)

# ===============================
# أوامر البوت
# ===============================
@bot.message_handler(commands=["start", "help"])
def welcome(message):
    bot.reply_to(message, "🤖 النظام الشامل الذكي (إضافة + تحديث جودات)\n/add | /del | /list | /backup | /restore | /setep | /setdate | /testapi")

@bot.message_handler(commands=["backup"])
def backup_data(message):
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "rb") as f: bot.send_document(message.chat.id, f, caption="✅ نسخة احتياطية")
    else: bot.reply_to(message, "⚠️ لا توجد بيانات.")

@bot.message_handler(commands=["restore"])
def restore_data_step(message):
    msg = bot.reply_to(message, "📥 انسخ محتوى الملف (النص) وابعته هنا في رسالة، أو ارفع الملف كـ Document:")
    bot.register_next_step_handler(msg, process_restore)

def process_restore(message):
    try:
        # فحص هل المستخدم بعت ملف ولا نص عادي
        if message.document:
            file_info = bot.get_file(message.document.file_id)
            downloaded_file = bot.download_file(file_info.file_path)
            raw_data = downloaded_file.decode('utf-8', errors='ignore')
        elif message.text:
            raw_data = message.text
        else:
            return bot.reply_to(message, "❌ لازم تبعت النص أو الملف.")

        parsed_data = json.loads(raw_data)
        save_series_data(parsed_data)
        bot.reply_to(message, "✅ تم استعادة البيانات بنجاح! تقدر تتأكد بأمر /list")
    except Exception as e: 
        bot.reply_to(message, f"❌ خطأ في قراءة البيانات: تأكد من نسخ الكود كاملاً.\n{str(e)}")

@bot.message_handler(commands=["add"])
def add_item_start(message):
    msg = bot.reply_to(message, "🔗 أرسل رابط الحلقة أو المصارعة:")
    bot.register_next_step_handler(msg, process_link_step)

def process_link_step(message):
    if message.text.startswith('/'): return
    link = message.text.strip()
    try:
        if "/wrestling/" in link.lower():
            slug = link.split('/')[5]
            date_str = re.search(r'-(\d{4}-\d{2}-\d{2})-', link).group(1)
            msg = bot.reply_to(message, f"✅ مصارعة\nالمعرف: {slug}\nالتاريخ: {date_str}\n📝 أرسل اسم العرض بالعربي:")
            bot.register_next_step_handler(msg, w_title_step, slug, date_str)
        else:
            parts = link.split('/')
            region, slug = parts[4], parts[5]
            match = re.search(r'-S(\d+)-EP(\d+)', link, re.IGNORECASE)
            season, episode = int(match.group(1)), int(match.group(2))
            msg = bot.reply_to(message, f"✅ مسلسل\nالمعرف: {slug}\nالموسم: {season}\nالحلقة: {episode}\n📝 أرسل اسم المسلسل بالعربي:")
            bot.register_next_step_handler(msg, s_title_step, slug, region, season, episode)
    except: bot.reply_to(message, "❌ خطأ في قراءة الرابط.")

def w_title_step(m, slug, date_str):
    title = m.text.strip()
    msg = bot.reply_to(m, "🔢 أرسل الـ ID في موقعك (رقم):")
    bot.register_next_step_handler(msg, w_id_step, slug, date_str, title)

def w_id_step(m, slug, date_str, title):
    series_id = int(m.text.strip())
    msg = bot.reply_to(m, "🔢 أرسل رقم الحلقة الحالي للعرض في موقعك (الرقم اللي هنزود عليه):")
    bot.register_next_step_handler(msg, w_save_step, slug, date_str, title, series_id)

def w_save_step(m, slug, date_str, title, series_id):
    last_ep = int(m.text.strip())
    data = load_series_data()
    data[slug] = {"type": "wrestling", "title": title, "last_date": date_str, "last_ep": last_ep, "series_id": series_id}
    save_series_data(data)
    bot.reply_to(m, "✅ تمت إضافة المصارعة بنجاح! سيتم البحث عن العرض القادم.")

def s_title_step(m, slug, region, season, episode):
    title = m.text.strip()
    msg = bot.reply_to(m, "🔢 أرسل الـ ID في موقعك (رقم):")
    bot.register_next_step_handler(msg, s_save_step, slug, region, season, episode, title)

def s_save_step(m, slug, region, season, episode, title):
    series_id = int(m.text.strip())
    data = load_series_data()
    data[slug] = {"type": "series", "title": title, "season": season, "last_ep": episode, "region": region, "series_id": series_id}
    save_series_data(data)
    bot.reply_to(m, f"✅ تمت إضافة المسلسل! سيبحث عن حلقة {episode + 1}")

@bot.message_handler(commands=["setep"])
def set_episode(message):
    try:
        slug, episode = message.text.split()[1:3]
        data = load_series_data()
        if slug in data:
            data[slug]["last_ep"] = int(episode)
            data[slug].pop("track_id", None)
            save_series_data(data)
            bot.reply_to(message, f"✅ تم تعديل الحلقة السابقة إلى: {episode}. سيبحث عن: {int(episode)+1}")
    except: bot.reply_to(message, "❌ الاستخدام الصحيح: /setep slug number")

@bot.message_handler(commands=["setdate"])
def set_date(message):
    try:
        slug, new_date = message.text.split()[1:3]
        data = load_series_data()
        if slug in data and data[slug].get("type") == "wrestling":
            data[slug]["last_date"] = new_date
            save_series_data(data)
            bot.reply_to(message, f"✅ تم تعديل تاريخ آخر عرض إلى: {new_date}")
    except: pass

@bot.message_handler(commands=["list"])
def list_items(message):
    data = load_series_data()
    if not data: return bot.reply_to(message, "📭 القائمة فارغة.")
    lines = ["📺 القائمة الحالية:"]
    for slug, info in data.items():
        state = "⏳ يجمع جودات" if "track_id" in info else "✅ مكتمل"
        if info.get("type") == "wrestling":
            lines.append(f"🥊 {info.get('title')} | آخر حلقة: {info.get('last_ep')} ({info.get('last_date')}) | {state}")
        else:
            lines.append(f"🎬 {info.get('title')} | آخر حلقة: {info.get('last_ep')} | {state}")
    bot.reply_to(message, "\n".join(lines))

@bot.message_handler(commands=["del"])
def delete_item(message):
    data = load_series_data()
    if not data: return bot.reply_to(message, "القائمة فارغة.")
    markup = InlineKeyboardMarkup(row_width=1)
    for slug, info in data.items(): markup.add(InlineKeyboardButton(text=f"❌ {info.get('title')}", callback_data=f"del_{slug}"))
    bot.reply_to(message, "اختر للحذف:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith('del_'))
def process_delete(call):
    slug = call.data.split('del_')[1]
    data = load_series_data()
    if slug in data:
        del data[slug]
        save_series_data(data)
        bot.edit_message_text("✅ تم الحذف بنجاح.", call.message.chat.id, call.message.message_id)

@bot.message_handler(commands=["testapi"])
def test_api(message):
    try:
        slug = message.text.split()[1]
        res = send_to_site(999, f"Test-{slug}", 999, "360|https://test.mp4")
        bot.reply_to(message, f"نتيجة الاختبار:\n{res}")
    except: bot.reply_to(message, "❌ اكتب /testapi واسم للتجربة")

if __name__ == "__main__":
    threading.Thread(target=auto_checker_loop, daemon=True).start()
    print("Bot is running with Smart Quality Tracking...", flush=True)
    bot.infinity_polling()
