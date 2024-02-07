from telegram import Update
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext
import random
import time

TOKEN = '6801731329:AAFg0hKVMmUHaq_db5Q497j47pLcxi6tGbU'

azkar_list = [
    "اللهم بك أصبحنا وبك أمسينا وبك نحيا وبك نموت وإليك المصير.",
    "الحمد لله الذي أحيانا بعد ما أماتنا وإليه النشور.",
    "اللهم ما أصبح بي من نعمة أو بأحد من خلقك فمنك وحدك لا شريك لك فلك الحمد ولك الشكر.",
    # Add additional azkar here
]

def send_azkar(update: Update, context: CallbackContext) -> None:
    chat_id = update.message.chat_id
    while True:
        azkar = random.choice(azkar_list)
        context.bot.send_message(chat_id=chat_id, text=azkar)
        time.sleep(1)  # أرسل أذكار كل ساعة

def main() -> None:
    updater = Updater(token=TOKEN)
    dispatcher = updater.dispatcher

    # تعريف أمر البداية
    start_handler = CommandHandler('start', send_azkar)
    dispatcher.add_handler(start_handler)

    # تشغيل البوت
    updater.start_polling()
    updater.idle()

if __name__ == '__main__':
    main()
