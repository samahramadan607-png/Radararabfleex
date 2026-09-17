import html
import json
import os
import re
import threading
import time
import binascii
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse, quote

import requests
from curl_cffi import requests as curl_requests 
import urllib3
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from Crypto.Cipher import AES

# تعطيل تحذيرات SSL
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BOT_TOKEN = "7808630939:AAEY0_q6vnkKlMRjvXNmEXwK1G80hv0vghY"
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "1013251619")
DATA_FILE = os.environ.get("DATA_FILE", "series.json")
# تم التعديل ليصبح الفحص كل 5 دقائق (300 ثانية) لتخفيف الضغط تماماً
CHECK_INTERVAL_SECONDS = int(os.environ.get("CHECK_INTERVAL_SECONDS", "300"))
SOURCE_DOMAINS = ["b2.shahidtv.net", "b1.shahidtv.net", "b3.shahidtv.net"]

API_URL = "https://arabfleex.live/api_bot.php"
SECRET_KEY = "ArabFleex_2024_SecRet"

# قائمة حسابات Cloudflare Workers الـ 3 الجديدة لتوزيع ضغط الفحص
CF_WORKERS = [
    "https://jolly-term-f45d.afu6656gu.workers.dev/?url=",
    "https://shy-snow-52c3.alifalah9988044.workers.dev/?url=",
    "https://young-glade-3a0e.sspw9f88.workers.dev/?url="
]

bot = telebot.TeleBot(BOT_TOKEN)

scan_lock = threading.Lock()
started_at = datetime.now(timezone.utc)
last_scan_at = None
scan_cycles = 0
total_added = 0
last_scan_result = "لم يبدأ فحص بعد"

# ==========================================
# دالة تخطي حماية InfinityFree
# ==========================================
def get_infinity_session(url):
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
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
        print(f"[ERROR] Infinity Session: {e}", flush=True)
    return session

def load_series_data():
    if not os.path.exists(DATA_FILE):
        return {}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError) as error:
        print(f"Error loading series data: {error}", flush=True)
        return {}

