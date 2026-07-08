# -*- coding: utf-8 -*-
"""
بوت تليجرام لعزل الأصوات/الموسيقى باستخدام Demucs.

تشغيل:
    export BOT_TOKEN="123456:ABC..."
    export ADMIN_ID="8084323446"
    python3 music_isolator_bot.py

متغيرات بيئة اختيارية لضبط استهلاك الرام والأداء (القيم الافتراضية مناسبة لسيرفر ضعيف الموارد):
    WORK_DIR                 مجلد الملفات المؤقتة (افتراضي: workdir)
    MAX_VIDEO_SECONDS         أقصى مدة فيديو قبل الاقتصاص (افتراضي: 180)
    MAX_DOWNLOAD_MB           أقصى حجم ملف/رابط بالميجابايت (افتراضي: 80)
    QUEUE_WORKERS             عدد عمّال الطابور المتوازيين (افتراضي: 2)
    MAX_CONCURRENT_DEMUCS     كم عملية Demucs تشتغل بنفس الوقت (افتراضي: 1) <-- الأهم لتوفير الرام
    DEMUCS_DEVICE             cpu أو cuda (افتراضي: cpu)
    DEMUCS_SEGMENT            تقطيع المعالجة بالثواني لتقليل الذاكرة (افتراضي: 7، الحد الأقصى ~7.8 لموديل htdemucs)
    YTDLP_TIMEOUT / DEMUCS_TIMEOUT / FFMPEG_TIMEOUT   مهلات زمنية بالثواني لكل عملية

ملاحظة: يتطلب توفر ffmpeg و ffprobe و yt-dlp و demucs في PATH.
"""

import os
import time
import uuid
import shutil
import queue
import logging
import subprocess
import threading
from dataclasses import dataclass
from typing import Optional

import psutil
import telebot
from telebot import types
from flask import Flask

# ---------------------------------------------------------------------------
# الإعدادات
# ---------------------------------------------------------------------------

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0") or 0)

if not BOT_TOKEN:
    raise SystemExit(
        "❌ لم يتم تعيين BOT_TOKEN. عرّفه كمتغير بيئة قبل التشغيل:\n"
        "   export BOT_TOKEN='123456:ABC...'\n"
        "(ولا تضع التوكن مباشرة داخل الكود أبداً — إذا كان مكشوفاً سابقاً، جدّده فوراً عبر @BotFather)"
    )
if not ADMIN_ID:
    raise SystemExit("❌ لم يتم تعيين ADMIN_ID. عرّفه كمتغير بيئة: export ADMIN_ID='123456789'")

WORK_DIR = os.environ.get("WORK_DIR", "workdir")
MAX_VIDEO_SECONDS = int(os.environ.get("MAX_VIDEO_SECONDS", "180"))
MAX_DOWNLOAD_MB = int(os.environ.get("MAX_DOWNLOAD_MB", "80"))
QUEUE_WORKERS = int(os.environ.get("QUEUE_WORKERS", "2"))
MAX_CONCURRENT_DEMUCS = int(os.environ.get("MAX_CONCURRENT_DEMUCS", "1"))
DEMUCS_DEVICE = os.environ.get("DEMUCS_DEVICE", "cpu")
DEMUCS_SEGMENT = os.environ.get("DEMUCS_SEGMENT", "7")
YTDLP_TIMEOUT = int(os.environ.get("YTDLP_TIMEOUT", "240"))
DEMUCS_TIMEOUT = int(os.environ.get("DEMUCS_TIMEOUT", "600"))
FFMPEG_TIMEOUT = int(os.environ.get("FFMPEG_TIMEOUT", "300"))

USERS_FILE = "bot_users.txt"
BANNED_FILE = "banned_users.txt"

