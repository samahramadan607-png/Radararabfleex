import html
import json
import os
import re
import threading
import time
import binascii
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
import urllib3
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from Crypto.Cipher import AES

# تعطيل تحذيرات عدم التحقق من شهادة SSL
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BOT_TOKEN = "7808630939:AAEY0_q6vnkKlMRjvXNmEXwK1G80hv0vghY"
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "1013251619")

DATA_FILE = os.environ.get("DATA_FILE", "series.json")
CHECK_INTERVAL_SECONDS = int(os.environ.get("CHECK_INTERVAL_SECONDS", "180"))
SOURCE_DOMAINS = ["b2.shahidtv.net", "b1.shahidtv.net", "b3.shahidtv.net"]

# بيانات الـ API لموقع arab flex
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
                session.get(f"{url}?i=1", timeout=15, verify=False)
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

def candidate_urls(slug, season, episode, region):
    regions = list(dict.fromkeys([region, "EG", "LB", "SA", "SY", "MA"]))
    qualities = ["360p", "480p", "720p", "1080p"]
    episode_codes = [f"EP{episode:03d}", f"EP{episode:02d}"]
    suffixes = {
        quality: [
            f"-{quality}-v3.mp4",
            f"-{quality}-v2.mp4",
            f"-{quality}.mp4",
            f"-{quality}-v1.mp4",
            f"-{quality}-v4.mp4",
        ]
        for quality in qualities
    }
    for quality in qualities:
        for domain in SOURCE_DOMAINS:
            for item_region in regions:
                for episode_code in episode_codes:
                    for suffix in suffixes[quality]:
                        yield quality, (
                            f"https://{domain}/files/{item_region}/{slug}/"
                            f"{slug}-S{season:02d}-{episode_code}{suffix}"
                        )

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
        has_video_type = (
            not content_type
            or "video/" in content_type
            or "application/octet-stream" in content_type
        )
        if response.status_code != 200 or not has_video_type:
            return False
        if content_length and content_length.isdigit() and int(content_length) < 100_000:
            return False
        return True
    except requests.RequestException:
        return False

def scan_series(slug, info):
    global last_scan_result
    episode = int(info.get("last_ep", 0)) + 1
    season = int(info.get("season", 1))
    links = {}
    attempts = 0
    for quality, url in candidate_urls(
        slug, season, episode, str(info.get("region", "EG")).upper()
    ):
        if quality in links:
            continue
        attempts += 1
        if check_link(url):
            links[quality] = url
            print(f"[FOUND] {slug} episode={episode} quality={quality}", flush=True)

    if not links:
        last_scan_result = (
            f"{info.get('title', slug)}: الحلقة {episode} غير موجودة "
            f"(تم فحص {attempts} رابط)"
        )
        return None

    formatted_links = []
    for q in ["360p", "480p", "720p", "1080p"]:
        if q in links:
            q_number = q.replace("p", "")
            formatted_links.append(f"{q_number}|{links[q]}")
    
    links_string = ",".join(formatted_links)
    title = info.get("title", slug)
    series_id = info.get("series_id")

    api_status = "لم يتم تحديد ID للمسلسل"
    if series_id:
        payload = {
            "secret_key": SECRET_KEY,
            "action": "insert",
            "series_id": series_id,
            "title": f"الحلقة {episode}",
            "episode_number": episode,
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
        f"📺 <b>المسلسل:</b> {title}\n"
        f"🎬 <b>الحلقة:</b> {episode}\n"
        f"🌐 <b>حالة الموقع:</b> {api_status}\n\n"
        f"<code>{links_string}</code>"
    )

    bot.send_message(
        ADMIN_CHAT_ID,
        final_message,
        disable_web_page_preview=True,
        parse_mode="HTML"
    )

    last_scan_result = f"{info.get('title', slug)}: تم إيجاد الحلقة {episode} وإرسال التنبيه ✅"
    return episode

def scan_all_series_once():
    global last_scan_at, scan_cycles, total_added
    if not scan_lock.acquire(blocking=False):
        return []
    try:
        scan_cycles += 1
        last_scan_at = datetime.now(timezone.utc)
        data = load_series_data()
        results = []
        for slug, info in list(data.items()):
            new_episode = scan_series(slug, info)
            if new_episode:
                total_added += 1
                data[slug]["last_ep"] = new_episode
                save_series_data(data)
                results.append(f"{info.get('title', slug)}: تم إرسال الحلقة {new_episode} ✅")
            else:
                results.append(last_scan_result)
            time.sleep(2)
        return results
    finally:
        scan_lock.release()