def save_series_data(data):
    parent = os.path.dirname(DATA_FILE)
    if parent:
        os.makedirs(parent, exist_ok=True)
    temporary_file = f"{DATA_FILE}.tmp"
    with open(temporary_file, "w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
    os.replace(temporary_file, DATA_FILE)

# ==========================================
# توليد الروابط للمسلسلات
# ==========================================
def candidate_urls_series(slug, season, episode, region):
    regions = list(dict.fromkeys([region, "EG", "LB", "SA", "SY", "MA"]))
    qualities = ["360p", "480p", "720p", "1080p"]
    episode_codes = [f"EP{episode:03d}", f"EP{episode:02d}"]
    suffixes = {
        q: [f"-{q}-v3.mp4", f"-{q}-v2.mp4", f"-{q}.mp4", f"-{q}-v1.mp4", f"-{q}-v4.mp4"] for q in qualities
    }
    for quality in qualities:
        for domain in SOURCE_DOMAINS:
            for item_region in regions:
                for episode_code in episode_codes:
                    for suffix in suffixes[quality]:
                        yield quality, f"https://{domain}/files/{item_region}/{slug}/{slug}-S{season:02d}-{episode_code}{suffix}"

# ==========================================
# توليد الروابط لعروض المصارعة
# ==========================================
def candidate_urls_wrestling(slug, date_str):
    qualities = ["360p", "480p", "720p", "1080p"]
    suffixes = {
        q: [f"-{q}-v3.mp4", f"-{q}-v2.mp4", f"-{q}.mp4", f"-{q}-v1.mp4", f"-{q}-v4.mp4"] for q in qualities
    }
    for quality in qualities:
        for domain in SOURCE_DOMAINS:
            for suffix in suffixes[quality]:
                yield quality, f"https://{domain}/files/wrestling/{slug}/{slug}-{date_str}{suffix}"

# ==========================================
# توليد روابط "الكشاف الذكي" (480p فقط على سيرفر b2)
# ==========================================
def probe_urls_series(slug, season, episode, region):
    domain = "b2.shahidtv.net"
    regions = list(dict.fromkeys([region, "EG"]))
    episode_codes = [f"EP{episode:03d}", f"EP{episode:02d}"]
    suffixes = ["-480p.mp4", "-480p-v2.mp4"]
    for r in regions:
        for ep_code in episode_codes:
            for suffix in suffixes:
                yield f"https://{domain}/files/{r}/{slug}/{slug}-S{season:02d}-{ep_code}{suffix}"

def probe_urls_wrestling(slug, date_str):
    domain = "b2.shahidtv.net"
    suffixes = ["-480p.mp4", "-480p-v2.mp4"]
    for suffix in suffixes:
        yield f"https://{domain}/files/wrestling/{slug}/{slug}-{date_str}{suffix}"

# ==========================================
# فحص الروابط عبر Cloudflare Workers
# ==========================================
def check_link(original_url):
    import random
    try:
        # اختيار Worker عشوائي لتوزيع الضغط
        worker = random.choice(CF_WORKERS)
        test_url = f"{worker}{quote(original_url, safe='')}"
        
        response = curl_requests.get(
            test_url,
            impersonate="chrome",
            timeout=10,
            stream=True,
            verify=False
        )
        content_type = response.headers.get("Content-Type", "").lower()
        content_length = response.headers.get("Content-Length")
        
        valid_types = ["video/", "application/octet-stream", "application/force-download", "application/x-download"]
        has_video_type = not content_type or any(t in content_type for t in valid_types)
        
        if response.status_code != 200 or not has_video_type: 
            return False
            
        if content_length and content_length.isdigit() and int(content_length) < 100_000: 
            return False
            
        return True
    except Exception as e:
        return False

# ==========================================
# دالة حساب وقت الفحص (النوم الذكي)
# ==========================================
def is_time_to_scan(info):
    release_time_str = info.get("release_time")
    # لو مفيش وقت متسجل (ملف الباك أب القديم)، افحص عادي
    if not release_time_str: return True 
    
    try:
        release_time_str = release_time_str.strip().upper()
        if ":" in release_time_str:
            dt = datetime.strptime(release_time_str, "%I:%M %p")
        else:
            dt = datetime.strptime(release_time_str, "%I %p")
    except ValueError:
        return True # لو صيغة الوقت مكتوبة غلط، افحص احتياطي

    # تحديد توقيت مصر (UTC+3)
    egypt_tz = timezone(timedelta(hours=3))
    now = datetime.now(egypt_tz)
    
    now_mins = now.hour * 60 + now.minute
    target_mins = dt.hour * 60 + dt.minute
    
    # حساب الفرق للتعامل السلس لو الوقت كان بعد منتصف الليل
    diff = now_mins - target_mins
    if diff < -720: diff += 1440
    elif diff > 720: diff -= 1440
    
    # البوت هيصحى قبل الميعاد بـ 60 دقيقة، ويفضل صاحي للمسلسل ده لمدة 10 ساعات كحد أقصى لو اتأخر
    if -60 <= diff <= 600:
        return True
        
    return False

# ==========================================
# عملية الفحص الأساسية (باستخدام الكشاف)
# ==========================================
def scan_item(slug, info):
    global last_scan_result
    
    # التيك تشيك بتاع موعد النزول أول حاجة
    if not is_time_to_scan(info):
        last_scan_result = f"{info.get('title', slug)}: خارج موعد النزول (في وضع النوم 💤)"
        return False
        
    item_type = info.get("type", "series")
    links = {}
    attempts = 0
    probe_found = False

    if item_type == "wrestling":
        last_date_str = info.get("last_date", "2026-01-01")
        last_ep = int(info.get("last_ep", 0))
        date_obj = datetime.strptime(last_date_str, "%Y-%m-%d")
        next_date_obj = date_obj + timedelta(days=7)
        target_date_to_scan = next_date_obj.strftime("%Y-%m-%d")
        target_episode = last_ep + 1
        
        # إرسال الكشاف أولاً
        for url in probe_urls_wrestling(slug, target_date_to_scan):
            attempts += 1
            if check_link(url):
                probe_found = True
                break
                
        # لو الكشاف لقى الحلقة، نبدأ الفحص الشامل
        if probe_found:
            for quality, url in candidate_urls_wrestling(slug, target_date_to_scan):
                if quality in links: continue
                attempts += 1
                if check_link(url):
                    links[quality] = url
                
        display_title = target_date_to_scan.replace("-", ".")
    else:
        target_episode = int(info.get("last_ep", 0)) + 1
        season = int(info.get("season", 1))
        
        # إرسال الكشاف أولاً
        for url in probe_urls_series(slug, season, target_episode, str(info.get("region", "EG")).upper()):
            attempts += 1
            if check_link(url):
                probe_found = True
                break
                
        # لو الكشاف لقى الحلقة، نبدأ الفحص الشامل
        if probe_found:
            for quality, url in candidate_urls_series(slug, season, target_episode, str(info.get("region", "EG")).upper()):
                if quality in links: continue
                attempts += 1
                if check_link(url):
                    links[quality] = url
                
        display_title = f"الحلقة {target_episode}"
        target_date_to_scan = None

    if not links:
        last_scan_result = f"{info.get('title', slug)}: الفحص لم يجد جديد (تم فحص {attempts} رابط)"
        return False

    title = info.get("title", slug)
    series_id = info.get("series_id")
    found_keys = list(links.keys())
    
    # نرسل الروابط المباشرة للموقع (لأن play.php أصبح يضيف الـ Worker تلقائياً)
    formatted_links = [f"{q.replace('p', '')}|{links[q]}" for q in ["360p", "480p", "720p", "1080p"] if q in links]
    links_string = ",".join(formatted_links)
    
    api_status = "لم يتم تحديد ID"
    if series_id:
        payload = {
            "secret_key": SECRET_KEY, "action": "insert", "series_id": series_id,
            "title": display_title, "episode_number": target_episode, "links_string": links_string
        }
        try:
            session = get_infinity_session(API_URL)
            res = session.post(API_URL, data=payload, timeout=20, verify=False)
            if "INSERTED" in res.text: api_status = "تمت الإضافة للموقع بنجاح ✅"
            elif "already exists" in res.text: api_status = "موجودة مسبقاً ⚠️"
            else: api_status = f"خطأ: {res.text}"
        except Exception as e: api_status = f"فشل الاتصال: {e}"

    msg = (
        f"🎬 <b>تم اصطياد وإضافة جديد:</b> {title}\n"
        f"📺 <b>{display_title}</b>\n"
        f"📶 <b>الجودات اللي نزلت:</b> {len(links)}/4 ({', '.join(found_keys)})\n"
        f"🌐 <b>الموقع:</b> {api_status}\n\n"
        f"✅ <i>تم قفل الحلقة والانتقال للبحث عن الحلقة القادمة...</i>"
    )
    bot.send_message(ADMIN_CHAT_ID, msg, parse_mode="HTML")
    
    # التحديث الفوري عشان يتخطى الحلقة ويدخل على اللي بعدها فوراً
    info["last_ep"] = target_episode
    if item_type == "wrestling": 
        info["last_date"] = target_date_to_scan
        
    # تنظيف أي بيانات تتبع قديمة كانت محفوظة
    info.pop("track_id", None)
    info.pop("track_start", None)
    info.pop("track_qualities", None)

    return True

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
                results.append(f"{info.get('title', slug)}: تم التحديث ✅")
            else:
                results.append(last_scan_result)
            time.sleep(2)
        return results
    finally:
        scan_lock.release()

def auto_checker_loop():
    while True:
        try: scan_all_series_once()
        except Exception as error: print(f"[ERROR] Checker loop: {error}", flush=True)
        time.sleep(CHECK_INTERVAL_SECONDS)

def format_duration(total_seconds):
    seconds = max(0, int(total_seconds))
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f"{hours}س {minutes}د {seconds}ث"

def status_message():
    uptime = (datetime.now(timezone.utc) - started_at).total_seconds()
    data = load_series_data()
    lines = [
        "✅ <b>البوت شغّال وبيفحص بانتظام!</b>\n",
        f"⏱ <b>وقت التشغيل:</b> {format_duration(uptime)}",
        f"🔍 <b>آخر فحص:</b> {last_scan_at.astimezone().strftime('%Y-%m-%d %H:%M:%S') if last_scan_at else 'لم يبدأ'}",
        f"🔄 <b>دورات الفحص:</b> {scan_cycles} | ➕ <b>الإشعارات:</b> {total_added}\n",
        "📺 <b>آخر حالة:</b>",
    ]
    if not data:
        lines.append("  لا توجد عناصر مضافة.")
    else:
        for slug, info in data.items():
            title = html.escape(str(info.get("title", slug)))
            series_id = info.get("series_id", "❌")
            rel_time = info.get("release_time", "طوال اليوم")
            state = "✅ مستعد"
            
            if info.get("type") == "wrestling":
                lines.append(f"  🥊 <b>{title}</b> (ID: {series_id}): آخر عرض {info.get('last_date')} | ⏱ {rel_time} | {state}")
            else:
                lines.append(f"  🎬 <b>{title}</b> (ID: {series_id}): حلقة {info.get('last_ep', 0)} | ⏱ {rel_time} | {state}")
    return "\n".join(lines)

def admin_only(message):
    return str(message.chat.id) == str(ADMIN_CHAT_ID)

@bot.message_handler(commands=["start", "help"])
def welcome(message):
    if admin_only(message):
        bot.reply_to(message, "🤖 <b>نظام المراقبة (سريع + دعم Worker)</b>\n\n🔹 <code>/add</code> — إضافة جديد\n🔹 <code>/del</code> — حذف\n🔹 <code>/list</code> — قائمة\n🔹 <code>/setep</code> — تعديل حلقة\n🔹 <code>/setdate</code> — تعديل تاريخ\n🔹 <code>/settime</code> — تحديد موعد النزول ⏱\n🔹 <code>/check</code> — الحالة\n🔹 <code>/scan</code> — فحص يدوي\n🔹 <code>/test</code> — فحص رابط", parse_mode="HTML")

@bot.message_handler(commands=["backup"])
def backup_data(message):
    if not admin_only(message): return
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "rb") as f:
            bot.send_document(message.chat.id, f, caption="✅ نسخة احتياطية (series.json)")
    else:
        bot.reply_to(message, "⚠️ لا توجد بيانات للنسخ.")