os.makedirs(WORK_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("music_bot")

bot = telebot.TeleBot(BOT_TOKEN, threaded=True)

# يحدّ من عدد عمليات Demucs المتزامنة بغض النظر عن عدد عمّال الطابور،
# لأن هذه العملية هي الأكثر استهلاكاً للرام والمعالج.
demucs_semaphore = threading.Semaphore(MAX_CONCURRENT_DEMUCS)

# ---------------------------------------------------------------------------
# تخزين المستخدمين (مع أقفال لتفادي تعارض الكتابة بين الخيوط)
# ---------------------------------------------------------------------------

_users_lock = threading.Lock()
_banned_lock = threading.Lock()

for _file in (USERS_FILE, BANNED_FILE):
    if not os.path.exists(_file):
        open(_file, "w").close()


def save_user(user_id: int) -> None:
    with _users_lock:
        with open(USERS_FILE, "r+") as f:
            users = set(f.read().splitlines())
            if str(user_id) not in users:
                f.write(str(user_id) + "\n")


def get_users_count() -> int:
    with _users_lock:
        with open(USERS_FILE, "r") as f:
            return len(f.read().splitlines())


def get_all_users():
    with _users_lock:
        with open(USERS_FILE, "r") as f:
            return f.read().splitlines()


def ban_user_permanently(user_id: int) -> None:
    with _banned_lock:
        with open(BANNED_FILE, "r+") as f:
            banned = set(f.read().splitlines())
            if str(user_id) not in banned:
                f.write(str(user_id) + "\n")


def is_permanently_banned(user_id: int) -> bool:
    with _banned_lock:
        with open(BANNED_FILE, "r") as f:
            return str(user_id) in f.read().splitlines()


# ---------------------------------------------------------------------------
# الحالة داخل الذاكرة
# ---------------------------------------------------------------------------

user_status = {}
user_lang = {}
maintenance_mode = False

user_spam_counter = {}
temp_banned_users = {}
warning_emitted = {}

# ---------------------------------------------------------------------------
# اللغات
# ---------------------------------------------------------------------------

LANGUAGES = {
    'ar': {
        'welcome': "أهلاً بك في بوت عازل الموسيقى والذكاء الاصطناعي المتكامل! 🚀\n\nالرجاء اختيار الخدمة المطلوبة من الأزرار بالأسفل، ثم أرسل الملف أو الرابط.",
        'admin_line': "\n\n⚙️ أهلاً أيها المطور: يمكنك استخدام أمر /admin لفتح لوحة التحكم.",
        'maintenance': "⚠️ البوت في وضع الصيانة السريعة لتحديث السيرفرات، سنعود خلال دقائق ونعتذر عن الإزعاج!",
        'spam_warn': "⚠️ يرجى إرسال ملف واحد في المرة لعدم الضغط على السيرفر، تم تجاهل الطلب الزائد تفادياً للحظر.",
        'spam_temp': "🚫 تم حظرك مؤقتاً لمدة 10 دقائق بسبب إرسال ملفات كثيرة بدافع التخريب. يرجى الانتظار.",
        'spam_perm': "❌ تم حظرك نهائياً من البوت لمخالفتك شروط الاستخدام والتخريب المستمر للسيرفر.",
        'btn_video': "🎬 عزل فيديو / رابط", 'btn_audio': "🎵 عزل وتصفية صوت", 'btn_summary': "📝 تلخيص البودكاست (مغلق)", 'btn_trans': "🌍 ترجمة وطباعة نصوص (مغلق)", 'btn_search': "🔍 بحث داخل الفيديو (مغلق)",
        'choose_service': "الرجاء تحديد نوع الخدمة أولاً بالضغط على الأزرار بالأسفل 👇",
        'send_video': "أرسل مقطع فيديو (MP4) أو رابط تيك توك/ريلز لعزل الموسيقى منه تلقائياً.",
        'send_audio': "أرسل الآن ملف صوتي أو ريكورد لتنقيته وعزله من الضوضاء والموسيقى.",
        'send_sum': "⚠️ عذراً، ميزة التلخيص غير متوفرة حالياً بصيانة السيرفر.",
        'send_trans': "⚠️ عذراً، ميزة الترجمة غير متوفرة حالياً بصيانة السيرفر.",
        'send_search': "⚠️ عذراً، ميزة البحث داخل الفيديو غير متوفرة حالياً.",
        'processing': "⏳ بدأ دورك! جاري معالجة طلبك عبر الذكاء الاصطناعي، يرجى الانتظار بقناة الطابور...",
        'success': "🚀 تمت المعالجة بنجاح! جاري رفع ملفك...",
        'txt_link': "📥 جاري استخراج وتحميل مقطع الفيديو من الرابط بأعلى جودة...",
        'txt_cut': "✂️ المقطع طويل، تم اقتصاص أول 3 دقائق منه لعزلها وحماية السيرفر...",
        'txt_iso': "🎧 جاري عزل الموسيقى بالذكاء الاصطناعي (Demucs)...",
        'txt_merge': "🎬 جاري إعادة الدمج الصوتي وإنتاج الملف النظيف...",
        'txt_clean': "🎵 جاري عزل الموسيقى وتنقية الريكورد من الشوشة المحيطة...",
        'err_too_large': f"⚠️ حجم الملف أكبر من الحد المسموح ({MAX_DOWNLOAD_MB}MB). جرّب ملفاً أصغر.",
        'err_link': "❌ تعذّر تحميل الرابط. تأكد أنه صحيح وعام (غير خاص) وحاول مجدداً.",
        'err_generic': "❌ حدث خطأ أثناء المعالجة، حاول مجدداً أو أرسل ملفاً/رابطاً آخر.",
        'err_timeout': "⏱️ استغرقت المعالجة وقتاً طويلاً جداً وتم إلغاؤها، جرّب ملفاً أقصر.",
    },
    'en': {
        'welcome': "Welcome to the Ultimate AI Music Isolator Bot! 🚀\nPlease choose a service from the buttons below, then send your file or link.",
        'admin_line': "\n\n⚙️ Hello Developer: Use /admin to open the panel.",
        'maintenance': "⚠️ The bot is under maintenance. We will be back shortly!",
        'spam_warn': "⚠️ Please send one file at a time to avoid spamming the server.",
        'spam_temp': "🚫 You have been temporarily banned for 10 minutes due to spamming.",
        'spam_perm': "❌ You have been permanently banned for continuous spamming.",
        'btn_video': "🎬 Isolate Video/Link", 'btn_audio': "🎵 Isolate/Clean Audio", 'btn_summary': "📝 Audio Summary (Closed)", 'btn_trans': "🌍 Video Translator (Closed)", 'btn_search': "🔍 In-Video Search (Closed)",
        'choose_service': "Please select a service from the buttons below 👇",
        'send_video': "Send a video clip (MP4) or a TikTok/Reels link to remove music automatically.",
        'send_audio': "Send an audio file or voice note to remove noise and isolate the vocals.",
        'send_sum': "⚠️ Sorry, Summary service is currently unavailable.",
        'send_trans': "⚠️ Sorry, Translator service is currently unavailable.",
        'send_search': "⚠️ Sorry, In-Video Search is currently unavailable.",
        'processing': "⏳ Processing your request via advanced AI, please wait...",
        'success': "🚀 Processed successfully! Uploading your file...",
        'txt_link': "📥 Extracting and downloading video from link...",
        'txt_cut': "✂️ Video is too long, cutting first 3 minutes to protect server...",
        'txt_iso': "🎧 Isolating music using Demucs AI...",
        'txt_merge': "🎬 Remuxing audio and generating clean file...",
        'txt_clean': "🎵 Removing noise and music from voice note...",
        'err_too_large': f"⚠️ File too large (max {MAX_DOWNLOAD_MB}MB). Try a smaller file.",
        'err_link': "❌ Could not download the link. Make sure it's valid and public, then retry.",
        'err_generic': "❌ An error occurred while processing. Please try again with another file/link.",
        'err_timeout': "⏱️ Processing took too long and was cancelled. Try a shorter file.",
    }
}


def t(chat_id: int, key: str) -> str:
    lang = user_lang.get(chat_id, 'ar')
    return LANGUAGES[lang].get(key, LANGUAGES['ar'].get(key, key))


# ---------------------------------------------------------------------------
# طابور المعالجة
# ---------------------------------------------------------------------------

@dataclass
class Task:
    chat_id: int
    message_id: int
    mode: str            # "video" | "audio" | "link"
    source: str          # file_id تليجرام أو رابط
    file_ext: str = ""   # امتداد الملف الأصلي (فيديو/صوت/بصوتية)


task_queue: "queue.Queue[Optional[Task]]" = queue.Queue()


def safe_edit(chat_id: int, message_id: int, text: str) -> None:
    try:
        bot.edit_message_text(text, chat_id, message_id)
    except Exception as e:
        log.debug("edit_message_text failed (%s): %s", chat_id, e)


def safe_delete(chat_id: int, message_id: int) -> None:
    try:
        bot.delete_message(chat_id, message_id)
    except Exception:
        pass


def safe_send_message(chat_id: int, text: str, **kwargs) -> None:
    try:
        bot.send_message(chat_id, text, **kwargs)
    except Exception as e:
        log.warning("send_message failed (%s): %s", chat_id, e)


# ---------------------------------------------------------------------------
# أدوات ffmpeg / ffprobe / yt-dlp / demucs
# ---------------------------------------------------------------------------

def get_media_duration(path: str) -> Optional[float]:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=30, check=True,
        )
        return float(result.stdout.strip())
    except Exception as e:
        log.warning("ffprobe failed for %s: %s", path, e)
        return None


