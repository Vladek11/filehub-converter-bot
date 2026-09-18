"""
Telegram-бот-конвертер файлов.

Бесплатно: конвертация картинок, извлечение текста из PDF, DOCX->PDF.
Premium: видео->аудио, без дневного лимита (50/день), файлы до 50 МБ.
VIP: всё из Premium + конвертация аудио, объединение PDF, OCR сканов.

Оплата — через Telegram Stars.

Подписки и лимиты хранятся в Upstash Redis (постоянно, переживают
перезапуски сервиса). Переменные окружения UPSTASH_REDIS_REST_URL
и UPSTASH_REDIS_REST_TOKEN обязательны.

Токен берётся из переменной окружения TELEGRAM_TOKEN.
"""

import asyncio
import io
import json
import logging
import os
import subprocess
import tempfile
import uuid
from datetime import date, timedelta

import aiohttp
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import (
    BufferedInputFile,
    KeyboardButton,
    LabeledPrice,
    PreCheckoutQuery,
    ReplyKeyboardMarkup,
)
from aiohttp import web
from PIL import Image
import fitz  # PyMuPDF
import img2pdf
import pytesseract

# ==== НАСТРОЙКИ ====
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
PORT = int(os.environ.get("PORT", 8080))

REDIS_URL = os.environ["UPSTASH_REDIS_REST_URL"]
REDIS_TOKEN = os.environ["UPSTASH_REDIS_REST_TOKEN"]

OWNER_IDS = {1745647417}  # владелец бота — VIP навсегда, без лимитов

TIER_LIMITS = {
    "free": {"daily": 15, "max_size_mb": 20, "video_audio_daily": 0},
    "premium": {"daily": 50, "max_size_mb": 50, "video_audio_daily": 10},
    "vip": {"daily": 150, "max_size_mb": 50, "video_audio_daily": 30},
}

PRICES_STARS = {"premium": 100, "vip": 250}
SUBSCRIPTION_DAYS = 30

VIP_ONLY_ACTIONS = {"audio_convert", "merge_pdf", "ocr"}
PREMIUM_PLUS_ACTIONS = {"video_to_audio"}  # premium и vip
# ====================

logging.basicConfig(level=logging.INFO)

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

pending_action: dict[int, str] = {}
pending_merge_files: dict[int, list[bytes]] = {}


# ==== Redis (Upstash) — постоянное хранилище подписок и лимитов ====

async def redis_command(*args) -> any:
    """Выполняет одну команду Redis через REST API Upstash."""
    headers = {"Authorization": f"Bearer {REDIS_TOKEN}"}
    async with aiohttp.ClientSession() as session:
        async with session.post(REDIS_URL, headers=headers, json=list(args)) as response:
            data = await response.json()
            return data.get("result")


async def get_user_tier(user_id: int) -> str:
    if user_id in OWNER_IDS:
        return "vip"
    raw = await redis_command("GET", f"sub:{user_id}")
    if not raw:
        return "free"
    sub = json.loads(raw)
    if date.fromisoformat(sub["expires"]) >= date.today():
        return sub["tier"]
    return "free"


async def grant_subscription(user_id: int, tier: str):
    expires = date.today() + timedelta(days=SUBSCRIPTION_DAYS)
    value = json.dumps({"tier": tier, "expires": expires.isoformat()})
    # ключ живёт чуть дольше самой подписки — просто с запасом
    await redis_command("SET", f"sub:{user_id}", value, "EX", str(SUBSCRIPTION_DAYS * 86400 + 86400))


async def check_and_increment_limit(user_id: int, kind: str, daily_limit: int) -> bool:
    if user_id in OWNER_IDS:
        return True
    key = f"usage:{kind}:{user_id}:{date.today().isoformat()}"
    current_raw = await redis_command("GET", key)
    current = int(current_raw) if current_raw else 0
    if current >= daily_limit:
        return False
    new_count = await redis_command("INCR", key)
    if new_count == 1:
        await redis_command("EXPIRE", key, "172800")  # авто-очистка через 2 дня
    return True