def auto_checker_loop():
    while True:
        try:
            scan_all_series_once()
        except Exception as error:
            print(f"[ERROR] Checker loop: {error}", flush=True)
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
        "✅ <b>البوت شغّال وبيفحص كل 3 دقايق!</b>",
        "",
        f"⏱ <b>وقت التشغيل:</b> {format_duration(uptime)}",
        f"🕐 <b>بدأ في:</b> {started_at.astimezone().strftime('%Y-%m-%d %H:%M:%S')}",
        f"🔍 <b>آخر فحص:</b> {last_scan_at.astimezone().strftime('%Y-%m-%d %H:%M:%S') if last_scan_at else 'لم يبدأ بعد'}",
        f"🔄 <b>عدد دورات الفحص:</b> {scan_cycles}",
        f"➕ <b>إجمالي الحلقات المكتشفة:</b> {total_added}",
        "",
        "📺 <b>آخر حالة للمسلسلات:</b>",
    ]
    if not data:
        lines.append("  لا توجد مسلسلات مضافة.")
    else:
        for slug, info in data.items():
            title = html.escape(str(info.get("title", slug)))
            last_episode = int(info.get("last_ep", 0))
            series_id = info.get("series_id", "غير محدد ❌")
            lines.append(
                f"  • <b>{title}</b> (ID: {series_id}): حلقة {last_episode} — "
                f"⏳ ح{last_episode + 1} بانتظار الفحص"
            )
    return "\n".join(lines)

def admin_only(message):
    return str(message.chat.id) == str(ADMIN_CHAT_ID)

@bot.message_handler(commands=["start", "help"])
def welcome(message):
    if admin_only(message):
        bot.reply_to(
            message,
            "🤖 <b>أهلاً بك في نظام المراقبة التلقائي</b> 🎬\n\n"
            "📌 <b>قائمة الأوامر المتاحة:</b>\n"
            "🔹 <code>/add</code> — إضافة مسلسل جديد للمراقبة\n"
            "🔹 <code>/del</code> — حذف مسلسل من المراقبة\n"
            "🔹 <code>/list</code> — عرض قائمة المسلسلات الحالية\n"
            "🔹 <code>/setep</code> — تعديل رقم آخر حلقة لمسلسل\n"
            "🔹 <code>/setid</code> — إضافة أو تعديل ID المسلسل في الموقع\n"
            "🔹 <code>/check</code> — عرض حالة البوت والإحصائيات\n"
            "🔹 <code>/scan</code> — إجبار البوت على الفحص فوراً\n"
            "🔹 <code>/testapi</code> — إرسال حلقة وهمية لاختبار الموقع",
            parse_mode="HTML",
        )

@bot.message_handler(commands=["testapi"])
def test_api_command(message):
    if not admin_only(message):
        return
    
    try:
        slug = message.text.split()[1]
    except IndexError:
        bot.reply_to(message, "❌ <b>خطأ:</b> اكتب الأمر وبجواره مُعرف المسلسل.\nمثال: <code>/testapi Ahmr-wla-Abyd</code>", parse_mode="HTML")
        return
        
    data = load_series_data()
    if slug not in data:
        bot.reply_to(message, "⚠️ <b>المسلسل غير موجود.</b>", parse_mode="HTML")
        return
        
    series_id = data[slug].get("series_id")
    if not series_id:
        bot.reply_to(message, "❌ <b>هذا المسلسل ليس له ID مربوط بالموقع.</b>", parse_mode="HTML")
        return

    payload = {
        "secret_key": SECRET_KEY,
        "action": "insert",
        "series_id": series_id,
        "title": "حلقة تجريبية (999)",
        "episode_number": 999,
        "links_string": "1080|https://test.com/vid.mp4"
    }
    
    msg = bot.reply_to(message, "⏳ جاري إرسال حلقة وهمية (999) لاختبار الاتصال بالموقع مع فك تشفير الحماية...")
    
    try:
        session = get_infinity_session(API_URL)
        res = session.post(API_URL, data=payload, timeout=20, verify=False)
        
        if "INSERTED" in res.text:
            bot.edit_message_text(f"✅ **نجح الاتصال!**\nتمت إضافة الحلقة 999 بنجاح للمسلسل (ID: {series_id}).\n\n(لا تنسَ حذفها من لوحة تحكم موقعك لاحقاً).", msg.chat.id, msg.message_id)
        else:
            bot.edit_message_text(f"⚠️ **رد غير متوقع من الموقع:**\n`{res.text}`", msg.chat.id, msg.message_id, parse_mode="Markdown")
    except Exception as e:
        bot.edit_message_text(f"❌ **خطأ برمجي أو فشل في الاتصال:**\n`{str(e)}`", msg.chat.id, msg.message_id, parse_mode="Markdown")