def trim_video(src: str, dst: str, seconds: int) -> None:
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", src, "-t", str(seconds), "-c", "copy", dst],
            capture_output=True, timeout=FFMPEG_TIMEOUT, check=True,
        )
        return
    except subprocess.CalledProcessError:
        log.info("stream-copy trim failed, falling back to re-encode")
    subprocess.run(
        ["ffmpeg", "-y", "-i", src, "-t", str(seconds),
         "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac", dst],
        capture_output=True, timeout=FFMPEG_TIMEOUT, check=True,
    )


def merge_video_audio(video_src: str, audio_src: str, dst: str) -> None:
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", video_src, "-i", audio_src,
             "-map", "0:v:0", "-map", "1:a:0",
             "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", dst],
            capture_output=True, timeout=FFMPEG_TIMEOUT, check=True,
        )
        return
    except subprocess.CalledProcessError:
        log.info("video stream copy failed on merge, re-encoding video")
    subprocess.run(
        ["ffmpeg", "-y", "-i", video_src, "-i", audio_src,
         "-map", "0:v:0", "-map", "1:a:0",
         "-c:v", "libx264", "-preset", "veryfast",
         "-c:a", "aac", "-b:a", "192k", "-shortest", dst],
        capture_output=True, timeout=FFMPEG_TIMEOUT, check=True,
    )