@bot.message_handler(commands=["restore"])
def restore_data_step(message):
    if not admin_only(message): return
    msg = bot.reply_to(message, "📥 <b>أرسل لي ملف series.json كرسالة (Document) أو نص:</b>", parse_mode="HTML")
    bot.register_next_step_handler(msg, process_restore)

def process_restore(message):
    if not admin_only(message): return
    raw_data = ""
    try:
        if message.document:
            file_info = bot.get_file(message.document.file_id)
            downloaded_file = bot.download_file(file_info.file_path)
            raw_data = downloaded_file.decode('utf-8')
        elif message.text:
            raw_data = message.text
        else:
            bot.reply_to(message, "❌ ملف أو نص غير صالح.")
            return
        parsed_data = json.loads(raw_data)
        save_series_data(parsed_data)
        bot.reply_to(message, "✅ <b>تمت الاستعادة بنجاح!</b>", parse_mode="HTML")
    except Exception as e:
        bot.reply_to(message, f"❌ خطأ: {e}")

@bot.message_handler(commands=["add"])
def add_item_start(message):
    if not admin_only(message): return
    msg = bot.reply_to(message, "🔗 <b>أرسل رابط الحلقة أو العرض:</b>", parse_mode="HTML")
    bot.register_next_step_handler(msg, process_link_step)

