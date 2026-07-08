# -*- coding: utf-8 -*-
"""
بوت تليجرام لتحميل/قص الفيديوهات وتنقية الصوت — نسخة خفيفة مخصصة لخطط الاستضافة المجانية محدودة الرام.
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

import telebot
from telebot import types
from flask import Flask

# ---------------------------------------------------------------------------
# المتغيرات الثابتة والمحددة مسبقاً
# ---------------------------------------------------------------------------

BOT_TOKEN = "8817595725:AAHFUP7oTN0Km177V9KXSe6Pt-1gQ8HCr5k"
ADMIN_ID = 8084323446

WORK_DIR = os.environ.get("WORK_DIR", "workdir")
MAX_TRIM_SECONDS = int(os.environ.get("MAX_TRIM_SECONDS", "60"))
MAX_DOWNLOAD_MB = int(os.environ.get("MAX_DOWNLOAD_MB", "50"))
QUEUE_WORKERS = int(os.environ.get("QUEUE_WORKERS", "1"))
YTDLP_TIMEOUT = int(os.environ.get("YTDLP_TIMEOUT", "180"))
FFMPEG_TIMEOUT = int(os.environ.get("FFMPEG_TIMEOUT", "120"))

os.makedirs(WORK_DIR, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("light_bot")

bot = telebot.TeleBot(BOT_TOKEN, threaded=True)

# ---------------------------------------------------------------------------
# النصوص
# ---------------------------------------------------------------------------

TXT = {
    "welcome": (
        "أهلاً بك! 🚀\n\n"
        "🎬 أرسل رابط فيديو (يوتيوب/تيك توك/إلخ) وسأحمّله وأقصّ أول "
        f"{MAX_TRIM_SECONDS} ثانية منه.\n"
        "🎵 أو أرسل ملف صوت/فيديو/تسجيل وسأستخرج الصوت منه وأنقّيه من الضجيج.\n\n"
        "اختر الخدمة من الأزرار بالأسفل ثم أرسل الملف أو الرابط."
    ),
    "btn_link": "🎬 تحميل وقص من رابط",
    "btn_audio": "🎵 استخراج وتنقية صوت",
    "choose_first": "الرجاء اختيار نوع الخدمة أولاً من الأزرار بالأسفل 👇",
    "send_link": "أرسل الآن رابط الفيديو (يوتيوب/تيك توك/رابط عام).",
    "send_media": "أرسل الآن ملف صوت أو فيديو أو تسجيل صوتي.",
    "processing": "⏳ جاري معالجة طلبك، يرجى الانتظار...",
    "queued": "⏳ طلبك بالطابور، دورك رقم {n}...",
    "txt_download": "📥 جاري تحميل الفيديو من الرابط...",
    "txt_trim": "✂️ جاري قص المقطع...",
    "txt_extract": "🎧 جاري استخراج الصوت وتنقيته من الضجيج...",
    "success": "✅ تم! جاري رفع الملف...",
    "err_too_large": f"⚠️ حجم الملف أكبر من الحد المسموح ({MAX_DOWNLOAD_MB}MB). جرّب ملفاً أصغر.",
    "err_link": "❌ تعذّر تحميل الرابط. تأكد أنه صحيح وعام (غير خاص) وحاول برابط آخر.",
    "err_timeout": "⏱️ استغرقت المعالجة وقتاً طويلاً وتم إلغاؤها. جرّب ملفاً/رابطاً أقصر.",
    "err_generic": "❌ تعذّر معالجة هذا الطلب، حاول مجدداً أو جرّب ملفاً/رابطاً آخر.",
}


def main_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(types.KeyboardButton(TXT["btn_link"]), types.KeyboardButton(TXT["btn_audio"]))
    return markup


# ---------------------------------------------------------------------------
# حالة المستخدمين
# ---------------------------------------------------------------------------

user_mode = {}  # chat_id -> "link" | "audio"

# ---------------------------------------------------------------------------
# طابور المعالجة
# ---------------------------------------------------------------------------

@dataclass
class Task:
    chat_id: int
    message_id: int
    mode: str
    source: str
    file_ext: str = ""


task_queue: "queue.Queue[Optional[Task]]" = queue.Queue()


def safe_edit(chat_id: int, message_id: int, text: str) -> None:
    try:
        bot.edit_message_text(text, chat_id, message_id)
    except Exception as e:
        log.debug("edit_message_text failed: %s", e)


def safe_delete(chat_id: int, message_id: int) -> None:
    try:
        bot.delete_message(chat_id, message_id)
    except Exception:
        pass


def safe_reply(message, text: str, **kwargs):
    try:
        return bot.reply_to(message, text, **kwargs)
    except Exception as e:
        log.warning("reply failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# أدوات ffmpeg / ffprobe / yt-dlp
# ---------------------------------------------------------------------------

def get_media_duration(path: str) -> Optional[float]:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=20, check=True,
        )
        return float(result.stdout.strip())
    except Exception as e:
        log.warning("ffprobe failed: %s", e)
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


def extract_and_clean_audio(src: str, dst_mp3: str, seconds: int) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-i", src, "-t", str(seconds),
         "-vn", "-af", "afftdn=nf=-25",
         "-ac", "2", "-ar", "44100", "-b:a", "128k", dst_mp3],
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
        raise RuntimeError(f"yt-dlp failed: {(result.stderr or '')[-500:]}")


# ---------------------------------------------------------------------------
# تنفيذ المهمة
# ---------------------------------------------------------------------------

def execute_task(task: Task) -> None:
    task_id = uuid.uuid4().hex[:10]
    task_dir = os.path.join(WORK_DIR, task_id)
    os.makedirs(task_dir, exist_ok=True)

    try:
        if task.mode == "link":
            safe_edit(task.chat_id, task.message_id, TXT["txt_download"])
            raw_path = os.path.join(task_dir, "source.mp4")
            try:
                download_from_link(task.source, raw_path)
            except subprocess.TimeoutExpired:
                raise RuntimeError("timeout")
            except Exception as e:
                raise RuntimeError(f"yt-dlp failed: {e}")

            duration = get_media_duration(raw_path)
            output_path = raw_path
            if duration and duration > MAX_TRIM_SECONDS:
                safe_edit(task.chat_id, task.message_id, TXT["txt_trim"])
                trimmed_path = os.path.join(task_dir, "trimmed.mp4")
                trim_video(raw_path, trimmed_path, MAX_TRIM_SECONDS)
                output_path = trimmed_path

            safe_edit(task.chat_id, task.message_id, TXT["success"])
            with open(output_path, "rb") as f:
                bot.send_video(task.chat_id, f, reply_markup=main_keyboard(), timeout=120)

        elif task.mode == "audio":
            file_info = bot.get_file(task.source)
            if file_info.file_size and file_info.file_size > MAX_DOWNLOAD_MB * 1024 * 1024:
                raise RuntimeError("file_too_large")
            raw_bytes = bot.download_file(file_info.file_path)
            raw_path = os.path.join(task_dir, f"source{task.file_ext or ''}")
            with open(raw_path, "wb") as f:
                f.write(raw_bytes)

            safe_edit(task.chat_id, task.message_id, TXT["txt_extract"])
            output_path = os.path.join(task_dir, "clean.mp3")
            extract_and_clean_audio(raw_path, output_path, MAX_TRIM_SECONDS)

            safe_edit(task.chat_id, task.message_id, TXT["success"])
            with open(output_path, "rb") as f:
                bot.send_audio(task.chat_id, f, reply_markup=main_keyboard(), timeout=120)

        else:
            raise RuntimeError("unknown_mode")

        safe_delete(task.chat_id, task.message_id)

    except subprocess.TimeoutExpired:
        log.warning("timeout processing task for chat %s", task.chat_id)
        safe_edit(task.chat_id, task.message_id, TXT["err_timeout"])
    except RuntimeError as e:
        msg = str(e)
        log.warning("processing error for chat %s: %s", task.chat_id, msg)
        if msg == "file_too_large":
            safe_edit(task.chat_id, task.message_id, TXT["err_too_large"])
        elif msg == "timeout":
            safe_edit(task.chat_id, task.message_id, TXT["err_timeout"])
        elif msg.startswith("yt-dlp failed"):
            safe_edit(task.chat_id, task.message_id, TXT["err_link"])
        else:
            safe_edit(task.chat_id, task.message_id, TXT["err_generic"])
    except Exception:
        log.exception("unexpected error processing task for chat %s", task.chat_id)
        safe_edit(task.chat_id, task.message_id, TXT["err_generic"])
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
            log.exception("worker crashed unexpectedly on a task")
        finally:
            task_queue.task_done()


for _ in range(QUEUE_WORKERS):
    threading.Thread(target=process_queue, daemon=True).start()


# ---------------------------------------------------------------------------
# أوامر ومعالجات تيليجرام
# ---------------------------------------------------------------------------

@bot.message_handler(commands=["start"])
def start_cmd(message):
    try:
        bot.send_message(message.chat.id, TXT["welcome"], reply_markup=main_keyboard())
    except Exception:
        log.exception("start_cmd failed")


@bot.message_handler(commands=["status"])
def status_cmd(message):
    try:
        if message.chat.id != ADMIN_ID:
            return
        bot.send_message(
            message.chat.id,
            f"📊 حالة البوت\nالطابور الحالي: {task_queue.qsize()}\nعمّال المعالجة: {QUEUE_WORKERS}",
        )
    except Exception:
        log.exception("status_cmd failed")


@bot.message_handler(func=lambda m: True, content_types=["text"])
def handle_text(message):
    try:
        uid = message.chat.id
        text = (message.text or "").strip()

        if text == TXT["btn_link"]:
            user_mode[uid] = "link"
            safe_reply(message, TXT["send_link"])
            return

        if text == TXT["btn_audio"]:
            user_mode[uid] = "audio"
            safe_reply(message, TXT["send_media"])
            return

        if text.startswith("http://") or text.startswith("https://"):
            user_mode[uid] = "link"
            status_msg = safe_reply(message, TXT["processing"])
            if status_msg is None:
                return
            task_queue.put(Task(chat_id=uid, message_id=status_msg.message_id, mode="link", source=text))
            return

        safe_reply(message, TXT["choose_first"], reply_markup=main_keyboard())

    except Exception:
        log.exception("handle_text failed")
        try:
            safe_reply(message, TXT["err_generic"])
        except Exception:
            pass


@bot.message_handler(content_types=["video", "audio", "voice"])
def handle_media(message):
    try:
        uid = message.chat.id
        mode = user_mode.get(uid)

        if mode != "audio":
            mode = "audio"

        if message.content_type == "video":
            source, file_ext = message.video.file_id, ".mp4"
        elif message.content_type == "audio":
            source, file_ext = message.audio.file_id, ".mp3"
        else:
            source, file_ext = message.voice.file_id, ".ogg"

        q_size = task_queue.qsize()
        status_msg = safe_reply(message, TXT["queued"].format(n=q_size + 1))
        if status_msg is None:
            return
        task_queue.put(Task(chat_id=uid, message_id=status_msg.message_id, mode=mode, source=source, file_ext=file_ext))

    except Exception:
        log.exception("handle_media failed")
        try:
            safe_reply(message, TXT["err_generic"])
        except Exception:
            pass


# ---------------------------------------------------------------------------
# سيرفر Flask (Health Check)
# ---------------------------------------------------------------------------

app = Flask(__name__)


@app.route("/")
def health_check():
    return "Bot is alive and running ✅", 200


def run_flask_server():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))


# ---------------------------------------------------------------------------
# التشغيل الرئيسي
# ---------------------------------------------------------------------------

def run_bot_forever():
    while True:
        try:
            log.info("🚀 Bot polling started (queue_workers=%s)", QUEUE_WORKERS)
            bot.infinity_polling(timeout=60, long_polling_timeout=60)
        except Exception:
            log.exception("polling crashed, restarting in 5 seconds...")
            time.sleep(5)


if __name__ == "__main__":
    flask_thread = threading.Thread(target=run_flask_server, daemon=True)
    flask_thread.start()
    run_bot_forever()
