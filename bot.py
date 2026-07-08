# 1. تثبيت كافة المكتبات الأساسية والمتقدمة

import telebot
import subprocess
import os
import queue
import threading
import time
import psutil
from flask import Flask
from telebot import types
from moviepy import VideoFileClip, AudioFileClip

BOT_TOKEN = "8817595725:AAHJaK-h187XPh51ToF_20oTIQ_nkzV2mgQ"
ADMIN_ID = 8084323446

bot = telebot.TeleBot(BOT_TOKEN)

USERS_FILE = "bot_users.txt"
BANNED_FILE = "banned_users.txt"
user_status = {}
user_lang = {}
maintenance_mode = False

user_spam_counter = {}
temp_banned_users = {}
warning_emitted = {}

for file in [USERS_FILE, BANNED_FILE]:
    if not os.path.exists(file):
        with open(file, "w") as f: pass

def save_user(user_id):
    with open(USERS_FILE, "r+") as f:
        users = f.read().splitlines()
        if str(user_id) not in users: f.write(str(user_id) + "\n")

def get_users_count():
    with open(USERS_FILE, "r") as f: return len(f.read().splitlines())

def ban_user_permanently(user_id):
    with open(BANNED_FILE, "r+") as f:
        banned = f.read().splitlines()
        if str(user_id) not in banned: f.write(str(user_id) + "\n")

def is_permanently_banned(user_id):
    with open(BANNED_FILE, "r") as f: return str(user_id) in f.read().splitlines()

# تم جمع كافة النصوص والإيموجيات هنا لحماية أسطر الدالة التكرارية من البتر
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
        'txt_iso': "🎧 جاري عزل وعزل الموسيقى بالذكاء الاصطناعي (Demucs)...",
        'txt_merge': "🎬 جاري إعادة الدمج الصوتي وإنتاج الملف النظيف...",
        'txt_clean': "🎵 جاري عزل الموسيقى وتنقية الريكورد من الشوشة المحيطة...",
        'txt_sum': "📝 الخدمة متوقفة مؤقتاً...",
        'txt_trans': "🌍 الخدمة متوقفة مؤقتاً...",
        'txt_search': "📥 الخدمة متوقفة مؤقتاً..."
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
        'txt_sum': "📝 Service unavailable...",
        'txt_trans': "🌍 Service unavailable...",
        'txt_search': "🔍 Service unavailable..."
    }
}

task_queue = queue.Queue()

def process_queue():
    while True:
        task = task_queue.get()
        if task is None: break
        chat_id, message_id, file_id, mode, file_name, extra_data = task
        execute_processing(chat_id, message_id, file_id, mode, file_name, extra_data)
        task_queue.task_done()

for _ in range(2):
    threading.Thread(target=process_queue, daemon=True).start()

def main_keyboard(lang):
    l = LANGUAGES.get(lang, LANGUAGES['ar'])
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(
        types.KeyboardButton(l['btn_video']), types.KeyboardButton(l['btn_audio'])
    )
    return markup

def check_spam_and_status(message):
    uid = message.chat.id
    lang = user_lang.get(uid, 'ar')
    l = LANGUAGES[lang]
    
    if is_permanently_banned(uid):
        bot.send_message(uid, l['spam_perm'])
        return False
        
    current_time = time.time()
    if uid in temp_banned_users:
        if current_time < temp_banned_users[uid]:
            bot.send_message(uid, f"{l['spam_temp']}")
            return False
        else:
            del temp_banned_users[uid]
            
    if maintenance_mode and uid != ADMIN_ID:
        bot.send_message(uid, l['maintenance'])
        return False

    if uid != ADMIN_ID:
        if uid not in user_spam_counter:
            user_spam_counter[uid] = []
        user_spam_counter[uid] = [t for t in user_spam_counter[uid] if current_time - t < 30]
        user_spam_counter[uid].append(current_time)
        
        if len(user_spam_counter[uid]) > 5:
            if uid not in warning_emitted:
                bot.send_message(uid, l['spam_warn'])
                warning_emitted[uid] = 1
                return False
            elif len(user_spam_counter[uid]) > 8:
                temp_banned_users[uid] = current_time + 600
                bot.send_message(uid, l['spam_temp'])
                return False
    return True

