# -*- coding: utf-8 -*-
import logging
import os
import sys
import json
from datetime import datetime, timedelta

# استيراد مكونات تيليجرام
from telegram import Update, ChatMember
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# استيراد مكونات Firebase
from firebase_admin import initialize_app, firestore, credentials
from google import genai 

# ----------------------------------------------------
# 1. إعدادات البوت والـ Token
# ----------------------------------------------------
# إعدادات متغيرات البيئة (Environmental Variables)
PORT = int(os.environ.get('PORT', 8080))
# يتم قراءة مفتاح البوت من متغير BOT_TOKEN
BOT_TOKEN = os.environ.get("BOT_TOKEN", "7961349865:AAHXldLZwaL2BC5BANBCXcD4p4VEYRtFOL4") 

# مفتاح Gemini API (يجب توفيره كمتغير بيئة)
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "") 

# ----------------------------------------------------
# 2. إعداد Logging
# ----------------------------------------------------
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    stream=sys.stdout
)
logger = logging.getLogger(__name__)

# ----------------------------------------------------
# 3. إعداد Firebase/Firestore و Gemini (محاكاة أو تهيئة)
# ----------------------------------------------------
# يتم تهيئة Firebase بطريقة آمنة في بيئات مثل Render باستخدام مفتاح الخدمة
try:
    # لبيئة Canvas أو Render، قد نحتاج إلى استخدام Default Credentials
    cred = credentials.ApplicationDefault()
    initialize_app(cred)
    db = firestore.client()
    logger.info("تم تهيئة Firestore بنجاح.")
except Exception as e:
    logger.warning(f"فشل تهيئة Firebase. سيتم العمل بدون قاعدة بيانات. {e}")
    db = None 
    

# تهيئة Gemini
try:
    if not GEMINI_API_KEY:
        raise ValueError("لم يتم توفير مفتاح GEMINI_API_KEY.")
        
    gemini_client = genai.Client(api_key=GEMINI_API_KEY)
    
    # تحديد التعليمات البرمجية لنظام Gemini
    SYSTEM_PROMPT = (
        "أنت بوت مسابقات تفاعلي متخصص في إنشاء أسئلة ثقافة عامة باللغة العربية. "
        "عندما تتلقى طلباً لإنشاء سؤال، يجب أن تعيد الرد بصيغة JSON صارمة (Strict JSON) تحتوي على مفتاحين فقط: "
        "'question' للسؤال، و 'answer' للإجابة الصحيحة. "
        "مثال: {'question': 'ما هو أطول نهر في العالم؟', 'answer': 'نهر النيل'}"
    )
    
except Exception as e:
    logger.error(f"فشل تهيئة Gemini API: {e}")
    gemini_client = None
    logger.warning("البوت سيعمل بدون توليد أسئلة (فقط محاكاة).")

# ----------------------------------------------------
# 4. دالة توليد الأسئلة بواسطة Gemini
# ----------------------------------------------------

async def generate_quiz_question(prompt: str) -> tuple[str, str] | None:
    """تتواصل مع Gemini API لتوليد سؤال وإجابته الصحيحة."""
    if not gemini_client:
        return "ما هو أكبر كوكب في مجموعتنا الشمسية؟", "المشتري" # سؤال محاكاة
        
    try:
        
        response = gemini_client.models.generate_content(
            model='gemini-2.5-flash-preview-09-2025',
            contents=[
                {"role": "user", "parts": [{"text": "أنشئ سؤال ثقافة عامة جديد باللغة العربية ومناسب للمسابقات."}]},
            ],
            config=genai.types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                response_mime_type="application/json",
                response_schema={
                    "type": "OBJECT",
                    "properties": {
                        "question": {"type": "STRING", "description": "سؤال المسابقة باللغة العربية."},
                        "answer": {"type": "STRING", "description": "الإجابة الصحيحة للسؤال."},
                    }
                }
            )
        )
        
        # تحليل استجابة JSON
        json_text = response.text.strip()
        data = json.loads(json_text)
        
        return data.get("question"), data.get("answer")

    except Exception as e:
        logger.error(f"خطأ في توليد السؤال من Gemini: {e}")
        return "ما هي عاصمة اليابان؟", "طوكيو" # سؤال احتياطي

# ----------------------------------------------------
# 5. دوال Firebase (لتفاعل المسابقة)
# ----------------------------------------------------

# (سنفترض وجود __app_id في بيئة التشغيل، لكن للتشغيل المحلي نستخدم 'quiz-app')
APP_ID = os.environ.get('__app_id', 'quiz-app')

def get_quiz_ref(chat_id: int):
    """الحصول على مرجع المستند الخاص بمسابقة المجموعة."""
    if not db:
        return None
    # المسار: /artifacts/{appId}/public/data/quizzes/{chat_id}
    return db.collection('artifacts').document(APP_ID).collection('public').document('data').collection('quizzes').document(str(chat_id))

def get_leaderboard_ref(chat_id: int):
    """الحصول على مرجع مجموعة النقاط للاعبين."""
    if not db:
        return None
    # المسار: /artifacts/{appId}/public/data/leaderboards/{chat_id}/scores
    return db.collection('artifacts').document(APP_ID).collection('public').document('data').collection('leaderboards').document(str(chat_id)).collection('scores')


