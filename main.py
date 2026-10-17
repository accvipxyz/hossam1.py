# -*- coding: utf-8 -*-

"""
Telegram Audio + Image to Video Bot
Python 3.11+
python-telegram-bot
Pillow
FFmpeg + FFprobe

Features:
- Receive multiple audio files.
- Request the same number of images.
- Randomly pair audio and image files.
- Persistent SQLite history.
- Prevent repeating an exact audio/image pair.
- Generate MP4 videos.
- Separate workspaces per Telegram user.
"""

import asyncio
import hashlib
import logging
import os
import random
import shutil
import sqlite3
import subprocess
import tempfile
import time

from pathlib import Path
from typing import Optional

from PIL import Image, ImageOps, UnidentifiedImageError

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


# =========================================================
# CONFIGURATION
# =========================================================

BOT_TOKEN = "8657598364:AAF42QHCxDwX82-pLNVPZdPqI_UPfQ-TMRg"

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "audio_image_bot_data"
WORK_DIR = DATA_DIR / "work"
DB_PATH = DATA_DIR / "history.sqlite3"

# Set to 0 to disable this additional application-level limit.
# Telegram's own file-size limits still apply.
MAX_INPUT_FILE_MB = 100

# Maximum number of audio/image pairs in a single project.
MAX_FILES_PER_PROJECT = 20

# Output resolution and aspect ratio.
VIDEO_PRESETS = {
    "vertical": {
        "width": 1080,
        "height": 1920,
        "label": "عمودي 9:16",
    },
    "square": {
        "width": 1080,
        "height": 1080,
        "label": "مربع 1:1",
    },
    "horizontal": {
        "width": 1920,
        "height": 1080,
        "label": "أفقي 16:9",
    },
}

DEFAULT_PRESET = "vertical"

# H.264 video quality. Lower CRF means higher quality and larger files.
VIDEO_CRF = 18
VIDEO_PRESET = "medium"
AUDIO_BITRATE = "192k"

# Maximum time allowed for one FFmpeg operation.
FFMPEG_TIMEOUT_SECONDS = 3600

# Remove abandoned temporary sessions after this period.
SESSION_TTL_SECONDS = 6 * 60 * 60

# Set False if you do not want FFmpeg's -shortest behavior.
# When True, the video ends when the audio ends.
END_VIDEO_WITH_AUDIO = True

AUDIO_EXTENSIONS = {
    ".mp3", ".wav", ".ogg", ".oga", ".opus",
    ".m4a", ".aac", ".flac", ".wma", ".aiff",
    ".aif", ".amr", ".ac3", ".mp2", ".ape",
    ".wv", ".mp4", ".mka", ".webm", ".mov",
    ".m4b", ".3gp", ".caf",
}

IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".webp", ".bmp",
    ".tif", ".tiff", ".gif", ".ico", ".avif",
}

# Telegram audio and image content may be uploaded as documents.
# Actual content is validated after download.


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("audio_image_bot")


# =========================================================
# GLOBAL STATE
# =========================================================

SESSIONS = {}
USER_LOCKS = {}
SESSION_LOCK = asyncio.Lock()


# =========================================================
# DATABASE
# =========================================================