def process_link_step(message):
    if message.text.startswith('/'): return
    link = message.text.strip()
    try:
        if "/wrestling/" in link.lower():
            parts = link.split('/')
            slug = parts[5]
            filename = parts[-1]
            match = re.search(r'-(\d{4}-\d{2}-\d{2})-', filename)
            if match:
                date_str = match.group(1)
                msg = bot.reply_to(message, f"✅ تم اكتشاف عرض مصارعة!\nالمعرف: <code>{slug}</code>\nالتاريخ: <code>{date_str}</code>\n\n📝 أرسل اسم العرض:", parse_mode="HTML")
                bot.register_next_step_handler(msg, w_title_step, slug, date_str)
            else:
                bot.reply_to(message, "❌ لم يتم العثور على التاريخ في الرابط.")
        else:
            parts = link.split('/')
            region = parts[4]
            slug = parts[5]
            filename = parts[-1]
            match = re.search(r'-S(\d+)-EP(\d+)', filename, re.IGNORECASE)
            if match:
                season = int(match.group(1))
                episode = int(match.group(2))
                msg = bot.reply_to(message, f"✅ تم اكتشاف مسلسل!\nالمعرف: <code>{slug}</code>\nالموسم: {season}\nالحلقة: {episode}\n\n📝 أرسل اسم المسلسل:", parse_mode="HTML")
                bot.register_next_step_handler(msg, s_title_step, slug, region, season, episode)
            else:
                bot.reply_to(message, "❌ لم يتم العثور على S-EP في الرابط.")
    except Exception as e:
        bot.reply_to(message, f"❌ خطأ: {e}")

