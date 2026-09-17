"""
Telegram-бот-конвертер файлов.

Возможности:
- Конвертация картинок: JPG / PNG / WEBP
- Сжатие картинок
- Несколько картинок -> один PDF
- Извлечение текста из PDF
- DOCX -> PDF (точно, через LibreOffice)

Навигация: сначала выбираешь функцию в меню, потом бот просит прислать файл.

Токен берётся из переменной окружения TELEGRAM_TOKEN.
"""

import asyncio
import io
import logging
import os
import subprocess
import tempfile
import uuid
from datetime import date

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from aiohttp import web
from PIL import Image
import fitz  # PyMuPDF
import img2pdf

# ==== НАСТРОЙКИ ====
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
MAX_FILE_SIZE_MB = 20
DAILY_LIMIT = 15
PORT = int(os.environ.get("PORT", 8080))
# ====================

logging.basicConfig(level=logging.INFO)

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

# Какую функцию пользователь выбрал и ждёт файл: {user_id: action}
pending_action: dict[int, str] = {}

# Учёт дневного лимита: {user_id: (дата, количество)}
usage_tracker: dict[int, tuple[date, int]] = {}

# Соответствие действия -> какой тип файла ожидается и текст-подсказка
ACTION_INFO = {
    "img_png": ("image", "Пришли картинку — сконвертирую в PNG."),
    "img_jpg": ("image", "Пришли картинку — сконвертирую в JPG."),
    "img_webp": ("image", "Пришли картинку — сконвертирую в WEBP."),
    "img_compress": ("image", "Пришли картинку — сожму её."),
    "img_to_pdf": ("image", "Пришли картинку — сделаю из неё PDF."),
    "pdf_text": ("pdf", "Пришли PDF-файл — извлеку из него текст."),
    "docx_to_pdf": ("docx", "Пришли Word-файл (.docx) — точно сконвертирую в PDF."),
}


def check_and_increment_limit(user_id: int) -> bool:
    today = date.today()
    last_date, count = usage_tracker.get(user_id, (today, 0))
    if last_date != today:
        count = 0
    if count >= DAILY_LIMIT:
        return False
    usage_tracker[user_id] = (today, count + 1)
    return True


def convert_docx_to_pdf_bytes(docx_bytes: bytes) -> bytes | None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        input_path = os.path.join(tmp_dir, f"{uuid.uuid4()}.docx")
        with open(input_path, "wb") as f:
            f.write(docx_bytes)
        try:
            subprocess.run(
                ["soffice", "--headless", "--convert-to", "pdf", "--outdir", tmp_dir, input_path],
                check=True,
                timeout=60,
                capture_output=True,
            )
        except Exception as e:
            logging.error("LibreOffice conversion failed: %s", e)
            return None

        output_path = input_path.replace(".docx", ".pdf")
        if not os.path.exists(output_path):
            return None
        with open(output_path, "rb") as f:
            return f.read()


# ==== Клавиатуры ====

def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🖼 В PNG", callback_data="select_img_png"),
                InlineKeyboardButton(text="🖼 В JPG", callback_data="select_img_jpg"),
                InlineKeyboardButton(text="🖼 В WEBP", callback_data="select_img_webp"),
            ],
            [
                InlineKeyboardButton(text="🗜 Сжать картинку", callback_data="select_img_compress"),
                InlineKeyboardButton(text="📄 Картинка → PDF", callback_data="select_img_to_pdf"),
            ],
            [
                InlineKeyboardButton(text="📝 Извлечь текст из PDF", callback_data="select_pdf_text"),
            ],
            [
                InlineKeyboardButton(text="📝 DOCX → PDF (точно)", callback_data="select_docx_to_pdf"),
            ],
            [
                InlineKeyboardButton(text="💎 Подписка", callback_data="show_subscription"),
            ],
        ]
    )


def waiting_for_file_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад в меню", callback_data="back_to_menu")]]
    )


def subscription_info_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💰 Тарифы", callback_data="show_tariffs")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_menu")],
        ]
    )


def tariffs_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Купить", callback_data="buy_subscription")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="show_subscription")],
        ]
    )


MAIN_MENU_TEXT = (
    "Привет! 👋 Я конвертирую файлы.\n\n"
    "Выбери функцию из меню ниже — потом пришлю запрос на файл.\n\n"
    f"Лимит: {DAILY_LIMIT} конвертаций в день бесплатно."
)

SUBSCRIPTION_INFO_TEXT = (
    "💎 Подписка FileHub\n\n"
    "Что даёт подписка (скоро):\n"
    "🎬 Конвертация видео и аудио\n"
    "📚 Объединение нескольких PDF в один\n"
    "♾ Без дневного лимита конвертаций\n"
    "📦 Увеличенный лимит размера файла\n\n"
    "🚧 Раздел в разработке."
)