def user_has_access(action: str, tier: str) -> bool:
    if action in VIP_ONLY_ACTIONS:
        return tier == "vip"
    if action in PREMIUM_PLUS_ACTIONS:
        return tier in ("premium", "vip")
    return True  # бесплатные функции доступны всем


# ==== Конвертация DOCX -> PDF (LibreOffice) ====

def convert_docx_to_pdf_bytes(docx_bytes: bytes) -> bytes | None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        input_path = os.path.join(tmp_dir, f"{uuid.uuid4()}.docx")
        with open(input_path, "wb") as f:
            f.write(docx_bytes)
        try:
            subprocess.run(
                ["soffice", "--headless", "--convert-to", "pdf", "--outdir", tmp_dir, input_path],
                check=True, timeout=60, capture_output=True,
            )
        except Exception as e:
            logging.error("LibreOffice conversion failed: %s", e)
            return None
        output_path = input_path.replace(".docx", ".pdf")
        if not os.path.exists(output_path):
            return None
        with open(output_path, "rb") as f:
            return f.read()


# ==== Видео -> аудио и конвертация аудио (ffmpeg) ====

def extract_audio_from_video(video_bytes: bytes, suffix_in: str = ".mp4") -> bytes | None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        input_path = os.path.join(tmp_dir, f"{uuid.uuid4()}{suffix_in}")
        output_path = os.path.join(tmp_dir, f"{uuid.uuid4()}.mp3")
        with open(input_path, "wb") as f:
            f.write(video_bytes)
        try:
            subprocess.run(
                ["ffmpeg", "-i", input_path, "-vn", "-acodec", "libmp3lame", "-y", output_path],
                check=True, timeout=120, capture_output=True,
            )
        except Exception as e:
            logging.error("ffmpeg video->audio failed: %s", e)
            return None
        if not os.path.exists(output_path):
            return None
        with open(output_path, "rb") as f:
            return f.read()


def convert_audio_format(audio_bytes: bytes, target_ext: str, suffix_in: str = ".mp3") -> bytes | None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        input_path = os.path.join(tmp_dir, f"{uuid.uuid4()}{suffix_in}")
        output_path = os.path.join(tmp_dir, f"{uuid.uuid4()}.{target_ext}")
        with open(input_path, "wb") as f:
            f.write(audio_bytes)
        try:
            subprocess.run(
                ["ffmpeg", "-i", input_path, "-y", output_path],
                check=True, timeout=120, capture_output=True,
            )
        except Exception as e:
            logging.error("ffmpeg audio convert failed: %s", e)
            return None
        if not os.path.exists(output_path):
            return None
        with open(output_path, "rb") as f:
            return f.read()


# ==== OCR (сканы/фото документов) ====

def ocr_image_bytes(image_bytes: bytes) -> str:
    img = Image.open(io.BytesIO(image_bytes))
    return pytesseract.image_to_string(img, lang="rus+ukr+eng")


def ocr_pdf_bytes(pdf_bytes: bytes) -> str:
    pdf_doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    text_parts = []
    for page in pdf_doc:
        pix = page.get_pixmap(dpi=200)
        img_bytes = pix.tobytes("png")
        img = Image.open(io.BytesIO(img_bytes))
        text_parts.append(pytesseract.image_to_string(img, lang="rus+ukr+eng"))
    pdf_doc.close()
    return "\n\n".join(text_parts)


# ==== Reply-клавиатуры ====

BTN_PNG = "🖼 В PNG"
BTN_JPG = "🖼 В JPG"
BTN_WEBP = "🖼 В WEBP"
BTN_COMPRESS = "🗜 Сжать картинку"
BTN_IMG_TO_PDF = "📄 Картинка → PDF"
BTN_PDF_TEXT = "📝 Извлечь текст из PDF"
BTN_DOCX_TO_PDF = "📝 DOCX → PDF"
BTN_VIDEO_TO_AUDIO = "🎬 Видео → аудио"
BTN_AUDIO_CONVERT = "🎵 Конвертация аудио"
BTN_MERGE_PDF = "📚 Объединить PDF"
BTN_OCR = "🔍 OCR (текст со скана)"
BTN_SUBSCRIPTION = "💎 Подписка"
BTN_BACK = "◀️ Назад"
BTN_TARIFFS = "💰 Тарифы"
BTN_BUY_PREMIUM = f"✅ Купить Premium — {PRICES_STARS['premium']}⭐"
BTN_BUY_VIP = f"✅ Купить VIP — {PRICES_STARS['vip']}⭐"
BTN_DONE_MERGE = "✅ Готово, объединить"