def w_title_step(message, slug, date_str):
    if message.text.startswith('/'): return
    title = message.text.strip()
    msg = bot.reply_to(message, "🔢 أرسل ID العرض في موقعك:", parse_mode="HTML")
    bot.register_next_step_handler(msg, w_id_step, slug, date_str, title)

def w_id_step(message, slug, date_str, title):
    if message.text.startswith('/'): return
    try: series_id = int(message.text.strip())
    except ValueError: return bot.reply_to(message, "❌ يجب أن يكون رقماً.")
    msg = bot.reply_to(message, "🔢 أرسل رقم الحلقة الحالي (لو جديد اكتب 0):", parse_mode="HTML")
    bot.register_next_step_handler(msg, w_save_step, slug, date_str, title, series_id)

def w_save_step(message, slug, date_str, title, series_id):
    if message.text.startswith('/'): return
    try: last_ep = int(message.text.strip())
    except ValueError: return bot.reply_to(message, "❌ يجب أن يكون رقماً.")
    data = load_series_data()
    data[slug] = {"type": "wrestling", "title": title, "last_date": date_str, "last_ep": last_ep, "series_id": series_id}
    save_series_data(data)
    bot.reply_to(message, "✅ تمت إضافة المصارعة بنجاح!", parse_mode="HTML")

def s_title_step(message, slug, region, season, episode):
    if message.text.startswith('/'): return
    title = message.text.strip()
    msg = bot.reply_to(message, "🔢 أرسل ID المسلسل في موقعك:", parse_mode="HTML")
    bot.register_next_step_handler(msg, s_save_step, slug, region, season, episode, title)

def s_save_step(message, slug, region, season, episode, title):
    if message.text.startswith('/'): return
    try: series_id = int(message.text.strip())
    except ValueError: return bot.reply_to(message, "❌ يجب أن يكون رقماً.")
    data = load_series_data()
    data[slug] = {"type": "series", "title": title, "season": season, "last_ep": episode, "region": region, "series_id": series_id}
    save_series_data(data)
    bot.reply_to(message, "✅ تمت إضافة المسلسل بنجاح!", parse_mode="HTML")

@bot.message_handler(commands=["setep"])
def set_episode(message):
    if not admin_only(message): return
    try:
        slug, episode = message.text.split()[1:3]
        data = load_series_data()
        if slug in data:
            data[slug]["last_ep"] = int(episode)
            save_series_data(data)
            bot.reply_to(message, f"✅ تم التعديل إلى حلقة: {episode}", parse_mode="HTML")
    except: bot.reply_to(message, "❌ الصيغة: /setep slug number")

@bot.message_handler(commands=["setdate"])
def set_date(message):
    if not admin_only(message): return
    try:
        slug, new_date = message.text.split()[1:3]
        data = load_series_data()
        if slug in data and data[slug].get("type") == "wrestling":
            data[slug]["last_date"] = new_date
            save_series_data(data)
            bot.reply_to(message, f"✅ تم التعديل إلى تاريخ: {new_date}", parse_mode="HTML")
    except: bot.reply_to(message, "❌ الصيغة: /setdate slug YYYY-MM-DD")