def init_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    WORK_DIR.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA journal_mode=WAL")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS used_pairs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                audio_hash TEXT NOT NULL,
                image_hash TEXT NOT NULL,
                audio_name TEXT NOT NULL,
                image_name TEXT NOT NULL,
                output_name TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                UNIQUE(user_id, audio_hash, image_hash)
            )
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_used_pairs_user
            ON used_pairs(user_id)
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS used_audio (
                user_id INTEGER NOT NULL,
                audio_hash TEXT NOT NULL,
                audio_name TEXT NOT NULL,
                first_seen_at INTEGER NOT NULL,
                PRIMARY KEY(user_id, audio_hash)
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS used_images (
                user_id INTEGER NOT NULL,
                image_hash TEXT NOT NULL,
                image_name TEXT NOT NULL,
                first_seen_at INTEGER NOT NULL,
                PRIMARY KEY(user_id, image_hash)
            )
        """)


def pair_exists(user_id: int, audio_hash: str, image_hash: str) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            """
            SELECT 1
            FROM used_pairs
            WHERE user_id = ?
              AND audio_hash = ?
              AND image_hash = ?
            LIMIT 1
            """,
            (user_id, audio_hash, image_hash),
        ).fetchone()

    return row is not None


def save_pair(
    user_id: int,
    audio_hash: str,
    image_hash: str,
    audio_name: str,
    image_name: str,
    output_name: str,
):
    now = int(time.time())

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("BEGIN IMMEDIATE")

        conn.execute(
            """
            INSERT INTO used_audio (
                user_id, audio_hash, audio_name, first_seen_at
            )
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, audio_hash) DO NOTHING
            """,
            (user_id, audio_hash, audio_name, now),
        )

        conn.execute(
            """
            INSERT INTO used_images (
                user_id, image_hash, image_name, first_seen_at
            )
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, image_hash) DO NOTHING
            """,
            (user_id, image_hash, image_name, now),
        )

        conn.execute(
            """
            INSERT INTO used_pairs (
                user_id,
                audio_hash,
                image_hash,
                audio_name,
                image_name,
                output_name,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                audio_hash,
                image_hash,
                audio_name,
                image_name,
                output_name,
                now,
            ),
        )


def get_history_count(user_id: int) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        result = conn.execute(
            "SELECT COUNT(*) FROM used_pairs WHERE user_id = ?",
            (user_id,),
        ).fetchone()

    return int(result[0])


# =========================================================
# FILE UTILITIES
# =========================================================

def safe_filename(name: Optional[str], fallback: str) -> str:
    """
    Prevent path traversal and unsafe filename characters.
    """
    if not name:
        return fallback

    name = Path(name).name

    cleaned = "".join(
        c for c in name
        if c.isalnum() or c in "._- ()"
    ).strip()

    if not cleaned or cleaned in {".", ".."}:
        return fallback

    return cleaned[:150]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()

    with path.open("rb") as file:
        while True:
            chunk = file.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)

    return digest.hexdigest()


def get_ffmpeg_path() -> str:
    path = shutil.which("ffmpeg")
    if not path:
        raise RuntimeError(
            "لم يتم العثور على FFmpeg. ثبّته وأضفه إلى PATH."
        )
    return path


def get_ffprobe_path() -> str:
    path = shutil.which("ffprobe")
    if not path:
        raise RuntimeError(
            "لم يتم العثور على FFprobe. ثبّته وأضفه إلى PATH."
        )
    return path


def run_command(command: list[str], timeout: int):
    """
    Run an external process without shell=True.
    This avoids shell-injection problems.
    """
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )

    if result.returncode != 0:
        error_text = result.stderr.decode(
            "utf-8",
            errors="replace",
        )[-4000:]

        raise RuntimeError(
            f"فشل تنفيذ العملية الخارجية:\n{error_text}"
        )

    return result


def validate_audio(path: Path):
    """
    Use FFprobe to ensure the file contains an audio stream.
    File extensions alone are not trusted.
    """
    command = [
        get_ffprobe_path(),
        "-v", "error",
        "-select_streams", "a:0",
        "-show_entries", "stream=codec_name",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]

    result = run_command(command, timeout=60)

    codec = result.stdout.decode(
        "utf-8",
        errors="replace",
    ).strip()

    if not codec:
        raise ValueError(
            "الملف لا يحتوي على مسار صوتي صالح."
        )