BUTTON_TO_ACTION = {
    BTN_PNG: "img_png",
    BTN_JPG: "img_jpg",
    BTN_WEBP: "img_webp",
    BTN_COMPRESS: "img_compress",
    BTN_IMG_TO_PDF: "img_to_pdf",
    BTN_PDF_TEXT: "pdf_text",
    BTN_DOCX_TO_PDF: "docx_to_pdf",
    BTN_VIDEO_TO_AUDIO: "video_to_audio",
    BTN_AUDIO_CONVERT: "audio_convert",
    BTN_MERGE_PDF: "merge_pdf",
    BTN_OCR: "ocr",
}

ACTION_INFO = {
    "img_png": ("image", "Пришли картинку — сконвертирую в PNG."),
    "img_jpg": ("image", "Пришли картинку — сконвертирую в JPG."),
    "img_webp": ("image", "Пришли картинку — сконвертирую в WEBP."),
    "img_compress": ("image", "Пришли картинку — сожму её."),
    "img_to_pdf": ("image", "Пришли картинку — сделаю из неё PDF."),
    "pdf_text": ("pdf", "Пришли PDF-файл — извлеку из него текст."),
    "docx_to_pdf": ("docx", "Пришли Word-файл (.docx) — точно сконвертирую в PDF."),
    "video_to_audio": ("video", "Пришли видео — извлеку из него звук (MP3)."),
    "audio_convert": ("audio", "Пришли аудиофайл — сконвертирую в MP3."),
    "merge_pdf": ("pdf_multi", "Присылай PDF-файлы по одному, потом нажми «Готово, объединить»."),
    "ocr": ("image_or_pdf", "Пришли скан/фото документа или PDF — извлеку текст (OCR)."),
}


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_PNG), KeyboardButton(text=BTN_JPG)],
            [KeyboardButton(text=BTN_WEBP), KeyboardButton(text=BTN_COMPRESS)],
            [KeyboardButton(text=BTN_IMG_TO_PDF), KeyboardButton(text=BTN_PDF_TEXT)],
            [KeyboardButton(text=BTN_DOCX_TO_PDF), KeyboardButton(text=BTN_VIDEO_TO_AUDIO)],
            [KeyboardButton(text=BTN_AUDIO_CONVERT), KeyboardButton(text=BTN_MERGE_PDF)],
            [KeyboardButton(text=BTN_OCR), KeyboardButton(text=BTN_SUBSCRIPTION)],
        ],
        resize_keyboard=True,
    )


def waiting_for_file_keyboard(show_done: bool = False) -> ReplyKeyboardMarkup:
    rows = []
    if show_done:
        rows.append([KeyboardButton(text=BTN_DONE_MERGE)])
    rows.append([KeyboardButton(text=BTN_BACK)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def subscription_info_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_TARIFFS)], [KeyboardButton(text=BTN_BACK)]],
        resize_keyboard=True,
    )


def tariffs_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_BUY_PREMIUM)],
            [KeyboardButton(text=BTN_BUY_VIP)],
            [KeyboardButton(text=BTN_BACK)],
        ],
        resize_keyboard=True,
    )


async def main_menu_text(user_id: int) -> str:
    tier = await get_user_tier(user_id)
    tier_names = {"free": "Бесплатный", "premium": "Premium 💎", "vip": "VIP 👑"}
    return (
        "Привет! 👋 Я конвертирую файлы.\n\n"
        f"Твой тариф: {tier_names[tier]}\n"
        "Выбери функцию на клавиатуре ниже.\n\n"
        "🎬 Видео→аудио, 🎵 аудио, 📚 объединение PDF и 🔍 OCR — платные функции (жми «💎 Подписка», чтобы узнать больше)."
    )