@bot.message_handler(commands=["settime"])
def set_release_time(message):
    if not admin_only(message): return
    try:
        parts = message.text.split(" ", 2)
        slug = parts[1]
        time_str = parts[2]
        
        # تجربة قراءة الوقت للتأكد من صحة الصيغة
        time_str_upper = time_str.strip().upper()
        if ":" in time_str_upper: datetime.strptime(time_str_upper, "%I:%M %p")
        else: datetime.strptime(time_str_upper, "%I %p")
        
        data = load_series_data()
        if slug in data:
            data[slug]["release_time"] = time_str_upper
            save_series_data(data)
            bot.reply_to(message, f"✅ تم تحديد موعد نزول [{slug}] الساعة: {time_str_upper}\n💤 البوت هينام ويصحى يراقبه قبل الميعاد بساعة.", parse_mode="HTML")
        else:
            bot.reply_to(message, "❌ المسلسل غير موجود.")
    except ValueError:
        bot.reply_to(message, "❌ صيغة الوقت خطأ. استخدم صيغة 12 ساعة (مثال: 8:00 PM أو 10 AM)")
    except Exception as e:
        bot.reply_to(message, "❌ الصيغة: /settime slug 8:00 PM")

@bot.message_handler(commands=["list"])
def list_items(message):
    if not admin_only(message): return
    bot.reply_to(message, status_message(), parse_mode="HTML")

@bot.message_handler(commands=["del"])
def delete_item(message):
    if not admin_only(message): return
    data = load_series_data()
    markup = InlineKeyboardMarkup(row_width=1)
    for slug, info in data.items():
        markup.add(InlineKeyboardButton(text=f"❌ حذف: {info.get('title', slug)}", callback_data=f"del_{slug}"))
    if not data: return bot.reply_to(message, "📭 القائمة فارغة.")
    bot.reply_to(message, "🗑 اختر للحذف:", reply_markup=markup, parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data.startswith('del_'))
def process_delete_callback(call):
    slug = call.data.split('del_')[1]
    data = load_series_data()
    if slug in data:
        del data[slug]
        save_series_data(data)
        bot.answer_callback_query(call.id, "تم الحذف بنجاح!")
        bot.edit_message_text("✅ تم الحذف.", call.message.chat.id, call.message.message_id)

@bot.message_handler(commands=["check", "status"])
def status(message):
    if admin_only(message): bot.reply_to(message, status_message(), parse_mode="HTML")

@bot.message_handler(commands=["scan"])
def force_check(message):
    if not admin_only(message): return
    if not scan_lock.acquire(blocking=False): return bot.reply_to(message, "⏳ يوجد فحص جارٍ...")
    scan_lock.release()
    bot.reply_to(message, "🔎 <b>بدأ الفحص السريع عبر الـ Worker...</b>", parse_mode="HTML")
    threading.Thread(target=lambda: bot.send_message(ADMIN_CHAT_ID, "✅ <b>انتهى الفحص!</b>\n\n" + ("\n".join(scan_all_series_once()) or "📭 فارغ."), parse_mode="HTML"), daemon=True).start()

@bot.message_handler(commands=["test"])
def test_link_cmd(message):
    if not admin_only(message): return
    try:
        import random
        url = message.text.split()[1]
        worker = random.choice(CF_WORKERS)
        msg_wait = bot.reply_to(message, f"🔄 جاري فحص الرابط عبر Worker عشوائي...\n`{worker}`", parse_mode="Markdown")
        test_url = f"{worker}{quote(url, safe='')}"
        
        response = curl_requests.get(
            test_url,
            impersonate="chrome",
            timeout=10,
            stream=True,
            verify=False
        )
        status = response.status_code
        c_type = response.headers.get("Content-Type", "غير معروف")
        
        msg = f"📊 **نتيجة الفحص عبر الـ Worker:**\nStatus Code: `{status}`\nContent-Type: `{c_type}`"
        if status == 200:
            msg += "\n\n✅ **نجاح تام! الـ Worker تخطى الحظر وقرأ الملف بنجاح!**"
        else:
            msg += f"\n\n⚠️ رد بـ {status}"
        bot.edit_message_text(msg, message.chat.id, msg_wait.message_id, parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(message, f"❌ خطأ: {e}")

if __name__ == "__main__":
    threading.Thread(target=auto_checker_loop, daemon=True).start()
    print("Bot is running with Fast Mode + CF Worker Download Proxy...", flush=True)
    bot.infinity_polling()