def normalize_image(source: Path, destination: Path):
    """
    Normalize a supported image into a high-resolution RGB JPEG.
    Animated images use their first frame.
    """
    try:
        with Image.open(source) as original:
            original.seek(0)

            image = ImageOps.exif_transpose(original)
            image.load()

            # Flatten transparent images onto a black background.
            if image.mode in ("RGBA", "LA") or (
                image.mode == "P" and "transparency" in image.info
            ):
                rgba = image.convert("RGBA")
                background = Image.new(
                    "RGBA",
                    rgba.size,
                    (0, 0, 0, 255),
                )
                background.alpha_composite(rgba)
                image = background.convert("RGB")
            else:
                image = image.convert("RGB")

            if image.width < 1 or image.height < 1:
                raise ValueError("أبعاد الصورة غير صالحة.")

            image.save(
                destination,
                format="JPEG",
                quality=95,
                subsampling=0,
                optimize=True,
            )

    except (
        UnidentifiedImageError,
        OSError,
        ValueError,
    ) as exc:
        raise ValueError(
            "تعذر قراءة الصورة. أرسل صورة صالحة بصيغة مدعومة."
        ) from exc


def get_audio_duration(path: Path) -> float:
    command = [
        get_ffprobe_path(),
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]

    result = run_command(command, timeout=60)

    try:
        duration = float(
            result.stdout.decode("utf-8").strip()
        )
    except (ValueError, AttributeError) as exc:
        raise ValueError(
            "تعذر تحديد مدة الملف الصوتي."
        ) from exc

    if duration <= 0:
        raise ValueError("مدة الصوت غير صالحة.")

    return duration


def create_video(
    image_path: Path,
    audio_path: Path,
    output_path: Path,
    preset_name: str,
):
    """
    Create an MP4 video from a still image and an audio file.
    """
    preset = VIDEO_PRESETS[preset_name]

    width = preset["width"]
    height = preset["height"]

    command = [
        get_ffmpeg_path(),
        "-hide_banner",
        "-loglevel", "error",
        "-y",

        "-loop", "1",
        "-framerate", "30",
        "-i", str(image_path),

        "-i", str(audio_path),

        "-map", "0:v:0",
        "-map", "1:a:0",

        "-vf",
        (
            f"scale={width}:{height}:"
            "force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
            "setsar=1,format=yuv420p"
        ),

        "-c:v", "libx264",
        "-preset", VIDEO_PRESET,
        "-crf", str(VIDEO_CRF),

        "-c:a", "aac",
        "-b:a", AUDIO_BITRATE,

        "-movflags", "+faststart",

        "-metadata", "title=Audio Image Video",
    ]

    if END_VIDEO_WITH_AUDIO:
        command.append("-shortest")

    command.append(str(output_path))

    run_command(
        command,
        timeout=FFMPEG_TIMEOUT_SECONDS,
    )

    if not output_path.exists() or output_path.stat().st_size == 0:
        raise RuntimeError("لم يتم إنشاء ملف فيديو صالح.")

    # Verify that the output contains both video and audio streams.
    verify_command = [
        get_ffprobe_path(),
        "-v", "error",
        "-show_entries", "stream=codec_type",
        "-of", "csv=p=0",
        str(output_path),
    ]

    result = run_command(verify_command, timeout=60)

    stream_types = result.stdout.decode(
        "utf-8",
        errors="replace",
    ).splitlines()

    if "video" not in stream_types or "audio" not in stream_types:
        raise RuntimeError(
            "الفيديو الناتج لا يحتوي على الصوت والصورة معًا."
        )


# =========================================================
# SESSION UTILITIES
# =========================================================

def get_user_lock(user_id: int) -> asyncio.Lock:
    if user_id not in USER_LOCKS:
        USER_LOCKS[user_id] = asyncio.Lock()

    return USER_LOCKS[user_id]


def get_session(user_id: int):
    return SESSIONS.get(user_id)


def new_session(user_id: int):
    """
    Create an isolated workspace for one user.
    """
    old = SESSIONS.get(user_id)

    if old:
        old_dir = old.get("directory")
        if old_dir:
            shutil.rmtree(old_dir, ignore_errors=True)

    user_dir = WORK_DIR / str(user_id)

    # A user's new project replaces that user's old unfinished project.
    shutil.rmtree(user_dir, ignore_errors=True)
    user_dir.mkdir(parents=True, exist_ok=True)

    session = {
        "directory": user_dir,
        "audios": [],
        "images": [],
        "state": "audios",
        "preset": DEFAULT_PRESET,
        "created_at": time.time(),
    }

    SESSIONS[user_id] = session
    return session