@bot.message_handler(commands=['start'])
def start_cmd(message):
    if not check_spam_and_status(message): return
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
        if message.chat.id == ADMIN_ID: welcome += l['admin_line']
        bot.send_message(message.chat.id, welcome, reply_markup=main_keyboard(lang))

@bot.callback_query_handler(func=lambda call: call.data.startswith("lang_"))
def set_language_callback(call):
    lang = call.data.split("_")[1]
    user_lang[call.message.chat.id] = lang
    bot.delete_message(call.message.chat.id, call.message.message_id)
    l = LANGUAGES[lang]
    welcome = l['welcome']
    if call.message.chat.id == ADMIN_ID: welcome += l['admin_line']
    bot.send_message(call.message.chat.id, welcome, reply_markup=main_keyboard(lang))

@bot.message_handler(commands=['admin'])
def admin_panel(message):
    if message.chat.id != ADMIN_ID: return
    count = get_users_count()
    disk = psutil.disk_usage('/')
    ram = psutil.virtual_memory()
    status_text = "🟢 شغال" if not maintenance_mode else "🔴 صيانة"
    
    panel_msg = f"📊 **لوحة تحكم المطور**\n\n" \
                f"👥 المستخدمين: `{count}`\n" \
                f"🛠️ الوضع: **{status_text}**\n" \
                f"💾 الرام: `{ram.percent}%`\n" \
                f"📁 القرص: `{disk.percent}%`\n" \
                f"⏳ الطابور: `{task_queue.qsize()}`"
                
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
    if call.message.chat.id != ADMIN_ID: return
    if call.data == "toggle_maint":
        maintenance_mode = not maintenance_mode
        bot.send_message(ADMIN_ID, "✅ تم تغيير حالة الصيانة")
    elif call.data == "adv_broadcast":
        msg = bot.send_message(ADMIN_ID, "أرسل الإذاعة:")
        bot.register_next_step_handler(msg, execute_advanced_broadcast)
    elif call.data == "manual_ban":
        msg = bot.send_message(ADMIN_ID, "أرسل ID المستخدم:")
        bot.register_next_step_handler(msg, execute_manual_ban)

def execute_advanced_broadcast(message):
    if message.chat.id != ADMIN_ID: return
    with open(USERS_FILE, "r") as f: users = f.read().splitlines()
    for u in users:
        try: bot.copy_message(int(u), ADMIN_ID, message.message_id)
        except: pass
    bot.send_message(ADMIN_ID, "✅ تم انتهاء الإذاعة")

def execute_manual_ban(message):
    if message.chat.id != ADMIN_ID: return
    target_id = message.text.strip()
    if target_id.isdigit():
        ban_user_permanently(int(target_id))
        bot.send_message(ADMIN_ID, f"✅ تم حظر `{target_id}`")

@bot.message_handler(func=lambda message: True)
def handle_text_menus(message):
    if not check_spam_and_status(message): return
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
        user_status[uid] = "video"
        status_msg = bot.reply_to(message, l['processing'])
        file_name = f"link_{uid}.mp4"
        task_queue.put((uid, status_msg.message_id, message.text, "link_download", file_name, ""))

@bot.message_handler(content_types=['video', 'audio', 'voice'])
def handle_incoming_media(message):
    if not check_spam_and_status(message): return
    uid = message.chat.id
    lang = user_lang.get(uid, 'ar')
    l = LANGUAGES[lang]
    mode = user_status.get(uid, None)
    
    if not mode or mode in ["summary", "translate", "search_init", "search_keyword"]:
        bot.reply_to(message, l['choose_service'], reply_markup=main_keyboard(lang))
        return

    if message.content_type == 'video':
        file_id = message.video.file_id
        file_name = f"video_{uid}.mp4"
    elif message.content_type == 'audio':
        file_id = message.audio.file_id
        file_name = f"audio_{uid}.mp3"
    else:
        file_id = message.voice.file_id
        file_name = f"voice_{uid}.ogg"

    q_size = task_queue.qsize()
    status_msg = bot.reply_to(message, f"{l['processing']} (# {q_size + 1})")
    task_queue.put((uid, status_msg.message_id, file_id, mode, file_name, ""))