@bot.message_handler(commands=["setid"])
def set_series_id(message):
    if not admin_only(message):
        return
    try:
        slug, series_id = message.text.split()[1:3]
        data = load_series_data()
        if slug not in data:
            bot.reply_to(message, "⚠️ <b>المسلسل غير موجود في قائمة المتابعة.</b>", parse_mode="HTML")
            return
        
        data[slug]["series_id"] = int(series_id)
        save_series_data(data)
        
        clean_title = html.escape(data[slug].get("title", slug))
        bot.reply_to(
            message, 
            f"✅ <b>تم ربط المسلسل بالموقع!</b>\n"
            f"📺 المسلسل: <b>{clean_title}</b>\n"
            f"🔢 الـ ID في الموقع: <b>{series_id}</b>", 
            parse_mode="HTML"
        )
    except (IndexError, ValueError):
        bot.reply_to(
            message, 
            "❌ <b>خطأ! الصيغة الصحيحة هي:</b>\n<code>/setid slug ID</code>\n\n💡 مثال: <code>/setid bnj-kuly 271</code>", 
            parse_mode="HTML"
        )

@bot.message_handler(commands=["add"])
def add_series_start(message):
    if not admin_only(message):
        return
    msg = bot.reply_to(message, "🔗 <b>إضافة مسلسل جديد:</b>\nأرسل لي <b>رابط آخر حلقة</b> نزلت للمسلسل لاستخراج البيانات منه تلقائياً.\n💡 مثال:\n<code>https://b2.shahidtv.net/files/EG/bnj-kuly/bnj-kuly-S01-EP011-360p.mp4</code>", parse_mode="HTML")
    bot.register_next_step_handler(msg, process_link_step)

def process_link_step(message):
    if message.text.startswith('/'): return
    link = message.text.strip()
    
    try:
        parts = link.split('/')
        region = parts[4]
        slug = parts[5]
        filename = parts[-1]
        
        match = re.search(r'-S(\d+)-EP(\d+)', filename, re.IGNORECASE)
        if match:
            season = int(match.group(1))
            episode = int(match.group(2))
        else:
            bot.reply_to(message, "❌ لم أتمكن من العثور على رقم الموسم والحلقة في الرابط. حاول مجدداً.")
            return

        msg = bot.reply_to(message, f"✅ تم استخراج البيانات:\nالمعرف: <code>{slug}</code>\nالموسم: <code>{season}</code>\nآخر حلقة: <code>{episode}</code>\n\n📝 أرسل الآن <b>اسم المسلسل</b> لحفظه:", parse_mode="HTML")
        bot.register_next_step_handler(msg, process_title_step, slug, region, season, episode)
    
    except Exception as e:
        bot.reply_to(message, f"❌ حدث خطأ في قراءة الرابط. تأكد أنه رابط صحيح.\nالخطأ: {e}")

def process_title_step(message, slug, region, season, episode):
    if message.text.startswith('/'): return
    title = message.text.strip()
    
    msg = bot.reply_to(message, f"✅ تم حفظ الاسم.\n\n🔢 أخيراً، أرسل <b>الـ ID الخاص بالمسلسل</b> في قاعدة بيانات موقعك (رقم فقط):", parse_mode="HTML")
    bot.register_next_step_handler(msg, save_extracted_series, slug, region, season, episode, title)

def save_extracted_series(message, slug, region, season, episode, title):
    if message.text.startswith('/'): return
    
    try:
        series_id = int(message.text.strip())
    except ValueError:
        bot.reply_to(message, "❌ الـ ID يجب أن يكون رقماً فقط. يرجى إعادة الإضافة من البداية عبر /add.")
        return

    data = load_series_data()
    data[slug] = {
        "title": title,
        "season": season,
        "last_ep": episode,
        "region": region,
        "series_id": series_id
    }
    save_series_data(data)
    
    bot.reply_to(
        message, 
        f"✅ <b>تمت الإضافة بنجاح!</b>\n📺 المسلسل: <b>{title}</b>\n🔗 الـ ID مربوط بـ: <b>{series_id}</b>\n⏳ البوت سيبحث الآن عن الحلقة القادمة: <b>{episode + 1}</b>", 
        parse_mode="HTML"
    )

