import re
from telethon import TelegramClient, events, Button
from telethon.tl.types import MessageMediaContact

# بيانات البوت والحساب
api_id = 28477626         # ضع هنا api_id الخاص بك
api_hash = "ffd0faf3767db33b6a2b0f727a5a60e0"   # ضع هنا api_hash الخاص بك
bot_token = "7348415101:AAFwd_K8tBzRD4nx9jmJuDAiQpwV11yP_ww" # ضع هنا توكن البوت

# معرف الشخص الذي ستُحوّل إليه جهات الاتصال (يمكنك تغييره)
FORWARD_ID = 5599020702

# قائمة أرقام الهواتف المسموح بها (بدون رموز مثل "+" أو فراغات)
ALLOWED_NUMBERS = [
    "01153275800",
    "+201145189549",
    # أضف باقي الأرقام هنا (حوالي 170 رقم)
]

# رابط الدعوة الذي سيتم إرساله للمستخدمين المسموح لهم
INVITE_LINK = "https://t.me/your_invite_link"

def normalize_number(number: str) -> str:
    """
    دالة لتطبيع رقم الهاتف بإزالة علامات مثل "+"، الفراغات، والواصلات.
    """
    return re.sub(r"[+\s\-()]", "", number)

# إنشاء عميل البوت باستخدام Telethon
client = TelegramClient('bot', api_id, api_hash).start(bot_token=bot_token)

@client.on(events.NewMessage(pattern='/start'))
async def start_handler(event):
    """
    عند استقبال أمر /start، يُرسل رسالة تطلب من المستخدم مشاركة جهة الاتصال
    مع زر يطلب رقم الهاتف (request contact).
    """
    await event.reply(
        "مرحباً، يرجى مشاركة جهة الاتصال الخاصة بك:",
        buttons=[Button.request_phone("شارك جهة الاتصال")]
    )

@client.on(events.NewMessage)
async def contact_handler(event):
    """
    عند استقبال رسالة، يتم التحقق مما إذا كانت تحتوي على جهة اتصال.
    إذا كانت كذلك:
      1. يتم إعادة توجيه الرسالة كاملةً للمعرف المحدد.
      2. يتم استخراج رقم الهاتف ومقارنته مع القائمة المسموح بها.
      3. يُرسل رد للمستخدم حسب حالة الرقم (مسموح أم لا).
    """
    if event.message.media and isinstance(event.message.media, MessageMediaContact):
        contact = event.message.media
        phone = contact.phone_number
        normalized_phone = normalize_number(phone)
        allowed = [normalize_number(num) for num in ALLOWED_NUMBERS]
        
        # إعادة توجيه رسالة جهة الاتصال إلى المعرف المحدد
        await client.forward_messages(FORWARD_ID, event.message)
        
        # التحقق مما إذا كان رقم الهاتف ضمن القائمة المسموح بها
        if normalized_phone in allowed:
            await event.reply(f"مرحباً! هذا هو رابط الدعوة: {INVITE_LINK}")
        else:
            await event.reply("عذراً، رقم هاتفك غير مسموح لك بالدخول.")
    else:
        # يمكن هنا التعامل مع رسائل أخرى أو تجاهلها
        pass

def main():
    print("البوت يعمل الآن...")
    client.run_until_disconnected()

if __name__ == '__main__':
    main()