SUBSCRIPTION_INFO_TEXT = (
    "💎 Подписки FileHub\n\n"
    "Premium:\n"
    "🎬 Видео → аудио (10/день)\n"
    "♾ 50 конвертаций в день\n"
    "📦 Файлы до 50 МБ\n\n"
    "VIP (всё из Premium +):\n"
    "🎵 Конвертация аудио форматов\n"
    "📚 Объединение нескольких PDF в один\n"
    "🔍 OCR — текст со сканов и фото документов\n"
    "♾ 150 конвертаций в день, видео→аудио 30/день"
)

TARIFFS_TEXT = (
    f"💰 Тарифы\n\n"
    f"Premium — {PRICES_STARS['premium']}⭐ / {SUBSCRIPTION_DAYS} дней\n"
    f"VIP — {PRICES_STARS['vip']}⭐ / {SUBSCRIPTION_DAYS} дней\n\n"
    "Оплата через Telegram Stars — нажми на нужный тариф ниже."
)


async def download_file_bytes(file_id: str) -> bytes:
    file = await bot.get_file(file_id)
    file_bytes = await bot.download_file(file.file_path)
    return file_bytes.read()


# ==== Команды и меню ====

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    pending_action.pop(user_id, None)
    pending_merge_files.pop(user_id, None)
    await message.answer(await main_menu_text(user_id), reply_markup=main_menu_keyboard())


@dp.message(F.text == BTN_SUBSCRIPTION)
async def show_subscription(message: types.Message):
    await message.answer(SUBSCRIPTION_INFO_TEXT, reply_markup=subscription_info_keyboard())


@dp.message(F.text == BTN_TARIFFS)
async def show_tariffs(message: types.Message):
    await message.answer(TARIFFS_TEXT, reply_markup=tariffs_keyboard())


@dp.message(F.text == BTN_BACK)
async def go_back(message: types.Message):
    user_id = message.from_user.id
    pending_action.pop(user_id, None)
    pending_merge_files.pop(user_id, None)
    await message.answer(await main_menu_text(user_id), reply_markup=main_menu_keyboard())


@dp.message(F.text == BTN_BUY_PREMIUM)
async def buy_premium(message: types.Message):
    await send_stars_invoice(message, "premium")


@dp.message(F.text == BTN_BUY_VIP)
async def buy_vip(message: types.Message):
    await send_stars_invoice(message, "vip")


async def send_stars_invoice(message: types.Message, tier: str):
    price_stars = PRICES_STARS[tier]
    title = "Подписка Premium" if tier == "premium" else "Подписка VIP"
    await bot.send_invoice(
        chat_id=message.chat.id,
        title=title,
        description=f"{title} на {SUBSCRIPTION_DAYS} дней в FileHub Converter",
        payload=f"sub_{tier}",
        provider_token="",  # для Telegram Stars всегда пустая строка
        currency="XTR",
        prices=[LabeledPrice(label=title, amount=price_stars)],
    )


@dp.pre_checkout_query()
async def process_pre_checkout(pre_checkout: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout.id, ok=True)


@dp.message(F.successful_payment)
async def process_successful_payment(message: types.Message):
    payload = message.successful_payment.invoice_payload
    tier = payload.replace("sub_", "")
    await grant_subscription(message.from_user.id, tier)
    tier_name = "Premium 💎" if tier == "premium" else "VIP 👑"
    await message.answer(
        f"Спасибо за покупку! Подписка {tier_name} активна на {SUBSCRIPTION_DAYS} дней. 🎉",
        reply_markup=main_menu_keyboard(),
    )


@dp.message(F.text == BTN_DONE_MERGE)
async def finish_merge(message: types.Message):
    user_id = message.from_user.id
    files = pending_merge_files.get(user_id, [])

    if len(files) < 2:
        await message.answer("Нужно минимум 2 PDF-файла, пришли ещё хотя бы один.")
        return

    await message.answer("Объединяю...")

    merged = fitz.open()
    for file_bytes in files:
        with fitz.open(stream=file_bytes, filetype="pdf") as part:
            merged.insert_pdf(part)
    output = io.BytesIO(merged.tobytes())
    merged.close()

    await message.answer_document(BufferedInputFile(output.getvalue(), filename="merged.pdf"))

    pending_merge_files.pop(user_id, None)
    pending_action.pop(user_id, None)
    await message.answer("Готово! Выбери следующую функцию:", reply_markup=main_menu_keyboard())