@bot.message_handler(commands=["setep"])
def set_episode(message):
    if not admin_only(message):
        return
    try:
        slug, episode = message.text.split()[1:3]
        data = load_series_data()
        if slug not in data:
            bot.reply_to(message, "⚠️ <b>المسلسل غير موجود في قائمة المتابعة.</b>", parse_mode="HTML")
            return
        data[slug]["last_ep"] = int(episode)
        save_series_data(data)
        
        clean_title = html.escape(data[slug].get("title", slug))
        bot.reply_to(
            message, 
            f"✅ <b>تم تحديث الحلقات!</b>\n"
            f"📺 المسلسل: <b>{clean_title}</b>\n"
            f"🔢 الحلقة السابقة أصبحت: <b>{int(episode)}</b>\n"
            f"⏳ البوت سيبحث الآن عن الحلقة: <b>{int(episode) + 1}</b>", 
            parse_mode="HTML"
        )
    except (IndexError, ValueError):
        bot.reply_to(
            message, 
            "❌ <b>خطأ! الصيغة الصحيحة هي:</b>\n<code>/setep slug رقم_الحلقة</code>\n\n💡 مثال: <code>/setep al-thaman 20</code>", 
            parse_mode="HTML"
        )

@bot.message_handler(commands=["list"])
def list_series(message):
    if not admin_only(message):
        return
    data = load_series_data()
    text = "\n".join(
        f"▪️ <b>{html.escape(str(info.get('title', slug)))}</b> <code>({slug})</code>\n"
        f"    └ آخر حلقة: {info.get('last_ep', 0)} | ID الموقع: {info.get('series_id', '❌')}"
        for slug, info in data.items()
    ) or "📭 لا توجد مسلسلات مضافة حالياً في القائمة."
    bot.reply_to(message, "📋 <b>قائمة المسلسلات تحت المراقبة:</b>\n\n" + text, parse_mode="HTML")

@bot.message_handler(commands=["del"])
def delete_series_start(message):
    if not admin_only(message):
        return
    data = load_series_data()
    if not data:
        bot.reply_to(message, "📭 لا توجد مسلسلات مضافة حالياً للحذف.")
        return
    
    markup = InlineKeyboardMarkup(row_width=1)
    for slug, info in data.items():
        title = info.get("title", slug)
        markup.add(InlineKeyboardButton(text=f"❌ حذف: {title}", callback_data=f"del_{slug}"))
    
    bot.reply_to(message, "🗑 <b>اختر المسلسل الذي تريد حذفه:</b>", reply_markup=markup, parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data.startswith('del_'))
def process_delete_callback(call):
    slug = call.data.split('del_')[1]
    data = load_series_data()
    
    if slug in data:
        deleted_title = html.escape(data[slug].get("title", slug))
        del data[slug]
        save_series_data(data)
        bot.answer_callback_query(call.id, "تم الحذف بنجاح! ✅")
        bot.edit_message_text(f"✅ تم حذف مسلسل <b>{deleted_title}</b> من قائمة المراقبة.", call.message.chat.id, call.message.message_id, parse_mode="HTML")
    else:
        bot.answer_callback_query(call.id, "⚠️ المسلسل غير موجود أو تم حذفه مسبقاً!", show_alert=True)

@bot.message_handler(commands=["check", "status"])
def status(message):
    if admin_only(message):
        bot.reply_to(message, status_message(), parse_mode="HTML")

@bot.message_handler(commands=["scan"])
def force_check(message):
    if not admin_only(message):
        return
    if not scan_lock.acquire(blocking=False):
        bot.reply_to(message, "⏳ <b>يوجد فحص جارٍ بالفعل في الخلفية، انتظر ثوانٍ...</b>", parse_mode="HTML")
        return
    
    scan_lock.release()
    bot.reply_to(message, "🔎 <b>بدأ الفحص اليدوي الآن، برجاء الانتظار...</b>", parse_mode="HTML")

    def manual_scan_worker():
        results = scan_all_series_once()
        bot.send_message(
            ADMIN_CHAT_ID,
            "✅ <b>انتهى الفحص اليدوي!</b>\n\n"
            + ("\n".join(f"🔸 {html.escape(str(item))}" for item in results) or "📭 لا توجد مسلسلات مسجلة."),
            parse_mode="HTML",
        )

    threading.Thread(target=manual_scan_worker, daemon=True).start()

if __name__ == "__main__":
    threading.Thread(target=auto_checker_loop, daemon=True).start()
    print("Bot is running with AES bypass & SSL ignore...", flush=True)
    bot.infinity_polling()