def cleanup_session(user_id: int):
    session = SESSIONS.pop(user_id, None)

    if session:
        directory = session.get("directory")
        if directory:
            shutil.rmtree(directory, ignore_errors=True)


async def cleanup_abandoned_sessions():
    now = time.time()

    for user_id, session in list(SESSIONS.items()):
        created_at = session.get("created_at", now)

        if now - created_at > SESSION_TTL_SECONDS:
            cleanup_session(user_id)


# =========================================================
# TELEGRAM HELPERS
# =========================================================

async def send_main_menu(message, text="اختر ما تريد فعله:"):
    keyboard = [
        [
            InlineKeyboardButton(
                "🎬 مشروع جديد",
                callback_data="new_project",
            )
        ],
        [
            InlineKeyboardButton(
                "📊 إحصائياتي",
                callback_data="stats",
            )
        ],
        [
            InlineKeyboardButton(
                "❌ إلغاء المشروع الحالي",
                callback_data="cancel",
            )
        ],
    ]

    await message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.effective_message or not update.effective_user:
        return

    await send_main_menu(
        update.effective_message,
        (
            "أهلًا بك في بوت تحويل الصوت والصورة إلى فيديو.\n\n"
            "يمكنك إرسال عدة ملفات صوتية، ثم العدد نفسه من الصور، "
            "وسأنتج لك فيديو مستقلًا لكل زوج.\n\n"
            "اضغط على «مشروع جديد» للبدء."
        ),
    )


async def cancel_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if update.effective_user:
        cleanup_session(update.effective_user.id)

    if update.effective_message:
        await update.effective_message.reply_text(
            "تم إلغاء المشروع وحذف ملفاته المؤقتة. "
            "سجل المطابقات السابقة محفوظ."
        )


async def button_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not query or not query.from_user:
        return

    await query.answer()

    user_id = query.from_user.id
    data = query.data

    if data == "new_project":
        session = new_session(user_id)

        await query.message.reply_text(
            "🎵 أرسل الملفات الصوتية الآن.\n\n"
            "يمكنك إرسال ملف واحد أو عدة ملفات.\n"
            "بعد الانتهاء، اضغط الزر التالي.",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "✅ انتهيت من الصوتيات",
                        callback_data="finish_audios",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "❌ إلغاء",
                        callback_data="cancel",
                    )
                ],
            ]),
        )

    elif data == "finish_audios":
        session = get_session(user_id)

        if not session:
            await query.message.reply_text(
                "لا يوجد مشروع نشط. ابدأ مشروعًا جديدًا."
            )
            return

        if session["state"] != "audios":
            await query.message.reply_text(
                "أنت بالفعل في مرحلة إرسال الصور."
            )
            return

        if not session["audios"]:
            await query.message.reply_text(
                "أرسل ملفًا صوتيًا واحدًا على الأقل أولًا."
            )
            return

        session["state"] = "images"
        required = len(session["audios"])

        await query.message.reply_text(
            f"🖼️ استلمت {required} ملف صوتي.\n\n"
            f"أرسل الآن {required} صورة بالضبط.\n"
            "سأخلط الصوتيات والصور عشوائيًا قبل المطابقة.\n\n"
            "إذا كانت مطابقة صوت وصورة مستخدمة سابقًا، "
            "سأطلب صورة بديلة."
        )

    elif data == "cancel":
        cleanup_session(user_id)

        await query.message.reply_text(
            "تم إلغاء المشروع وحذف الملفات المؤقتة."
        )

    elif data == "stats":
        count = get_history_count(user_id)

        await query.message.reply_text(
            f"📊 إحصائيات حسابك:\n\n"
            f"عدد مطابقات الصوت والصورة المحفوظة: {count}\n\n"
            "هذه البيانات محفوظة في قاعدة SQLite."
        )


# =========================================================
# MEDIA DOWNLOAD
# =========================================================