@dp.message(F.text.in_(BUTTON_TO_ACTION.keys()))
async def select_action(message: types.Message):
    user_id = message.from_user.id
    action = BUTTON_TO_ACTION[message.text]
    tier = await get_user_tier(user_id)

    if not user_has_access(action, tier):
        needed = "VIP 👑" if action in VIP_ONLY_ACTIONS else "Premium 💎 или VIP 👑"
        await message.answer(
            f"Эта функция доступна только с подпиской {needed}.\nЖми «💎 Подписка», чтобы узнать больше.",
            reply_markup=main_menu_keyboard(),
        )
        return

    pending_action[user_id] = action
    if action == "merge_pdf":
        pending_merge_files[user_id] = []
        await message.answer(ACTION_INFO[action][1], reply_markup=waiting_for_file_keyboard(show_done=True))
    else:
        await message.answer(ACTION_INFO[action][1], reply_markup=waiting_for_file_keyboard())


# ==== Обработка файлов: картинки ====

@dp.message(F.photo | (F.document & F.document.mime_type.startswith("image/")))
async def handle_image_upload(message: types.Message):
    user_id = message.from_user.id
    action = pending_action.get(user_id)

    if not action:
        await message.answer("Сначала выбери функцию в меню — напиши /start.")
        return

    expected_kind = ACTION_INFO[action][0]
    if expected_kind not in ("image", "image_or_pdf"):
        await message.answer("Сначала выбери функцию в меню — напиши /start.")
        return

    tier = await get_user_tier(user_id)
    size_limit = TIER_LIMITS[tier]["max_size_mb"]
    if message.document and message.document.file_size > size_limit * 1024 * 1024:
        await message.answer(f"Файл больше {size_limit} МБ, не могу обработать.")
        return

    daily_limit = TIER_LIMITS[tier]["daily"]
    if not await check_and_increment_limit(user_id, "usage", daily_limit):
        await message.answer(f"Дневной лимит ({daily_limit}) исчерпан. Попробуй завтра.")
        return

    file_id = message.photo[-1].file_id if message.photo else message.document.file_id
    file_bytes = await download_file_bytes(file_id)

    if action == "ocr":
        await message.answer("Распознаю текст...")
        text = ocr_image_bytes(file_bytes).strip()
        if not text:
            await message.answer("Не удалось распознать текст на этом изображении.")
        elif len(text) <= 4000:
            await message.answer(text)
        else:
            output = io.BytesIO(text.encode("utf-8"))
            await message.answer_document(BufferedInputFile(output.read(), filename="ocr_text.txt"))
        pending_action.pop(user_id, None)
        await message.answer("Готово! Выбери следующую функцию:", reply_markup=main_menu_keyboard())
        return

    await message.answer("Обрабатываю...")

    img = Image.open(io.BytesIO(file_bytes))
    if img.mode in ("RGBA", "P") and action == "img_jpg":
        img = img.convert("RGB")

    output = io.BytesIO()

    if action == "img_png":
        img.save(output, format="PNG")
        out_name = "converted.png"
    elif action == "img_jpg":
        img.save(output, format="JPEG", quality=90)
        out_name = "converted.jpg"
    elif action == "img_webp":
        img.save(output, format="WEBP")
        out_name = "converted.webp"
    elif action == "img_compress":
        if img.mode == "RGBA":
            img = img.convert("RGB")
        img.save(output, format="JPEG", quality=40, optimize=True)
        out_name = "compressed.jpg"
    elif action == "img_to_pdf":
        img_bytes_io = io.BytesIO()
        if img.mode == "RGBA":
            img = img.convert("RGB")
        img.save(img_bytes_io, format="JPEG")
        pdf_bytes = img2pdf.convert(img_bytes_io.getvalue())
        output = io.BytesIO(pdf_bytes)
        out_name = "converted.pdf"
    else:
        return

    output.seek(0)
    await message.answer_document(BufferedInputFile(output.read(), filename=out_name))
    pending_action.pop(user_id, None)
    await message.answer("Готово! Выбери следующую функцию:", reply_markup=main_menu_keyboard())


