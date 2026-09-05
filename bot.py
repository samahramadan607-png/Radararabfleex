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

def check_link(url):
    try:
        response = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            timeout=8,
            stream=True,
            verify=False
        )
        content_type = response.headers.get("Content-Type", "").lower()
        content_length = response.headers.get("Content-Length")
        has_video_type = (not content_type or "video/" in content_type or "application/octet-stream" in content_type)
        
        if response.status_code != 200 or not has_video_type: return False
        if content_length and content_length.isdigit() and int(content_length) < 100_000: return False
        return True
    except requests.RequestException:
        return False

# ==========================================
# عملية الفحص الأساسية (للمسلسلات والمصارعة)
# ==========================================
def scan_item(slug, info):
    global last_scan_result
    item_type = info.get("type", "series")
    links = {}
    attempts = 0

    if item_type == "wrestling":
        last_date_str = info.get("last_date", "2026-01-01")
        last_ep = int(info.get("last_ep", 0))
        date_obj = datetime.strptime(last_date_str, "%Y-%m-%d")
        next_date_obj = date_obj + timedelta(days=7)
        next_date_str = next_date_obj.strftime("%Y-%m-%d")
        target_episode = last_ep + 1
        
        for quality, url in candidate_urls_wrestling(slug, next_date_str):
            if quality in links: continue
            attempts += 1
            if check_link(url):
                links[quality] = url
    else:
        # مسلسلات
        target_episode = int(info.get("last_ep", 0)) + 1
        season = int(info.get("season", 1))
        for quality, url in candidate_urls_series(slug, season, target_episode, str(info.get("region", "EG")).upper()):
            if quality in links: continue
            attempts += 1
            if check_link(url):
                links[quality] = url

    if not links:
        last_scan_result = f"{info.get('title', slug)}: الفحص لم يجد جديد (تم فحص {attempts} رابط)"
        return None

    # بناء الروابط للإرسال
    formatted_links = [f"{q.replace('p', '')}|{links[q]}" for q in ["360p", "480p", "720p", "1080p"] if q in links]
    links_string = ",".join(formatted_links)
    
    title = info.get("title", slug)
    series_id = info.get("series_id")
    api_status = "لم يتم تحديد ID للمسلسل"

    if series_id:
        if item_type == "wrestling":
            display_title = next_date_str.replace("-", ".")
        else:
            display_title = f"الحلقة {target_episode}"

        payload = {
            "secret_key": SECRET_KEY,
            "action": "insert",
            "series_id": series_id,
            "title": display_title,
            "episode_number": target_episode,
            "links_string": links_string
        }
        
        try:
            session = get_infinity_session(API_URL)
            res = session.post(API_URL, data=payload, timeout=20, verify=False)
            if "INSERTED" in res.text:
                api_status = "تمت الإضافة للموقع بنجاح ✅"
            else:
                api_status = f"خطأ في الإضافة: {res.text}"
        except Exception as e:
            api_status = f"فشل الاتصال: {str(e)}"

    final_message = (
        f"📺 <b>العنوان:</b> {title}\n"
        f"🎬 <b>الإضافة الجديدة:</b> {display_title}\n"
        f"🌐 <b>حالة الموقع:</b> {api_status}\n\n"
        f"<code>{links_string}</code>"
    )

    bot.send_message(ADMIN_CHAT_ID, final_message, disable_web_page_preview=True, parse_mode="HTML")
    last_scan_result = f"{info.get('title', slug)}: تم إيجاد محتوى جديد وإرسال التنبيه ✅"
    
    return {"ep": target_episode, "date": next_date_str if item_type == "wrestling" else None}

def scan_all_series_once():
    global last_scan_at, scan_cycles, total_added
    if not scan_lock.acquire(blocking=False): return []
    try:
        scan_cycles += 1
        last_scan_at = datetime.now(timezone.utc)
        data = load_series_data()
        results = []
        for slug, info in list(data.items()):
            found = scan_item(slug, info)
            if found:
                total_added += 1
                data[slug]["last_ep"] = found["ep"]
                if found["date"]:
                    data[slug]["last_date"] = found["date"]
                save_series_data(data)
                results.append(f"{info.get('title', slug)}: تم التحديث بنجاح ✅")
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
        f"🔄 <b>دورات الفحص:</b> {scan_cycles} | ➕ <b>النجاح:</b> {total_added}\n",
        "📺 <b>آخر حالة:</b>",
    ]
    if not data:
        lines.append("  لا توجد عناصر مضافة.")
    else:
        for slug, info in data.items():
            title = html.escape(str(info.get("title", slug)))
            series_id = info.get("series_id", "❌")
            if info.get("type") == "wrestling":
                lines.append(f"  🥊 <b>{title}</b> (ID: {series_id}): آخر عرض {info.get('last_date')} (ح{info.get('last_ep')})")
            else:
                lines.append(f"  🎬 <b>{title}</b> (ID: {series_id}): حلقة {info.get('last_ep', 0)}")
    return "\n".join(lines)