async def get_score(chat_id: int, user_id: int, db_ref) -> int:
    """الحصول على نقاط لاعب معين."""
    if not db_ref:
        return 0
    try:
        score_doc = db_ref.document(str(user_id)).get()
        return score_doc.get('score') if score_doc.exists else 0
    except Exception as e:
        logger.error(f"خطأ في جلب النقاط: {e}")
        return 0

async def update_score(chat_id: int, user_id: int, username: str, points: int, db_ref) -> None:
    """تحديث نقاط لاعب معين في قاعدة البيانات."""
    if not db_ref:
        return
    try:
        current_score = await get_score(chat_id, user_id, db_ref)
        new_score = current_score + points
        
        db_ref.document(str(user_id)).set({
            'user_id': user_id,
            'username': username,
            'score': new_score,
            'last_updated': firestore.SERVER_TIMESTAMP
        }, merge=True)
        logger.info(f"تم تحديث نقاط اللاعب {username} إلى {new_score}")
    except Exception as e:
        logger.error(f"خطأ في تحديث النقاط: {e}")


# ----------------------------------------------------
# 6. معالجات الأوامر (Handlers)
# ----------------------------------------------------

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """رسالة الترحيب."""
    await update.message.reply_text(
        "مرحباً! أنا بوت مسابقات المعلومات الفورية. 🧠\n"
        "ابدأ مسابقة جديدة في أي مجموعة عن طريق الأمر `/newquiz`.\n"
        "أو استخدم الأمر `/score` لمعرفة لوحة المتصدرين."
    )

async def new_quiz_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """بدء مسابقة جديدة أو جولة جديدة في المجموعة."""
    
    # التأكد من أن الأمر يعمل في مجموعة (Group)
    if update.effective_chat.type not in [ChatMember.GROUP, ChatMember.SUPERGROUP]:
        await update.message.reply_text("عذراً، يجب استخدام هذا الأمر في مجموعة لكي يعمل البوت بشكل صحيح.")
        return

    chat_id = update.effective_chat.id
    quiz_ref = get_quiz_ref(chat_id)
    
    if db and quiz_ref:
        # التأكد من عدم وجود مسابقة نشطة حالياً (لتجنب بدء مسابقتين في آن واحد)
        try:
            quiz_doc = quiz_ref.get()
            if quiz_doc.exists and quiz_doc.get('is_active', False):
                # تحقق من المدة الزمنية للسؤال
                question_data = quiz_doc.to_dict()
                
                # التعامل مع أنواع التوقيت المختلفة
                if 'question_time' in question_data and question_data['question_time']:
                    last_question_time = question_data['question_time']
                    if hasattr(last_question_time, 'replace'):
                        last_question_time = last_question_time.replace(tzinfo=None)
                    
                    time_limit = timedelta(minutes=1) # نعتبر أن السؤال ينتهي بعد دقيقة
                    
                    if datetime.utcnow() - last_question_time < time_limit:
                         await update.message.reply_text("هناك مسابقة نشطة حالياً. يرجى انتظار الإجابة عن السؤال الحالي أو المحاولة بعد دقيقة.")
                         return
                
                # إذا مر وقت طويل على آخر سؤال، يمكننا اعتباره منتهياً
                quiz_ref.set({'is_active': False, 'question': None, 'answer': None, 'question_time': None})
        except Exception as e:
            logger.error(f"خطأ أثناء التحقق من حالة المسابقة في Firestore: {e}")


    await update.message.reply_text("جاري توليد سؤال جديد بالذكاء الاصطناعي... ⏳")
    
    # توليد السؤال
    question, correct_answer = await generate_quiz_question("أنشئ سؤال ثقافة عامة جديد.")
    
    if not question:
        await update.message.reply_text("عذراً، فشل توليد السؤال. يرجى المحاولة لاحقاً.")
        return

    # حفظ السؤال والإجابة في قاعدة البيانات
    if db and quiz_ref:
        quiz_ref.set({
            'is_active': True,
            'question': question,
            'answer': correct_answer,
            'question_time': datetime.utcnow()
        })
        logger.info(f"تم حفظ السؤال الجديد للمجموعة {chat_id}.")

    # إرسال السؤال للمجموعة
    await update.message.reply_text(
        f"🏆 **مسابقة جديدة!** 🏆\n\n**السؤال:** {question}\n\n"
        f"لديك 60 ثانية للإجابة! أول إجابة صحيحة تكسب نقطة.\n"
        f"للإجابة، فقط اكتب الإجابة مباشرة في المجموعة.",
        parse_mode='Markdown'
    )