TARIFFS_TEXT = (
    "💰 Тарифы\n\n"
    "Подписка: цена уточняется\n\n"
    "🚧 Оплата пока не настроена — раздел в разработке, следите за обновлениями!"
)


# ==== Хендлеры меню ====

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    pending_action.pop(message.from_user.id, None)
    await message.answer(MAIN_MENU_TEXT, reply_markup=main_menu_keyboard())


@dp.callback_query(F.data == "back_to_menu")
async def back_to_menu(callback: CallbackQuery):
    pending_action.pop(callback.from_user.id, None)
    await callback.answer()
    await callback.message.edit_text(MAIN_MENU_TEXT, reply_markup=main_menu_keyboard())


@dp.callback_query(F.data == "show_subscription")
async def show_subscription(callback: CallbackQuery):
    await callback.answer()
    await callback.message.edit_text(SUBSCRIPTION_INFO_TEXT, reply_markup=subscription_info_keyboard())


@dp.callback_query(F.data == "show_tariffs")
async def show_tariffs(callback: CallbackQuery):
    await callback.answer()
    await callback.message.edit_text(TARIFFS_TEXT, reply_markup=tariffs_keyboard())


@dp.callback_query(F.data == "buy_subscription")
async def buy_subscription(callback: CallbackQuery):
    await callback.answer(
        "Оплата пока не настроена, функция в разработке. Следите за обновлениями!",
        show_alert=True,
    )


@dp.callback_query(F.data.startswith("select_"))
async def select_action(callback: CallbackQuery):
    action = callback.data.removeprefix("select_")
    file_kind, prompt_text = ACTION_INFO[action]
    pending_action[callback.from_user.id] = action

    await callback.answer()
    await callback.message.edit_text(prompt_text, reply_markup=waiting_for_file_keyboard())


# ==== Обработка присланных файлов ====

async def download_file_bytes(file_id: str) -> bytes:
    file = await bot.get_file(file_id)
    file_bytes = await bot.download_file(file.file_path)
    return file_bytes.read()


@dp.message(F.photo | (F.document & F.document.mime_type.startswith("image/")))
async def handle_image_upload(message: types.Message):
    user_id = message.from_user.id
    action = pending_action.get(user_id)

    if not action or ACTION_INFO[action][0] != "image":
        await message.answer("Сначала выбери функцию в меню — напиши /start.")
        return

    if message.document and message.document.file_size > MAX_FILE_SIZE_MB * 1024 * 1024:
        await message.answer(f"Файл больше {MAX_FILE_SIZE_MB} МБ, не могу обработать.")
        return

    if not check_and_increment_limit(user_id):
        await message.answer(f"Дневной лимит ({DAILY_LIMIT}) исчерпан. Попробуй завтра.")
        return

    file_id = message.photo[-1].file_id if message.photo else message.document.file_id
    file_bytes = await download_file_bytes(file_id)

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


@dp.message(F.document & (F.document.mime_type == "application/pdf"))
async def handle_pdf_upload(message: types.Message):
    user_id = message.from_user.id
    action = pending_action.get(user_id)

    if action != "pdf_text":
        await message.answer("Сначала выбери функцию в меню — напиши /start.")
        return

    if message.document.file_size > MAX_FILE_SIZE_MB * 1024 * 1024:
        await message.answer(f"Файл больше {MAX_FILE_SIZE_MB} МБ, не могу обработать.")
        return

    if not check_and_increment_limit(user_id):
        await message.answer(f"Дневной лимит ({DAILY_LIMIT}) исчерпан. Попробуй завтра.")
        return

    file_bytes = await download_file_bytes(message.document.file_id)
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
        await message.answer("В этом PDF не нашлось текста (возможно, это скан/картинки).")
    elif len(full_text) <= 4000:
        await message.answer(full_text)
    else:
        output = io.BytesIO(full_text.encode("utf-8"))
        await message.answer_document(BufferedInputFile(output.read(), filename="extracted_text.txt"))

    pending_action.pop(user_id, None)
    await message.answer("Готово! Выбери следующую функцию:", reply_markup=main_menu_keyboard())


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

    if message.document.file_size > MAX_FILE_SIZE_MB * 1024 * 1024:
        await message.answer(f"Файл больше {MAX_FILE_SIZE_MB} МБ, не могу обработать.")
        return

    if not check_and_increment_limit(user_id):
        await message.answer(f"Дневной лимит ({DAILY_LIMIT}) исчерпан. Попробуй завтра.")
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


@dp.message()
async def handle_other(message: types.Message):
    if message.from_user.id not in pending_action:
        await message.answer("Напиши /start, чтобы открыть меню.")
    else:
        await message.answer("Пришли файл нужного типа для выбранной функции, либо нажми «Назад в меню».")


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