def download_from_link(url: str, dst_path: str) -> None:
    cmd = [
        "yt-dlp", "--no-playlist",
        "-f", "mp4/bestvideo+bestaudio/best",
        "--merge-output-format", "mp4",
        "--max-filesize", f"{MAX_DOWNLOAD_MB}M",
        "-o", dst_path, url,
    ]
    if os.path.exists("cookies.txt"):
        cmd[1:1] = ["--cookies", "cookies.txt"]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=YTDLP_TIMEOUT)
    if result.returncode != 0 or not os.path.exists(dst_path):
        raise RuntimeError(f"yt-dlp failed: {result.stderr[-500:]}")


def run_demucs(input_path: str, out_dir: str) -> None:
    cmd = [
        "demucs", "--two-stems=vocals",
        "-d", DEMUCS_DEVICE,
        "-j", "1",
        "--segment", DEMUCS_SEGMENT,
        "-o", out_dir,
        input_path,
    ]
    with demucs_semaphore:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=DEMUCS_TIMEOUT)
    if result.returncode != 0:
        raise RuntimeError(f"demucs failed: {result.stderr[-500:]}")


# ---------------------------------------------------------------------------
# تنفيذ المهمة
# ---------------------------------------------------------------------------

def execute_task(task: Task) -> None:
    task_id = uuid.uuid4().hex[:10]
    task_dir = os.path.join(WORK_DIR, task_id)
    os.makedirs(task_dir, exist_ok=True)
    demucs_out_dir = os.path.join(task_dir, "demucs_out")

    try:
        is_video_flow = task.mode in ("video", "link")

        if task.mode == "link":
            safe_edit(task.chat_id, task.message_id, t(task.chat_id, 'txt_link'))
            raw_path = os.path.join(task_dir, "source.mp4")
            download_from_link(task.source, raw_path)
        else:
            file_info = bot.get_file(task.source)
            if file_info.file_size and file_info.file_size > MAX_DOWNLOAD_MB * 1024 * 1024:
                raise RuntimeError("file_too_large")
            raw_bytes = bot.download_file(file_info.file_path)
            raw_path = os.path.join(task_dir, f"source{task.file_ext or ''}")
            with open(raw_path, "wb") as f:
                f.write(raw_bytes)

        working_media = raw_path

        if is_video_flow:
            duration = get_media_duration(raw_path)
            if duration and duration > MAX_VIDEO_SECONDS:
                safe_edit(task.chat_id, task.message_id, t(task.chat_id, 'txt_cut'))
                trimmed_path = os.path.join(task_dir, "trimmed.mp4")
                trim_video(raw_path, trimmed_path, MAX_VIDEO_SECONDS)
                working_media = trimmed_path

        safe_edit(
            task.chat_id, task.message_id,
            t(task.chat_id, 'txt_iso') if is_video_flow else t(task.chat_id, 'txt_clean'),
        )
        run_demucs(working_media, demucs_out_dir)

        base_name = os.path.splitext(os.path.basename(working_media))[0]
        vocals_path = os.path.join(demucs_out_dir, "htdemucs", base_name, "vocals.wav")
        if not os.path.exists(vocals_path):
            raise RuntimeError("demucs_no_output")

        lang = user_lang.get(task.chat_id, 'ar')

        if is_video_flow:
            safe_edit(task.chat_id, task.message_id, t(task.chat_id, 'txt_merge'))
            output_path = os.path.join(task_dir, "clean.mp4")
            merge_video_audio(working_media, vocals_path, output_path)
            safe_edit(task.chat_id, task.message_id, t(task.chat_id, 'success'))
            bot.send_chat_action(task.chat_id, 'upload_video')
            with open(output_path, 'rb') as f:
                bot.send_video(task.chat_id, f, reply_markup=main_keyboard(lang), timeout=120)
        else:
            safe_edit(task.chat_id, task.message_id, t(task.chat_id, 'success'))
            bot.send_chat_action(task.chat_id, 'upload_audio')
            with open(vocals_path, 'rb') as f:
                bot.send_audio(task.chat_id, f, reply_markup=main_keyboard(lang), timeout=120)

        safe_delete(task.chat_id, task.message_id)

    except subprocess.TimeoutExpired:
        log.warning("timeout processing task for %s", task.chat_id)
        safe_edit(task.chat_id, task.message_id, t(task.chat_id, 'err_timeout'))
    except RuntimeError as e:
        msg = str(e)
        log.warning("processing error for %s: %s", task.chat_id, msg)
        if msg == "file_too_large":
            safe_edit(task.chat_id, task.message_id, t(task.chat_id, 'err_too_large'))
        elif msg.startswith("yt-dlp failed"):
            safe_edit(task.chat_id, task.message_id, t(task.chat_id, 'err_link'))
        else:
            safe_edit(task.chat_id, task.message_id, t(task.chat_id, 'err_generic'))
    except Exception:
        log.exception("unexpected error processing task for %s", task.chat_id)
        safe_edit(task.chat_id, task.message_id, t(task.chat_id, 'err_generic'))
    finally:
        shutil.rmtree(task_dir, ignore_errors=True)