# ==== Обработка файлов: PDF ====

@dp.message(F.document & (F.document.mime_type == "application/pdf"))
async def handle_pdf_upload(message: types.Message):
    user_id = message.from_user.id
    action = pending_action.get(user_id)

    if action not in ("pdf_text", "merge_pdf", "ocr"):
        await message.answer("Сначала выбери функцию в меню — напиши /start.")
        return

    tier = await get_user_tier(user_id)
    size_limit = TIER_LIMITS[tier]["max_size_mb"]
    if message.document.file_size > size_limit * 1024 * 1024:
        await message.answer(f"Файл больше {size_limit} МБ, не могу обработать.")
        return

    file_bytes = await download_file_bytes(message.document.file_id)

    if action == "merge_pdf":
        pending_merge_files.setdefault(user_id, []).append(file_bytes)
        count = len(pending_merge_files[user_id])
        await message.answer(f"Добавлено ({count}). Пришли ещё PDF или нажми «Готово, объединить».")
        return

    daily_limit = TIER_LIMITS[tier]["daily"]
    if not await check_and_increment_limit(user_id, "usage", daily_limit):
        await message.answer(f"Дневной лимит ({daily_limit}) исчерпан. Попробуй завтра.")
        return

    if action == "ocr":
        await message.answer("Распознаю текст (это PDF-скан, может занять время)...")
        text = ocr_pdf_bytes(file_bytes).strip()
        if not text:
            await message.answer("Не удалось распознать текст в этом PDF.")
        elif len(text) <= 4000:
            await message.answer(text)
        else:
            output = io.BytesIO(text.encode("utf-8"))
            await message.answer_document(BufferedInputFile(output.read(), filename="ocr_text.txt"))
        pending_action.pop(user_id, None)
        await message.answer("Готово! Выбери следующую функцию:", reply_markup=main_menu_keyboard())
        return

    # action == "pdf_text"
    await message.answer("Извлекаю текст...")
    try:
        pdf_doc = fitz.open(stream=file_bytes, filetype="pdf")
        text_parts = [page.get_text("text") for page in pdf_doc]
        pdf_doc.close()
        full_text = "\n\n".join(text_parts).strip()
    except Exception as e:
        logging.error("PDF text extraction failed: %s", e)
        await message.answer("Не получилось извлечь текст из этого PDF.")
        return

    if not full_text:
        await message.answer("В этом PDF не нашлось текста (возможно, это скан — попробуй функцию OCR).")
    elif len(full_text) <= 4000:
        await message.answer(full_text)
    else:
        output = io.BytesIO(full_text.encode("utf-8"))
        await message.answer_document(BufferedInputFile(output.read(), filename="extracted_text.txt"))

    pending_action.pop(user_id, None)
    await message.answer("Готово! Выбери следующую функцию:", reply_markup=main_menu_keyboard())


# ==== Обработка файлов: DOCX ====