def execute_processing(chat_id, message_id, file_id, mode, file_name, extra_data):
    video_clip, audio_clip, final_clip = None, None, None
    lang = user_lang.get(chat_id, 'ar')
    l = LANGUAGES[lang]
    try:
        if mode == "link_download":
            bot.edit_message_text(l['txt_link'], chat_id, message_id)
            with subprocess.Popen(f"yt-dlp {file_id} -o {file_name} --merge-output-format mp4", shell=True) as proc: proc.wait()
            mode = "video"
        else:
            file_info = bot.get_file(file_id)
            downloaded_file = bot.download_file(file_info.file_path)
            with open(file_name, 'wb') as f: f.write(downloaded_file)

        if mode == "video" and os.path.exists(file_name):
            video_clip = VideoFileClip(file_name)
            if video_clip.duration > 180:
                bot.edit_message_text(l['txt_cut'], chat_id, message_id)
                truncated_video = f"cut_{file_name}"
                video_clip.subclip(0, 180).write_videofile(truncated_video, codec="libx264", logger=None)
                video_clip.close()
                os.remove(file_name)
                os.rename(truncated_video, file_name)
                video_clip = VideoFileClip(file_name)

        if mode == "video":
            bot.edit_message_text(l['txt_iso'], chat_id, message_id)
            subprocess.run(["demucs", "--two-stems=vocals", "-o", "./output", file_name], check=True)
            base_name = os.path.splitext(file_name)[0]
            vocals_path = os.path.join("./output", "htdemucs", base_name, "vocals.wav")
            
            if os.path.exists(vocals_path):
                bot.edit_message_text(l['txt_merge'], chat_id, message_id)
                output_video = f"clean_{chat_id}.mp4"
                audio_clip = AudioFileClip(vocals_path)
                final_clip = video_clip.set_audio(audio_clip)
                final_clip.write_videofile(output_video, codec="libx264", audio_codec="aac", bitrate="1500k", logger=None)
                
                bot.edit_message_text(l['success'], chat_id, message_id)
                with open(output_video, 'rb') as video_file: bot.send_video(chat_id, video_file, reply_markup=main_keyboard(lang))
                if os.path.exists(output_video): os.remove(output_video)

        elif mode == "audio":
            bot.edit_message_text(l['txt_clean'], chat_id, message_id)
            subprocess.run(["demucs", "--two-stems=vocals", "-o", "./output", file_name], check=True)
            base_name = os.path.splitext(file_name)[0]
            vocals_path = os.path.join("./output", "htdemucs", base_name, "vocals.wav")
            if os.path.exists(vocals_path):
                bot.edit_message_text(l['success'], chat_id, message_id)
                with open(vocals_path, 'rb') as audio_file: bot.send_audio(chat_id, audio_file, reply_markup=main_keyboard(lang))
            
        bot.delete_message(chat_id, message_id)
        
    except Exception as e:
        print("Error:", e)
        bot.edit_message_text("❌ Error", chat_id, message_id)
    finally:
        if video_clip: video_clip.close()
        if audio_clip: audio_clip.close()
        if final_clip: final_clip.close()
        if os.path.exists(file_name): os.remove(file_name)


app = Flask(__name__)

@app.route('/')
def health_check():
    return "Bot is alive and running ✅", 200

def run_flask_server():
    app.run(host="0.0.0.0", port=8000)

if __name__ == "__main__":
    flask_thread = threading.Thread(target=run_flask_server, daemon=True)
    flask_thread.start()

    print("🚀 Ready!")
    bot.infinity_polling()