def process_queue() -> None:
    while True:
        task = task_queue.get()
        if task is None:
            break
        try:
            execute_task(task)
        except Exception:
            log.exception("worker crashed on task")
        finally:
            task_queue.task_done()


for _ in range(QUEUE_WORKERS):
    threading.Thread(target=process_queue, daemon=True).start()


# ---------------------------------------------------------------------------
# لوحات المفاتيح
# ---------------------------------------------------------------------------

def main_keyboard(lang: str):
    l = LANGUAGES.get(lang, LANGUAGES['ar'])
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(types.KeyboardButton(l['btn_video']), types.KeyboardButton(l['btn_audio']))
    return markup


# ---------------------------------------------------------------------------
# الحماية من السبام + حالة الصيانة
# ---------------------------------------------------------------------------

def check_spam_and_status(message) -> bool:
    uid = message.chat.id
    l = LANGUAGES[user_lang.get(uid, 'ar')]

    if is_permanently_banned(uid):
        safe_send_message(uid, l['spam_perm'])
        return False

    current_time = time.time()
    if uid in temp_banned_users:
        if current_time < temp_banned_users[uid]:
            safe_send_message(uid, l['spam_temp'])
            return False
        del temp_banned_users[uid]

    if maintenance_mode and uid != ADMIN_ID:
        safe_send_message(uid, l['maintenance'])
        return False

    if uid != ADMIN_ID:
        user_spam_counter.setdefault(uid, [])
        user_spam_counter[uid] = [x for x in user_spam_counter[uid] if current_time - x < 30]
        user_spam_counter[uid].append(current_time)

        if len(user_spam_counter[uid]) > 5:
            if uid not in warning_emitted:
                safe_send_message(uid, l['spam_warn'])
                warning_emitted[uid] = 1
                return False
            elif len(user_spam_counter[uid]) > 8:
                temp_banned_users[uid] = current_time + 600
                safe_send_message(uid, l['spam_temp'])
                return False
    return True


