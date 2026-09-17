"""
Telegram-бот-конвертер файлов.

Возможности:
- Конвертация картинок: JPG / PNG / WEBP
- Сжатие картинок (уменьшение размера файла)
- Несколько картинок -> один PDF
- Извлечение текста из PDF

Токен берётся из переменной окружения TELEGRAM_TOKEN.
"""

import asyncio
import io
import logging
import os
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
from pypdf import PdfReader
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

pending_files: dict[int, dict] = {}
usage_tracker: dict[int, tuple[date, int]] = {}


def check_and_increment_limit(user_id: int) -> bool:
    today = date.today()
    last_date, count = usage_tracker.get(user_id, (today, 0))

    if last_date != today:
        count = 0

    if count >= DAILY_LIMIT:
        return False

    usage_tracker[user_id] = (today, count + 1)
    return True


def images_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🖼 В PNG", callback_data="img_png"),
                InlineKeyboardButton(text="🖼 В JPG", callback_data="img_jpg"),
                InlineKeyboardButton(text="🖼 В WEBP", callback_data="img_webp"),
            ],
            [
                InlineKeyboardButton(text="🗜 Сжать", callback_data="img_compress"),
                InlineKeyboardButton(text="📄 В PDF", callback_data="img_to_pdf"),
            ],
        ]
    )


def pdf_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📝 Извлечь текст", callback_data="pdf_text")]
        ]
    )


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "Привет! 👋 Я конвертирую файлы.\n\n"
        "📷 Пришли картинку — предложу конвертировать в другой формат, сжать или сделать PDF.\n"
        "📄 Пришли PDF — извлеку из него текст.\n\n"
        f"Лимит: {DAILY_LIMIT} конвертаций в день бесплатно."
    )


@dp.message(F.photo | (F.document & F.document.mime_type.startswith("image/")))
async def handle_image(message: types.Message):
    if message.photo:
        file_id = message.photo[-1].file_id
        file_name = "image.jpg"
    else:
        file_id = message.document.file_id
        file_name = message.document.file_name or "image.jpg"
        if message.document.file_size > MAX_FILE_SIZE_MB * 1024 * 1024:
            await message.answer(f"Файл больше {MAX_FILE_SIZE_MB} МБ, не могу обработать.")
            return

    file = await bot.get_file(file_id)
    file_bytes = await bot.download_file(file.file_path)

    pending_files[message.from_user.id] = {
        "bytes": file_bytes.read(),
        "kind": "image",
        "name": file_name,
    }

    await message.answer("Что сделать с картинкой?", reply_markup=images_keyboard())


@dp.message(F.document & (F.document.mime_type == "application/pdf"))
async def handle_pdf(message: types.Message):
    if message.document.file_size > MAX_FILE_SIZE_MB * 1024 * 1024:
        await message.answer(f"Файл больше {MAX_FILE_SIZE_MB} МБ, не могу обработать.")
        return

    file = await bot.get_file(message.document.file_id)
    file_bytes = await bot.download_file(file.file_path)

    pending_files[message.from_user.id] = {
        "bytes": file_bytes.read(),
        "kind": "pdf",
        "name": message.document.file_name or "file.pdf",
    }

    await message.answer("Что сделать с этим PDF?", reply_markup=pdf_keyboard())


@dp.callback_query(F.data.startswith("img_"))
async def process_image_action(callback: CallbackQuery):
    user_id = callback.from_user.id
    data = pending_files.get(user_id)

    if not data or data["kind"] != "image":
        await callback.answer("Файл не найден, пришли картинку заново.", show_alert=True)
        return

    if not check_and_increment_limit(user_id):
        await callback.answer(
            f"Дневной лимит ({DAILY_LIMIT}) исчерпан. Попробуй завтра.", show_alert=True
        )
        return

    await callback.answer("Обрабатываю...")
    action = callback.data
    img = Image.open(io.BytesIO(data["bytes"]))

    if img.mode in ("RGBA", "P") and action in ("img_jpg",):
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
    await callback.message.answer_document(
        BufferedInputFile(output.read(), filename=out_name)
    )
    pending_files.pop(user_id, None)


@dp.callback_query(F.data == "pdf_text")
async def process_pdf_text(callback: CallbackQuery):
    user_id = callback.from_user.id
    data = pending_files.get(user_id)

    if not data or data["kind"] != "pdf":
        await callback.answer("Файл не найден, пришли PDF заново.", show_alert=True)
        return

    if not check_and_increment_limit(user_id):
        await callback.answer(
            f"Дневной лимит ({DAILY_LIMIT}) исчерпан. Попробуй завтра.", show_alert=True
        )
        return

    await callback.answer("Извлекаю текст...")

    try:
        reader = PdfReader(io.BytesIO(data["bytes"]))
        text_parts = [page.extract_text() or "" for page in reader.pages]
        full_text = "\n\n".join(text_parts).strip()
    except Exception as e:
        logging.error("PDF text extraction failed: %s", e)
        await callback.message.answer("Не получилось извлечь текст из этого PDF.")
        return

    if not full_text:
        await callback.message.answer(
            "В этом PDF не нашлось текста (возможно, это скан/картинки)."
        )
        return

    if len(full_text) <= 4000:
        await callback.message.answer(full_text)
    else:
        output = io.BytesIO(full_text.encode("utf-8"))
        await callback.message.answer_document(
            BufferedInputFile(output.read(), filename="extracted_text.txt")
        )

    pending_files.pop(user_id, None)


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
