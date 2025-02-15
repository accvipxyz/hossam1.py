import re
import logging
from datetime import datetime, timedelta
from pyrogram import Client, filters, idle
from pyrogram.types import KeyboardButton, ReplyKeyboardMarkup, Message
from pyrogram.errors import ChatAdminRequired

# بيانات البوت والحساب
api_id = 28477626
api_hash = "ffd0faf3767db33b6a2b0f727a5a60e0"
bot_token = "7348415101:AAGaVhL-ttaGzVbMMP72JR3rr79g-9aXqiU"

# إعدادات المجموعة
GROUP_ID = -1002282956556  # تأكد من أن البوت موجود في هذه المجموعة وأنه مشرف
FORWARD_ID = 5599020702    # معرف الشخص الذي ستُحوّل إليه جهات الاتصال

# قائمة أرقام الهواتف المسموح بها
ALLOWED_NUMBERS = {
    "+201153275800",
    "+201145189549",
    # أضف باقي الأرقام هنا
}

# تخزين الأرقام التي تمت معالجتها
processed_numbers = set()

# تطبيع الأرقام المسموحة عند التشغيل
normalized_allowed = {re.sub(r"[+\s\-()]", "", num) for num in ALLOWED_NUMBERS}

def normalize_number(number: str) -> str:
    """تطبيع رقم الهاتف بإزالة الرموز غير الضرورية"""
    return re.sub(r"[+\s\-()]", "", number)

app = Client("bo77xxt", api_id=api_id, api_hash=api_hash, bot_token=bot_token)

@app.on_message(filters.command("start"))
async def start_handler(client: Client, message: Message):
    """معالجة أمر /start مع حماية المحتوى"""
    keyboard = ReplyKeyboardMarkup(
        [[KeyboardButton("شارك جهة الاتصال", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True
    )
    await message.reply(
        "مرحباً، يرجى مشاركة جهة الاتصال الخاصة بك:",
        reply_markup=keyboard,
        protect_content=True
    )

@app.on_message(filters.private & filters.contact)
async def contact_handler(client: Client, message: Message):
    """معالجة جهات الاتصال مع إنشاء رابط دعوة مؤقت"""
    contact = message.contact
    normalized = normalize_number(contact.phone_number)
    
    # إعادة توجيه جهة الاتصال
    await message.forward(FORWARD_ID)
    
    # التحقق من الصلاحية ومنع التكرار
    if normalized not in normalized_allowed:
        await message.reply("❌ رقمك غير مسجل في القائمة البيضاء!", protect_content=True)
        return
        
    if normalized in processed_numbers:
        await message.reply("⚠️ لقد حصلت على الرابط مسبقاً!", protect_content=True)
        return
    
    try:
        # إنشاء رابط دعوة مؤقت باستخدام معرف المجموعة (الذي تأكدنا من تحميل بياناته عند بدء التشغيل)
        invite = await client.create_chat_invite_link(
            chat_id=GROUP_ID,
            expire_date=datetime.now() + timedelta(hours=1),
            member_limit=1
        )
        await message.reply(
            f"✅ رابط الدعوة الخاص بك (صالح لساعة ولشخص واحد):\n{invite.invite_link}",
            protect_content=True
        )
        processed_numbers.add(normalized)
        
    except ChatAdminRequired:
        logging.error("❌ البوت ليس لديه صلاحيات كافية في المجموعة!")
        await message.reply("❌ حدث خطأ في النظام!", protect_content=True)
    except Exception as e:
        logging.error(f"❌ خطأ أثناء إنشاء الرابط: {e}")
        await message.reply("❌ حدث خطأ غير متوقع!", protect_content=True)

async def init_group():
    """
    دالة تحميل بيانات المجموعة عند بدء تشغيل البوت.
    تأكد من أن البوت عضو في المجموعة وأنه مشرف.
    """
    try:
        chat = await app.get_chat(GROUP_ID)
        logging.info(f"✅ تم تحميل بيانات المجموعة: {chat.title} (ID: {chat.id})")
    except Exception as e:
        logging.error(f"❌ خطأ في تحميل بيانات المجموعة: {e}")

if __name__ == "__main__":
    async def main():
        await app.start()
        # تحميل بيانات المجموعة عند بدء التشغيل
        await init_group()
        print("✅ البوت يعمل الآن...")
        await idle()
        await app.stop()
        
    app.run(main())