@bot.message_handler(commands=['start'])
def start_cmd(message):
    if not check_spam_and_status(message):
        return
    save_user(message.chat.id)
    
    if message.chat.id not in user_lang:
        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton("العربية 🇸🇦", callback_data="lang_ar"),
            types.InlineKeyboardButton("English 🇺🇸", callback_data="lang_en")
        )
        bot.send_message(message.chat.id, "🌐 Please choose your language / اختر لغتك:", reply_markup=markup)
    else:
        lang = user_lang[message.chat.id]
        l = LANGUAGES[lang]
        welcome = l['welcome']
        if message.chat.id == ADMIN_ID:
            welcome += l['admin_line']
        bot.send_message(message.chat.id, welcome, reply_markup=main_keyboard(lang))


@bot.callback_query_handler(func=lambda call: call.data.startswith("lang_"))
def set_language_callback(call):
    lang = call.data.split("_")[1]
    user_lang[call.message.chat.id] = lang
    safe_delete(call.message.chat.id, call.message.message_id)
    l = LANGUAGES[lang]
    welcome = l['welcome']
    if call.message.chat.id == ADMIN_ID:
        welcome += l['admin_line']
    bot.send_message(call.message.chat.id, welcome, reply_markup=main_keyboard(lang))


@bot.message_handler(commands=['admin'])
def admin_panel(message):
    if message.chat.id != ADMIN_ID:
        return
    count = get_users_count()
    disk = psutil.disk_usage('/')
    ram = psutil.virtual_memory()
    status_text = "🟢 شغال" if not maintenance_mode else "🔴 صيانة"
    
    panel_msg = (
        f"📊 **لوحة تحكم المطور**\n\n"
        f"👥 المستخدمين: `{count}`\n"
        f"🛠️ الوضع: **{status_text}**\n"
        f"💾 الرام: `{ram.percent}%`\n"
        f"📁 القرص: `{disk.percent}%`\n"
        f"⏳ الطابور: `{task_queue.qsize()}`"
    )
                
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("🔄 وضع الصيانة", callback_data="toggle_maint"),
        types.InlineKeyboardButton("📢 إذاعة متقدمة", callback_data="adv_broadcast"),
        types.InlineKeyboardButton("🚫 باند نهائي", callback_data="manual_ban")
    )
    bot.send_message(message.chat.id, panel_msg, reply_markup=markup, parse_mode="Markdown")


