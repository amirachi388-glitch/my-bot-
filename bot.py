# -*- coding: utf-8 -*-
"""
بوت تليجرام لمعالجة وتنقية الصوت من الملفات المرسلة فقط — نسخة خفيفة ومستقرة لخطط الاستضافة المجانية.
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
FFMPEG_TIMEOUT = int(os.environ.get("FFMPEG_TIMEOUT", "120"))

os.makedirs(WORK_DIR, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("media_bot")

bot = telebot.TeleBot(BOT_TOKEN, threaded=True)

# ---------------------------------------------------------------------------
# النصوص
# ---------------------------------------------------------------------------

TXT = {
    "welcome": (
        "أهلاً بك! 🚀\n\n"
        "📁 أرسل مباشرة أي ملف فيديو أو صوت أو تسجيل من هاتفك، وسأقوم بمعالجته واستخراج/تنقية الصوت منه فوراً وبدون أي مشاكل!"
    ),
    "processing": "⏳ جاري معالجة الملف، يرجى الانتظار...",
    "queued": "⏳ طلبك بالطابور، دورك رقم {n}...",
    "txt_extract": "🎧 جاري معالجة واستخراج الصوت...",
    "success": "✅ تم! جاري إرسال الملف...",
    "err_too_large": f"⚠️ حجم الملف أكبر من الحد المسموح ({MAX_DOWNLOAD_MB}MB). جرّب ملفاً أصغر.",
    "err_timeout": "⏱️ استغرقت المعالجة وقتاً طويلاً وتم إلغاؤها. جرّب ملفاً أقصر.",
    "err_generic": "❌ تعذّر معالجة هذا الملف، حاول مجدداً أو جرّب ملفاً آخر.",
}


# ---------------------------------------------------------------------------
# طابور المعالجة
# ---------------------------------------------------------------------------

@dataclass
class Task:
    chat_id: int
    message_id: int
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
# أدوات المعالجة الصوتية عبر ffmpeg
# ---------------------------------------------------------------------------

def extract_and_clean_audio(src: str, dst_mp3: str, seconds: int) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-i", src, "-t", str(seconds),
         "-vn", "-af", "afftdn=nf=-25",
         "-ac", "2", "-ar", "44100", "-b:a", "128k", dst_mp3],
        capture_output=True, timeout=FFMPEG_TIMEOUT, check=True,
    )


# ---------------------------------------------------------------------------
# تنفيذ المهمة للملفات المباشرة فقط
# ---------------------------------------------------------------------------

def execute_task(task: Task) -> None:
    task_id = uuid.uuid4().hex[:10]
    task_dir = os.path.join(WORK_DIR, task_id)
    os.makedirs(task_dir, exist_ok=True)

    try:
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
            bot.send_audio(task.chat_id, f, timeout=120)

        safe_delete(task.chat_id, task.message_id)

    except subprocess.TimeoutExpired:
        log.warning("timeout processing task for chat %s", task.chat_id)
        safe_edit(task.chat_id, task.message_id, TXT["err_timeout"])
    except RuntimeError as e:
        msg = str(e)
        log.warning("processing error for chat %s: %s", task.chat_id, msg)
        if msg == "file_too_large":
            safe_edit(task.chat_id, task.message_id, TXT["err_too_large"])
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
        bot.send_message(message.chat.id, TXT["welcome"])
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
        safe_reply(message, "أرسل ملف فيديو أو صوت أو تسجيل من هاتفك مباشرة لمعالجته.")
    except Exception:
        pass


@bot.message_handler(content_types=["video", "audio", "voice", "document"])
def handle_media(message):
    try:
        uid = message.chat.id

        if message.content_type == "video":
            source, file_ext = message.video.file_id, ".mp4"
        elif message.content_type == "audio":
            source, file_ext = message.audio.file_id, ".mp3"
        elif message.content_type == "voice":
            source, file_ext = message.voice.file_id, ".ogg"
        else:
            source, file_ext = message.document.file_id, ".dat"

        q_size = task_queue.qsize()
        status_msg = safe_reply(message, TXT["queued"].format(n=q_size + 1))
        if status_msg is None:
            return
        task_queue.put(Task(chat_id=uid, message_id=status_msg.message_id, source=source, file_ext=file_ext))

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
