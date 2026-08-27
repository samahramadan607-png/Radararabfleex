import html
import json
import os
import threading
import time
from datetime import datetime, timezone

import requests
import telebot
BOT_TOKEN = "7808630939:AAEY0_q6vnkKlMRjvXNmEXwK1G80hv0vghY" # تأكد من وضع التوكن الخاص بك
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "1013251619")

# مسار الحفظ (جاهز للعمل على سيرفرات Railway أو محلياً)
DATA_FILE = os.environ.get("DATA_FILE", "series.json")

CHECK_INTERVAL_SECONDS = int(os.environ.get("CHECK_INTERVAL_SECONDS", "180"))
SOURCE_DOMAINS = ["b2.shahidtv.net", "b1.shahidtv.net", "b3.shahidtv.net"]

bot = telebot.TeleBot(BOT_TOKEN)

scan_lock = threading.Lock()
started_at = datetime.now(timezone.utc)
last_scan_at = None
scan_cycles = 0
total_added = 0
last_scan_result = "لم يبدأ فحص بعد"

def load_series_data():
    """قراءة بيانات المسلسلات من ملف البوت المحلي"""
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
    """حفظ بيانات المسلسلات في ملف البوت المحلي"""
    parent = os.path.dirname(DATA_FILE)
    if parent:
        os.makedirs(parent, exist_ok=True)
    temporary_file = f"{DATA_FILE}.tmp"
    with open(temporary_file, "w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
    os.replace(temporary_file, DATA_FILE)

def candidate_urls(slug, season, episode, region):
    """توليد كل الاحتمالات الممكنة لرابط الحلقة الجديدة"""
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
    """التحقق مما إذا كان الرابط يعمل ويحتوي على فيديو"""
    try:
        response = requests.get(
            url,
            headers={"User-Agent": "EpisodeChecker/1.0"},
            timeout=8,
            stream=True,
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
        # التأكد أن الملف حجمه سليم وليس معطوباً
        if content_length and content_length.isdigit() and int(content_length) < 100_000:
            return False
        return True
    except requests.RequestException:
        return False

def scan_series(slug, info):
    """فحص الحلقة القادمة للمسلسل وإرسال رسالة تليجرام فور إيجادها"""
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

    # تم العثور على روابط! إرسال رسالة تليجرام فقط.
    title = html.escape(str(info.get("title", slug)))
    quality_lines = [
        f"✅ <b>{quality}</b>\n<code>{html.escape(links[quality])}</code>"
        for quality in ("360p", "480p", "720p", "1080p")
        if quality in links
    ]
    bot.send_message(
        ADMIN_CHAT_ID,
        "🚀 <b>حلقة جديدة متاحة!</b>\n\n"
        f"🎬 <b>المسلسل:</b> {title}\n"
        f"📺 <b>الحلقة:</b> {episode}\n\n"
        "🔗 <b>الروابط المتاحة:</b>\n\n"
        + "\n\n".join(quality_lines)
        + "\n\n📌 <i>هذا مجرد تنبيه، لم يتم إضافة أي شيء للموقع الخاص بك.</i>",
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    last_scan_result = f"{info.get('title', slug)}: تم إيجاد الحلقة {episode} وإرسال التنبيه ✅"
    return episode

def scan_all_series_once():
    """الدوران على كل المسلسلات الموجودة في النوتة (JSON) لفحصها"""
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
                # تحديث النوتة الخاصة بالبوت عشان يطلب الحلقة اللي بعدها المرة الجاية
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
    """هذه الدالة تعمل في الخلفية وتفحص كل 3 دقائق"""
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
            lines.append(
                f"  • <b>{title}</b>: حلقة {last_episode} — "
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
            "يقوم هذا البوت بفحص سيرفرات المشاهدة كل 3 دقائق للبحث عن الحلقات الجديدة، وإرسال الروابط المباشرة لك فور توفرها.\n"
            "<i>(لا يتم نشر أي شيء في موقعك، هذا للإشعار فقط)</i>\n\n"
            "📌 <b>قائمة الأوامر المتاحة:</b>\n"
            "🔹 <code>/add</code> — إضافة مسلسل جديد للمراقبة\n"
            "🔹 <code>/del</code> — حذف مسلسل من المراقبة\n"
            "🔹 <code>/list</code> — عرض قائمة المسلسلات الحالية\n"
            "🔹 <code>/setep</code> — تعديل رقم آخر حلقة لمسلسل\n"
            "🔹 <code>/check</code> — عرض حالة البوت والإحصائيات\n"
            "🔹 <code>/scan</code> — إجبار البوت على الفحص فوراً",
            parse_mode="HTML",
        )


@bot.message_handler(commands=["add"])
def add_series(message):
    if not admin_only(message):
        return
    try:
        parts = message.text.split()
        slug, title, series_id, last_ep = parts[1:5]
        region = parts[5].upper() if len(parts) > 5 else "EG"
        data = load_series_data()
        
        # استبدال الشرطة السفلية بمسافة لاسم المسلسل
        clean_title = title.replace("_", " ")
        
        data[slug] = {
            "series_id": int(series_id),
            "title": clean_title,
            "season": 1,
            "last_ep": int(last_ep),
            "region": region,
        }
        save_series_data(data)
        bot.reply_to(
            message, 
            f"✅ <b>تمت الإضافة بنجاح!</b>\n"
            f"📺 المسلسل: <b>{html.escape(clean_title)}</b>\n"
            f"⏳ ننتظر الآن توفر الحلقة: <b>{int(last_ep) + 1}</b>\n"
            f"🌍 المنطقة: <b>{region}</b>", 
            parse_mode="HTML"
        )
    except (IndexError, ValueError):
        bot.reply_to(
            message,
            "❌ <b>خطأ في كتابة الأمر!</b>\n\n"
            "📌 <b>الصيغة الصحيحة:</b>\n"
            "<code>/add slug Name ID Last_EP Region</code>\n\n"
            "💡 <b>مثال:</b>\n"
            "<code>/add al-thaman الثمن 123 15 EG</code>\n\n"
            "<i>⚠️ ملاحظة: إذا كان اسم المسلسل يتكون من كلمتين، استخدم شرطة سفلية ( _ ) بدلاً من المسافة، مثال: <code>مسار_إجباري</code></i>",
            parse_mode="HTML",
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
        f"    └ آخر حلقة: {info.get('last_ep', 0)} ⏳ (ننتظر {int(info.get('last_ep', 0))+1})"
        for slug, info in data.items()
    ) or "📭 لا توجد مسلسلات مضافة حالياً في القائمة."
    bot.reply_to(message, "📋 <b>قائمة المسلسلات تحت المراقبة:</b>\n\n" + text, parse_mode="HTML")


@bot.message_handler(commands=["del"])
def delete_series(message):
    if not admin_only(message):
        return
    try:
        slug = message.text.split()[1]
        data = load_series_data()
        if slug in data:
            deleted_title = html.escape(data[slug].get("title", slug))
            del data[slug]
            save_series_data(data)
            bot.reply_to(message, f"✅ <b>تم الحذف بنجاح!</b>\nتمت إزالة <b>{deleted_title}</b> من قائمة المراقبة.", parse_mode="HTML")
        else:
            bot.reply_to(message, "⚠️ <b>المسلسل غير موجود في قائمة المتابعة.</b>", parse_mode="HTML")
    except IndexError:
        bot.reply_to(message, "❌ <b>خطأ! الصيغة الصحيحة هي:</b>\n<code>/del slug</code>\n\n💡 مثال: <code>/del al-thaman</code>", parse_mode="HTML")


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
    # تشغيل لوب الفحص المستمر كل 3 دقائق في الخلفية
    threading.Thread(target=auto_checker_loop, daemon=True).start()
    print("Bot is running with JSON internal storage and 3-min loop...", flush=True)
    # تشغيل التليجرام
    bot.infinity_polling()