async def check_answer_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يتحقق من رسائل المستخدمين لمعرفة ما إذا كانت إجابة صحيحة."""
    
    if update.effective_chat.type not in [ChatMember.GROUP, ChatMember.SUPERGROUP]:
        return # يتجاهل الرسائل في المحادثات الخاصة
        
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    username = update.effective_user.first_name
    user_answer = update.message.text.strip().lower()

    quiz_ref = get_quiz_ref(chat_id)
    leaderboard_ref = get_leaderboard_ref(chat_id)

    if not db or not quiz_ref:
        return # لا يمكن التحقق بدون قاعدة بيانات

    try:
        quiz_doc = quiz_ref.get()
        if not quiz_doc.exists or not quiz_doc.get('is_active', False):
            return # لا توجد مسابقة نشطة حالياً

        correct_answer = quiz_doc.get('answer', '').lower().strip()
        
        # تحقق من مدة السؤال
        last_question_time = quiz_doc.get('question_time')
        if hasattr(last_question_time, 'replace'):
             last_question_time = last_question_time.replace(tzinfo=None)
        
        time_limit = timedelta(seconds=60)

        if datetime.utcnow() - last_question_time > time_limit:
            # انتهى الوقت
            await update.message.reply_text(
                f"⏰ انتهى وقت الإجابة على السؤال الحالي.\nالإجابة الصحيحة كانت: **{quiz_doc.get('answer')}**.\nابدأ مسابقة جديدة بـ `/newquiz`.",
                parse_mode='Markdown'
            )
            # إيقاف المسابقة الحالية
            quiz_ref.set({'is_active': False, 'question': None, 'answer': None, 'question_time': None}, merge=True)
            return
            
        # مقارنة الإجابة
        if user_answer == correct_answer:
            # الإجابة الصحيحة!
            
            # 1. إيقاف المسابقة لمنع الإجابات الأخرى
            quiz_ref.set({'is_active': False, 'question': None, 'answer': None, 'question_time': None}, merge=True)
            
            # 2. منح النقطة
            await update_score(chat_id, user_id, username, 1, leaderboard_ref)
            
            # 3. إرسال رسالة الفوز
            await update.message.reply_text(
                f"🎉 **الإجابة صحيحة!** 🎉\n"
                f"المتسابق **{username}** هو أول من أجاب بشكل صحيح.\n"
                f"تمت إضافة نقطة إلى رصيده! رصيده الحالي: {await get_score(chat_id, user_id, leaderboard_ref)} نقطة.\n"
                f"لعبة جديدة بـ `/newquiz`."
            )
            
        elif correct_answer in user_answer and len(correct_answer) > 5 and len(user_answer) < len(correct_answer) + 5:
             # معالجة الإجابات القريبة جداً
             # هذا قد لا يكون مثالياً، لكنه يضيف تفاعل
             pass

    except Exception as e:
        logger.error(f"خطأ في التحقق من الإجابة: {e}")
        await update.message.reply_text("عذراً، حدث خطأ أثناء التحقق من الإجابة. يرجى المحاولة مجدداً.")


async def score_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يعرض لوحة المتصدرين."""
    if update.effective_chat.type not in [ChatMember.GROUP, ChatMember.SUPERGROUP]:
        await update.message.reply_text("عذراً، يجب استخدام هذا الأمر في مجموعة لمعرفة لوحة المتصدرين المشتركة.")
        return

    chat_id = update.effective_chat.id
    leaderboard_ref = get_leaderboard_ref(chat_id)
    
    if not db or not leaderboard_ref:
        await update.message.reply_text("عذراً، البوت يعمل بدون قاعدة بيانات حالياً.")
        return
        
    try:
        # جلب أول 10 متصدرين
        query = leaderboard_ref.order_by('score', direction=firestore.Query.DESCENDING).limit(10)
        docs = query.get()
        
        if not docs:
            await update.message.reply_text("لا توجد نقاط مسجلة بعد. ابدأ اللعبة بـ `/newquiz`!")
            return

        leaderboard_text = "🏅 **لوحة المتصدرين** 🏅\n\n"
        rank = 1
        for doc in docs:
            data = doc.to_dict()
            leaderboard_text += f"{rank}. {data.get('username')} - **{data.get('score')} نقطة**\n"
            rank += 1
            
        await update.message.reply_text(leaderboard_text, parse_mode='Markdown')

    except Exception as e:
        logger.error(f"خطأ في عرض لوحة المتصدرين: {e}")
        await update.message.reply_text("عذراً، حدث خطأ أثناء جلب لوحة المتصدرين.")

# ----------------------------------------------------
# 7. دالة التشغيل الرئيسية (تستخدم Webhook)
# ----------------------------------------------------
def main() -> None:
    """تشغيل البوت باستخدام Webhook."""
    
    application = Application.builder().token(BOT_TOKEN).build()

    # إضافة المعالجات (Handlers)
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("newquiz", new_quiz_command))
    application.add_handler(CommandHandler("score", score_command))
    
    # معالج لجميع الرسائل النصية التي ليست أوامر
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, check_answer_message))

    # *** تشغيل Webhook بدلاً من Polling ***
    logger.info(f"تشغيل البوت على المنفذ: {PORT}")
    
    # هذا الرابط يجب تحديثه في Render بعد إنشاء الخدمة
    RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL", "https://your-render-app-name.onrender.com/") 
    
    application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path="",
        webhook_url=RENDER_URL,
    )

if __name__ == '__main__':
    main()