def admin_only(message):
    return str(message.chat.id) == str(ADMIN_CHAT_ID)

@bot.message_handler(commands=["start", "help"])
def welcome(message):
    if admin_only(message):
        bot.reply_to(message, "🤖 <b>نظام المراقبة (مسلسلات ومصارعة)</b>\n\n🔹 <code>/add</code> — إضافة جديد\n🔹 <code>/del</code> — حذف\n🔹 <code>/list</code> — قائمة\n🔹 <code>/setep</code> — تعديل حلقة مسلسل\n🔹 <code>/setdate</code> — تعديل تاريخ مصارعة\n🔹 <code>/check</code> — الحالة\n🔹 <code>/scan</code> — فحص يدوي\n🔹 <code>/backup</code> — أخذ نسخة\n🔹 <code>/restore</code> — استعادة نسخة", parse_mode="HTML")

# ==========================================
# النسخ الاحتياطي والاستعادة
# ==========================================
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
    msg = bot.reply_to(message, "📥 <b>أرسل لي ملف series.json كرسالة (Document) الآن:</b>", parse_mode="HTML")
    bot.register_next_step_handler(msg, process_restore)

def process_restore(message):
    if not admin_only(message): return
    if not message.document:
        bot.reply_to(message, "❌ هذا ليس ملفاً. حاول مرة أخرى.")
        return
    try:
        file_info = bot.get_file(message.document.file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        json.loads(downloaded_file.decode('utf-8')) # للتأكد إنه ملف سليم
        with open(DATA_FILE, 'wb') as new_file:
            new_file.write(downloaded_file)
        bot.reply_to(message, "✅ <b>تم استعادة البيانات بنجاح!</b>", parse_mode="HTML")
    except Exception as e:
        bot.reply_to(message, f"❌ حدث خطأ أثناء الاستعادة: {e}")

# ==========================================
# نظام الإضافة الذكي (مسلسلات / مصارعة)
# ==========================================
@bot.message_handler(commands=["add"])
def add_item_start(message):
    if not admin_only(message): return
    msg = bot.reply_to(message, "🔗 <b>أرسل لي رابط الحلقة أو عرض المصارعة:</b>", parse_mode="HTML")
    bot.register_next_step_handler(msg, process_link_step)

def process_link_step(message):
    if message.text.startswith('/'): return
    link = message.text.strip()
    
    try:
        if "/wrestling/" in link.lower():
            # رابط مصارعة
            parts = link.split('/')
            slug = parts[5]
            filename = parts[-1]
            match = re.search(r'-(\d{4}-\d{2}-\d{2})-', filename)
            if match:
                date_str = match.group(1)
                msg = bot.reply_to(message, f"✅ تم اكتشاف <b>عرض مصارعة</b>!\nالمعرف: <code>{slug}</code>\nالتاريخ: <code>{date_str}</code>\n\n📝 أرسل <b>اسم العرض</b>:", parse_mode="HTML")
                bot.register_next_step_handler(msg, w_title_step, slug, date_str)
            else:
                bot.reply_to(message, "❌ لم يتم العثور على تاريخ العرض في الرابط (YYYY-MM-DD).")
        else:
            # رابط مسلسل
            parts = link.split('/')
            region = parts[4]
            slug = parts[5]
            filename = parts[-1]
            match = re.search(r'-S(\d+)-EP(\d+)', filename, re.IGNORECASE)
            if match:
                season = int(match.group(1))
                episode = int(match.group(2))
                msg = bot.reply_to(message, f"✅ تم اكتشاف <b>مسلسل</b>!\nالمعرف: <code>{slug}</code>\nالموسم: <code>{season}</code>\nالحلقة: <code>{episode}</code>\n\n📝 أرسل <b>اسم المسلسل</b>:", parse_mode="HTML")
                bot.register_next_step_handler(msg, s_title_step, slug, region, season, episode)
            else:
                bot.reply_to(message, "❌ لم يتم العثور على S01-EP01 في الرابط.")
    except Exception as e:
        bot.reply_to(message, f"❌ خطأ في قراءة الرابط: {e}")

# مسار المصارعة
def w_title_step(message, slug, date_str):
    if message.text.startswith('/'): return
    title = message.text.strip()
    msg = bot.reply_to(message, "🔢 أرسل <b>الـ ID</b> الخاص بالعرض في الموقع (رقم):", parse_mode="HTML")
    bot.register_next_step_handler(msg, w_id_step, slug, date_str, title)

def w_id_step(message, slug, date_str, title):
    if message.text.startswith('/'): return
    try: series_id = int(message.text.strip())
    except ValueError: return bot.reply_to(message, "❌ يجب أن يكون الرقم صحيحاً.")
    msg = bot.reply_to(message, "🔢 أرسل <b>رقم الحلقة الحالي</b> للعرض في موقعك (عشان أزود عليه المرة الجاية، لو لسه جديد اكتب 0):", parse_mode="HTML")
    bot.register_next_step_handler(msg, w_save_step, slug, date_str, title, series_id)

def w_save_step(message, slug, date_str, title, series_id):
    if message.text.startswith('/'): return
    try: last_ep = int(message.text.strip())
    except ValueError: return bot.reply_to(message, "❌ يجب أن يكون الرقم صحيحاً.")
    
    data = load_series_data()
    data[slug] = {"type": "wrestling", "title": title, "last_date": date_str, "last_ep": last_ep, "series_id": series_id}
    save_series_data(data)
    bot.reply_to(message, f"✅ <b>تمت إضافة المصارعة!</b>\nسيبحث عن العرض التالي بعد 7 أيام من تاريخ {date_str}.", parse_mode="HTML")

# مسار المسلسلات
def s_title_step(message, slug, region, season, episode):
    if message.text.startswith('/'): return
    title = message.text.strip()
    msg = bot.reply_to(message, "🔢 أرسل <b>الـ ID</b> الخاص بالمسلسل في الموقع (رقم):", parse_mode="HTML")
    bot.register_next_step_handler(msg, s_save_step, slug, region, season, episode, title)

def s_save_step(message, slug, region, season, episode, title):
    if message.text.startswith('/'): return
    try: series_id = int(message.text.strip())
    except ValueError: return bot.reply_to(message, "❌ يجب أن يكون الرقم صحيحاً.")
    
    data = load_series_data()
    data[slug] = {"type": "series", "title": title, "season": season, "last_ep": episode, "region": region, "series_id": series_id}
    save_series_data(data)
    bot.reply_to(message, f"✅ <b>تمت إضافة المسلسل!</b>\nسيبحث عن الحلقة {episode + 1}", parse_mode="HTML")

# ==========================================
# أوامر التعديل والقوائم
# ==========================================
@bot.message_handler(commands=["setep"])
def set_episode(message):
    if not admin_only(message): return
    try:
        slug, episode = message.text.split()[1:3]
        data = load_series_data()
        if slug in data:
            data[slug]["last_ep"] = int(episode)
            save_series_data(data)
            bot.reply_to(message, f"✅ تم تعديل الحلقة السابقة إلى: <b>{episode}</b>", parse_mode="HTML")
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
            bot.reply_to(message, f"✅ تم تعديل آخر تاريخ للمصارعة إلى: <b>{new_date}</b>", parse_mode="HTML")
    except: bot.reply_to(message, "❌ الصيغة: /setdate slug YYYY-MM-DD")

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
    bot.reply_to(message, "🗑 <b>اختر للحذف:</b>", reply_markup=markup, parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data.startswith('del_'))
def process_delete_callback(call):
    slug = call.data.split('del_')[1]
    data = load_series_data()
    if slug in data:
        del data[slug]
        save_series_data(data)
        bot.answer_callback_query(call.id, "تم الحذف بنجاح! ✅")
        bot.edit_message_text("✅ تم الحذف من القائمة.", call.message.chat.id, call.message.message_id)

@bot.message_handler(commands=["check", "status"])
def status(message):
    if admin_only(message): bot.reply_to(message, status_message(), parse_mode="HTML")

@bot.message_handler(commands=["scan"])
def force_check(message):
    if not admin_only(message): return
    if not scan_lock.acquire(blocking=False): return bot.reply_to(message, "⏳ يوجد فحص جارٍ...")
    scan_lock.release()
    bot.reply_to(message, "🔎 <b>بدأ الفحص اليدوي...</b>", parse_mode="HTML")
    threading.Thread(target=lambda: bot.send_message(ADMIN_CHAT_ID, "✅ <b>انتهى الفحص!</b>\n\n" + ("\n".join(scan_all_series_once()) or "📭 فارغ."), parse_mode="HTML"), daemon=True).start()

if __name__ == "__main__":
    threading.Thread(target=auto_checker_loop, daemon=True).start()
    print("Bot is running with Wrestling & Backup features...", flush=True)
    bot.infinity_polling()