@dp.message(
    F.document
    & (F.document.mime_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
)
async def handle_docx_upload(message: types.Message):
    user_id = message.from_user.id
    action = pending_action.get(user_id)

    if action != "docx_to_pdf":
        await message.answer("Сначала выбери функцию в меню — напиши /start.")
        return

    tier = await get_user_tier(user_id)
    size_limit = TIER_LIMITS[tier]["max_size_mb"]
    if message.document.file_size > size_limit * 1024 * 1024:
        await message.answer(f"Файл больше {size_limit} МБ, не могу обработать.")
        return

    daily_limit = TIER_LIMITS[tier]["daily"]
    if not await check_and_increment_limit(user_id, "usage", daily_limit):
        await message.answer(f"Дневной лимит ({daily_limit}) исчерпан. Попробуй завтра.")
        return

    file_bytes = await download_file_bytes(message.document.file_id)
    await message.answer("Конвертирую, это может занять до минуты...")

    pdf_bytes = await asyncio.to_thread(convert_docx_to_pdf_bytes, file_bytes)

    if not pdf_bytes:
        await message.answer("Не получилось сконвертировать документ. Попробуй другой файл.")
    else:
        await message.answer_document(BufferedInputFile(pdf_bytes, filename="converted.pdf"))

    pending_action.pop(user_id, None)
    await message.answer("Готово! Выбери следующую функцию:", reply_markup=main_menu_keyboard())


# ==== Обработка файлов: видео ====

@dp.message(F.video | (F.document & F.document.mime_type.startswith("video/")))
async def handle_video_upload(message: types.Message):
    user_id = message.from_user.id
    action = pending_action.get(user_id)

    if action != "video_to_audio":
        await message.answer("Сначала выбери функцию в меню — напиши /start.")
        return

    tier = await get_user_tier(user_id)
    size_limit = TIER_LIMITS[tier]["max_size_mb"]
    file_obj = message.video or message.document
    if file_obj.file_size > size_limit * 1024 * 1024:
        await message.answer(f"Файл больше {size_limit} МБ, не могу обработать.")
        return

    video_limit = TIER_LIMITS[tier]["video_audio_daily"]
    if not await check_and_increment_limit(user_id, "video", video_limit):
        await message.answer(f"Дневной лимит на видео→аудио ({video_limit}) исчерпан. Попробуй завтра.")
        return

    file_bytes = await download_file_bytes(file_obj.file_id)
    await message.answer("Извлекаю звук из видео, это может занять время...")

    audio_bytes = await asyncio.to_thread(extract_audio_from_video, file_bytes)

    if not audio_bytes:
        await message.answer("Не получилось извлечь звук из этого видео.")
    else:
        await message.answer_document(BufferedInputFile(audio_bytes, filename="audio.mp3"))

    pending_action.pop(user_id, None)
    await message.answer("Готово! Выбери следующую функцию:", reply_markup=main_menu_keyboard())


# ==== Обработка файлов: аудио ====

@dp.message(F.audio | F.voice | (F.document & F.document.mime_type.startswith("audio/")))
async def handle_audio_upload(message: types.Message):
    user_id = message.from_user.id
    action = pending_action.get(user_id)

    if action != "audio_convert":
        await message.answer("Сначала выбери функцию в меню — напиши /start.")
        return

    tier = await get_user_tier(user_id)
    size_limit = TIER_LIMITS[tier]["max_size_mb"]
    file_obj = message.audio or message.voice or message.document
    if file_obj.file_size > size_limit * 1024 * 1024:
        await message.answer(f"Файл больше {size_limit} МБ, не могу обработать.")
        return

    daily_limit = TIER_LIMITS[tier]["daily"]
    if not await check_and_increment_limit(user_id, "usage", daily_limit):
        await message.answer(f"Дневной лимит ({daily_limit}) исчерпан. Попробуй завтра.")
        return

    file_bytes = await download_file_bytes(file_obj.file_id)
    await message.answer("Конвертирую в MP3...")

    result_bytes = await asyncio.to_thread(convert_audio_format, file_bytes, "mp3")

    if not result_bytes:
        await message.answer("Не получилось сконвертировать этот аудиофайл.")
    else:
        await message.answer_document(BufferedInputFile(result_bytes, filename="converted.mp3"))

    pending_action.pop(user_id, None)
    await message.answer("Готово! Выбери следующую функцию:", reply_markup=main_menu_keyboard())


@dp.message()
async def handle_other(message: types.Message):
    if message.from_user.id not in pending_action:
        await message.answer("Напиши /start, чтобы открыть меню.", reply_markup=main_menu_keyboard())
    else:
        await message.answer("Пришли файл нужного типа для выбранной функции, либо нажми «Назад».")


# ==== Веб-сервер для Render (health check) ====

async def health_check(request):
    return web.Response(text="Bot is running")


async def run_web_server():
    app = web.Application()
    app.router.add_get("/", health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logging.info("Health-check веб-сервер запущен на порту %s", PORT)


async def main():
    print("Бот запущен. Нажми Ctrl+C для остановки.")
    await run_web_server()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