async def download_telegram_file(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    destination: Path,
):
    message = update.effective_message

    if not message:
        raise ValueError("لم يتم العثور على الرسالة.")

    telegram_file = None
    original_name = None

    if message.document:
        document = message.document
        telegram_file = await context.bot.get_file(document.file_id)
        original_name = document.file_name

    elif message.audio:
        audio = message.audio
        telegram_file = await context.bot.get_file(audio.file_id)
        original_name = audio.file_name

    elif message.voice:
        voice = message.voice
        telegram_file = await context.bot.get_file(voice.file_id)
        original_name = "voice.ogg"

    elif message.photo:
        photo = message.photo[-1]
        telegram_file = await context.bot.get_file(photo.file_id)
        original_name = "telegram_photo.jpg"

    else:
        raise ValueError(
            "أرسل ملفًا صوتيًا أو صورة أو مستندًا مناسبًا."
        )

    file_size = getattr(telegram_file, "file_size", None)

    if (
        MAX_INPUT_FILE_MB > 0
        and file_size is not None
        and file_size > MAX_INPUT_FILE_MB * 1024 * 1024
    ):
        raise ValueError(
            f"حجم الملف أكبر من الحد المحدد "
            f"({MAX_INPUT_FILE_MB} MB)."
        )

    destination.parent.mkdir(parents=True, exist_ok=True)

    await telegram_file.download_to_drive(
        custom_path=str(destination)
    )

    if not destination.exists() or destination.stat().st_size == 0:
        raise ValueError("تعذر تنزيل الملف أو أنه فارغ.")

    return safe_filename(
        original_name,
        destination.name,
    )


# =========================================================
# AUDIO HANDLER
# =========================================================