@bot.callback_query_handler(func=lambda call: call.data in ["toggle_maint", "adv_broadcast", "manual_ban"])
def admin_callbacks(call):
    global maintenance_mode
    bot.answer_callback_query(call.id)
    if call.message.chat.id != ADMIN_ID:
        return
    if call.data == "toggle_maint":
        maintenance_mode = not maintenance_mode
        safe_send_message(ADMIN_ID, "✅ تم تغيير حالة الصيانة")
    elif call.data == "adv_broadcast":
        msg = bot.send_message(ADMIN_ID, "أرسل الإذاعة:")
        bot.register_next_step_handler(msg, execute_advanced_broadcast)
    elif call.data == "manual_ban":
        msg = bot.send_message(ADMIN_ID, "أرسل ID المستخدم:")
        bot.register_next_step_handler(msg, execute_manual_ban)


def execute_advanced_broadcast(message):
    if message.chat.id != ADMIN_ID:
        return
    users = get_all_users()
    for u in users:
        try:
            bot.copy_message(int(u), ADMIN_ID, message.message_id)
        except:
            pass
    safe_send_message(ADMIN_ID, "✅ تم انتهاء الإذاعة")


def execute_manual_ban(message):
    if message.chat.id != ADMIN_ID:
        return
    target_id = message.text.strip()
    if target_id.isdigit():
        ban_user_permanently(int(target_id))
        safe_send_message(ADMIN_ID, f"✅ تم حظر `{target_id}`")


@bot.message_handler(func=lambda message: True)
def handle_text_menus(message):
    if not check_spam_and_status(message):
        return
    uid = message.chat.id
    lang = user_lang.get(uid, 'ar')
    l = LANGUAGES[lang]
    
    if message.text in [LANGUAGES['ar']['btn_video'], LANGUAGES['en']['btn_video']]:
        user_status[uid] = "video"
        bot.reply_to(message, l['send_video'])
    elif message.text in [LANGUAGES['ar']['btn_audio'], LANGUAGES['en']['btn_audio']]:
        user_status[uid] = "audio"
        bot.reply_to(message, l['send_audio'])
    elif message.text in [LANGUAGES['ar']['btn_summary'], LANGUAGES['en']['btn_summary']]:
        bot.reply_to(message, l['send_sum'])
    elif message.text in [LANGUAGES['ar']['btn_trans'], LANGUAGES['en']['btn_trans']]:
        bot.reply_to(message, l['send_trans'])
    elif message.text in [LANGUAGES['ar']['btn_search'], LANGUAGES['en']['btn_search']]:
        bot.reply_to(message, l['send_search'])
        
    elif message.text.startswith("http://") or message.text.startswith("https://"):
        user_status[uid] = "link"
        status_msg = bot.reply_to(message, l['processing'])
        task_queue.put(Task(chat_id=uid, message_id=status_msg.message_id, mode="link", source=message.text))


@bot.message_handler(content_types=['video', 'audio', 'voice'])
def handle_incoming_media(message):
    if not check_spam_and_status(message):
        return
    uid = message.chat.id
    lang = user_lang.get(uid, 'ar')
    l = LANGUAGES[lang]
    mode = user_status.get(uid, None)
    
    if not mode or mode in ["summary", "translate", "search_init", "search_keyword"]:
        bot.reply_to(message, l['choose_service'], reply_markup=main_keyboard(lang))
        return

    file_id = ""
    file_ext = ""
    if message.content_type == 'video':
        file_id = message.video.file_id
        file_ext = ".mp4"
    elif message.content_type == 'audio':
        file_id = message.audio.file_id
        file_ext = ".mp3"
    else:
        file_id = message.voice.file_id
        file_ext = ".ogg"

    q_size = task_queue.qsize()
    status_msg = bot.reply_to(message, f"{l['processing']} (# {q_size + 1})")
    task_queue.put(Task(chat_id=uid, message_id=status_msg.message_id, mode=mode, source=file_id, file_ext=file_ext))


# ---------------------------------------------------------------------------
# سيرفر Flask لإبقاء الخدمة نشطة
# ---------------------------------------------------------------------------

app = Flask(__name__)

@app.route('/')
def health_check():
    return "Bot is alive and running ✅", 200

def run_flask_server():
    app.run(host="0.0.0.0", port=8000)

if __name__ == "__main__":
    flask_thread = threading.Thread(target=run_flask_server, daemon=True)
    flask_thread.start()

    log.info("🚀 Ready!")
    bot.infinity_polling()