async def handle_audio(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.effective_user or not update.effective_message:
        return

    user_id = update.effective_user.id
    session = get_session(user_id)

    if not session:
        await update.effective_message.reply_text(
            "ابدأ أولًا باستخدام /start."
        )
        return

    if session["state"] != "audios":
        await update.effective_message.reply_text(
            "انتهيت من مرحلة الصوتيات. أرسل الصور المطلوبة الآن."
        )
        return

    if len(session["audios"]) >= MAX_FILES_PER_PROJECT:
        await update.effective_message.reply_text(
            f"الحد الأقصى للمشروع هو {MAX_FILES_PER_PROJECT} ملفًا صوتيًا."
        )
        return

    user_dir = session["directory"]
    index = len(session["audios"]) + 1
    destination = user_dir / f"audio_{index}.bin"

    try:
        original_name = await download_telegram_file(
            update,
            context,
            destination,
        )

        await asyncio.to_thread(
            validate_audio,
            destination,
        )

        audio_hash = await asyncio.to_thread(
            sha256_file,
            destination,
        )

        session["audios"].append({
            "path": destination,
            "hash": audio_hash,
            "name": original_name,
        })

        await update.effective_message.reply_text(
            f"✅ تم استلام الصوت رقم {len(session['audios'])}:\n"
            f"{original_name}\n\n"
            f"إجمالي الصوتيات: {len(session['audios'])}\n"
            "أرسل المزيد أو اضغط «انتهيت من الصوتيات»."
        )

    except (
        ValueError,
        RuntimeError,
        TelegramError,
        OSError,
        subprocess.SubprocessError,
    ) as exc:
        destination.unlink(missing_ok=True)

        logger.warning(
            "Audio rejected for user %s: %s",
            user_id,
            exc,
        )

        await update.effective_message.reply_text(
            f"⚠️ لم أتمكن من قبول الملف الصوتي:\n{exc}"
        )


# =========================================================
# IMAGE HANDLER
# =========================================================

async def handle_image(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.effective_user or not update.effective_message:
        return

    user_id = update.effective_user.id
    session = get_session(user_id)

    if not session:
        await update.effective_message.reply_text(
            "ابدأ أولًا باستخدام /start."
        )
        return

    if session["state"] != "images":
        await update.effective_message.reply_text(
            "أرسل الصوتيات أولًا، ثم اضغط «انتهيت من الصوتيات»."
        )
        return

    required = len(session["audios"])

    if len(session["images"]) >= required:
        await update.effective_message.reply_text(
            "استلمت العدد المطلوب من الصور. جارٍ تجهيز المشروع."
        )
        return

    user_dir = session["directory"]
    index = len(session["images"]) + 1
    raw_path = user_dir / f"image_raw_{index}.bin"
    normalized_path = user_dir / f"image_{index}.jpg"

    try:
        original_name = await download_telegram_file(
            update,
            context,
            raw_path,
        )

        await asyncio.to_thread(
            normalize_image,
            raw_path,
            normalized_path,
        )

        image_hash = await asyncio.to_thread(
            sha256_file,
            normalized_path,
        )

        session["images"].append({
            "path": normalized_path,
            "hash": image_hash,
            "name": original_name,
        })

        raw_path.unlink(missing_ok=True)

        received = len(session["images"])

        await update.effective_message.reply_text(
            f"✅ تم استلام الصورة رقم {received}.\n"
            f"الصور المستلمة: {received}/{required}"
        )

        if received == required:
            await process_project(
                update,
                context,
                session,
            )

    except (
        ValueError,
        RuntimeError,
        TelegramError,
        OSError,
        subprocess.SubprocessError,
    ) as exc:
        raw_path.unlink(missing_ok=True)
        normalized_path.unlink(missing_ok=True)

        logger.warning(
            "Image rejected for user %s: %s",
            user_id,
            exc,
        )

        await update.effective_message.reply_text(
            f"⚠️ لم أتمكن من قبول الصورة:\n{exc}\n\n"
            "أرسل صورة بديلة."
        )


# =========================================================
# PROJECT PROCESSING
# =========================================================

def build_random_pairs(session, user_id: int):
    """
    Shuffle the order of audios and images independently.
    Skip any previously used exact audio/image combination.
    """
    audios = list(session["audios"])
    images = list(session["images"])

    random.shuffle(audios)
    random.shuffle(images)

    pairs = []
    rejected = []

    for audio in audios:
        selected_index = None

        for index, image in enumerate(images):
            if not pair_exists(
                user_id,
                audio["hash"],
                image["hash"],
            ):
                selected_index = index
                break

        if selected_index is None:
            rejected.append(audio)
            continue

        image = images.pop(selected_index)
        pairs.append((audio, image))

    return pairs, rejected


async def process_project(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    session,
):
    message = update.effective_message
    user_id = update.effective_user.id

    if not message:
        return

    await message.reply_text(
        "⏳ اكتمل إرسال الملفات.\n"
        "أتحقق من سجل المطابقات وأبدأ إنتاج الفيديوهات..."
    )

    async with get_user_lock(user_id):
        try:
            pairs, rejected = await asyncio.to_thread(
                build_random_pairs,
                session,
                user_id,
            )

            if rejected:
                names = "\n".join(
                    f"• {item['name']}" for item in rejected
                )

                await message.reply_text(
                    "⚠️ تعذر إيجاد صورة جديدة لبعض الصوتيات "
                    "من الصور المرسلة، لأن كل المطابقات الممكنة "
                    "مستخدمة سابقًا.\n\n"
                    f"الصوتيات التي تحتاج صورًا مختلفة:\n{names}\n\n"
                    "أرسل مشروعًا جديدًا بهذه الصوتيات وصورًا مختلفة."
                )

                cleanup_session(user_id)
                return

            if not pairs:
                await message.reply_text(
                    "لم أجد أي مطابقة جديدة صالحة."
                )
                cleanup_session(user_id)
                return

            # Keep the pairing order random.
            random.shuffle(pairs)

            success_count = 0
            failed_count = 0

            for number, (audio, image) in enumerate(pairs, start=1):
                output_name = (
                    f"video_{number}_{int(time.time())}.mp4"
                )

                output_path = session["directory"] / output_name

                try:
                    await asyncio.to_thread(
                        create_video,
                        image["path"],
                        audio["path"],
                        output_path,
                        session["preset"],
                    )

                    # Insert the pair before delivery so a retry does
                    # not silently generate the same pair again.
                    try:
                        await asyncio.to_thread(
                            save_pair,
                            user_id,
                            audio["hash"],
                            image["hash"],
                            audio["name"],
                            image["name"],
                            output_name,
                        )
                    except sqlite3.IntegrityError:
                        await message.reply_text(
                            "تم اكتشاف مطابقة مستخدمة سابقًا "
                            "أثناء الحفظ. تم تجاوز هذا الفيديو."
                        )
                        failed_count += 1
                        output_path.unlink(missing_ok=True)
                        continue

                    with output_path.open("rb") as video_file:
                        await message.reply_video(
                            video=video_file,
                            filename=output_name,
                            caption=(
                                f"🎬 الفيديو {number}/{len(pairs)}\n"
                                f"🎵 الصوت: {audio['name']}\n"
                                f"🖼️ الصورة: {image['name']}"
                            ),
                            supports_streaming=True,
                            read_timeout=300,
                            write_timeout=300,
                            connect_timeout=30,
                            pool_timeout=300,
                        )

                    success_count += 1

                except Exception:
                    failed_count += 1

                    logger.exception(
                        "Video generation/delivery failed "
                        "for user %s, item %s",
                        user_id,
                        number,
                    )

                    await message.reply_text(
                        f"⚠️ حدث خطأ أثناء معالجة الفيديو رقم {number}. "
                        "سأحاول متابعة بقية الملفات."
                    )

                finally:
                    output_path.unlink(missing_ok=True)

            await message.reply_text(
                "✅ انتهى المشروع.\n\n"
                f"الفيديوهات المرسلة بنجاح: {success_count}\n"
                f"العمليات التي لم تكتمل: {failed_count}\n\n"
                "تم الاحتفاظ بسجل المطابقات في قاعدة البيانات."
            )

        except Exception:
            logger.exception(
                "Project failed for user %s",
                user_id,
            )

            await message.reply_text(
                "حدث خطأ غير متوقع أثناء تجهيز المشروع. "
                "راجع سجل التشغيل إذا تكرر الخطأ."
            )

        finally:
            cleanup_session(user_id)


# =========================================================
# MESSAGE ROUTING
# =========================================================

async def media_router(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message

    if not message or not update.effective_user:
        return

    session = get_session(update.effective_user.id)

    if not session:
        await message.reply_text(
            "استخدم /start، ثم اضغط «مشروع جديد»."
        )
        return

    if session["state"] == "audios":
        if message.audio or message.voice:
            await handle_audio(update, context)
            return

        if message.document:
            # Documents are checked by FFprobe, not only extension.
            await handle_audio(update, context)
            return

        await message.reply_text(
            "أرسل ملفًا صوتيًا، أو اضغط زر إنهاء الصوتيات."
        )

    elif session["state"] == "images":
        if message.photo or message.document:
            await handle_image(update, context)
            return

        await message.reply_text(
            "أرسل صورة بصيغة مدعومة."
        )


# =========================================================
# ERROR HANDLER
# =========================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):
    logger.error(
        "Unhandled exception while processing update",
        exc_info=context.error,
    )


# =========================================================
# MAIN
# =========================================================

def main():
    if (
        not BOT_TOKEN
        or BOT_TOKEN == "PUT_YOUR_BOT_TOKEN_HERE"
    ):
        raise RuntimeError(
            "ضع توكن البوت الصحيح في المتغير BOT_TOKEN داخل الملف."
        )

    init_db()

    # Fail early if FFmpeg is unavailable.
    get_ffmpeg_path()
    get_ffprobe_path()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(False)
        .build()
    )

    application.add_handler(
        CommandHandler("start", start_command)
    )

    application.add_handler(
        CommandHandler("cancel", cancel_command)
    )

    application.add_handler(
        CallbackQueryHandler(button_handler)
    )

    application.add_handler(
        MessageHandler(
            filters.AUDIO
            | filters.VOICE
            | filters.PHOTO
            | filters.Document.ALL,
            media_router,
        )
    )

    application.add_error_handler(error_handler)

    logger.info("Bot is starting...")

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
