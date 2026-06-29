"""
МарИИя в режиме обычного Telegram-бота — для тестирования без Salebot.

Использует ту же логику (mariya.py + storage.py), что и FastAPI-версия.
Просто другой "front-end".

Запуск:
  python telegram_bot.py

Env:
  TELEGRAM_BOT_TOKEN — токен от @BotFather (создай ОТДЕЛЬНОГО тестового бота)
  ANTHROPIC_API_KEY  — Claude
  DB_PATH            — путь к SQLite
  RECIPES_PATH       — путь к recipes.json
  CLAUDE_MODEL       — модель (по умолчанию claude-sonnet-4-6)
"""

import asyncio
import json
import logging
import os
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction, ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from dotenv import load_dotenv

from mariya import Mariya
from menu_ui import register_menu, send_root_menu
from storage import Storage

load_dotenv()

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
DB_PATH = os.environ.get("DB_PATH", "mariya_data.db")
# menu.json содержит структуру меню (категории/подкатегории) + все рецепты с КБЖУ.
# Раньше использовался recipes.json — сейчас всё лежит в menu.json.
MENU_PATH = os.environ.get("MENU_PATH", os.environ.get("RECIPES_PATH", "menu.json"))
MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
LEARN_MODEL = os.environ.get("LEARN_MODEL", "claude-haiku-4-5-20251001")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("mariya-tg")

bot = Bot(
    token=TELEGRAM_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()

storage: Storage | None = None
mariya: Mariya | None = None


START_TEXT = (
    "Привет! Меня зовут МарИИя.\n"
    "Я твой AI-нутрициолог по сборнику рецептов Марии Дивисенко "
    "(232 рецепта, все полезные и быстрые).\n\n"
    "Что я умею:\n"
    "— Считать твою норму КБЖУ по методике Маши, если ты её ещё не знаешь\n"
    "— Составлять рационы на день, неделю, месяц под твои цели по КБЖУ\n"
    "— Подбирать блюда с учётом твоих предпочтений и аллергий\n"
    "— Заменять блюда в рационе на равноценные по КБЖУ\n"
    "— Считать граммовку каждого блюда, чтобы попасть в твою норму калорий\n"
    "— Помнить тебя между разговорами — твои цели, что любишь, что нет\n\n"
    "С чего начнём:\n\n"
    "1. Если ты УЖЕ знаешь свои ккал и БЖУ — просто скажи их и я составлю рацион.\n"
    "   Пример: «Хочу похудеть, 1500 ккал, БЖУ 120/50/130, не ем рыбу. "
    "Составь рацион на день».\n\n"
    "2. Если ты НЕ знаешь свою норму — попроси меня посчитать.\n"
    "   Пример: «Посчитай мне КБЖУ, хочу похудеть».\n"
    "   Я задам пару вопросов (вес, рост) и выдам твою норму.\n\n"
    "Команды:\n"
    "/menu — открыть меню рецептов (категории, подкатегории, сами рецепты)\n"
    "/help — это сообщение\n"
    "/profile — что я о тебе знаю\n"
    "/forget — стереть диалог и собранные факты (профиль сохранится)\n"
    "/reset — полный сброс как у нового клиента"
)


@dp.message(CommandStart())
async def cmd_start(message: Message):
    # parse_mode=None — приветствие в чистом тексте, без HTML-форматирования
    await message.answer(START_TEXT, parse_mode=None)


@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(START_TEXT, parse_mode=None)


@dp.message(Command("menu"))
async def cmd_menu(message: Message):
    """Открывает inline-меню рецептов."""
    await send_root_menu(message)


@dp.message(Command("profile"))
async def cmd_profile(message: Message):
    uid = str(message.from_user.id)
    client = await storage.get_client(uid)
    p = client.get("profile", {})
    facts = client.get("facts", [])

    parts = []
    parts.append(f"<b>Что я о тебе знаю</b>")
    parts.append(f"Имя: {client.get('name') or '—'}")
    parts.append(f"Цель: {p.get('goal') or '—'}")
    parts.append(
        f"Целевые ккал: {p.get('target_kcal') or '—'}\n"
        f"Целевые БЖУ: Б {p.get('target_protein') or '?'} / "
        f"Ж {p.get('target_fat') or '?'} / У {p.get('target_carbs') or '?'}"
    )
    if p.get("allergies"):
        parts.append(f"Аллергии: {', '.join(p['allergies'])}")
    if p.get("dislikes"):
        parts.append(f"Не любит: {', '.join(p['dislikes'])}")
    if p.get("likes"):
        parts.append(f"Любит: {', '.join(p['likes'])}")
    if p.get("notes"):
        parts.append(f"Заметки: {p['notes']}")

    if facts:
        parts.append(f"\n<b>Авто-собранные факты ({len(facts)}):</b>")
        for f in facts[:30]:
            parts.append(f"• {f['text']}")
    else:
        parts.append("\n<i>Авто-собранных фактов пока нет — поболтай со мной, я начну запоминать.</i>")

    await message.answer("\n".join(parts))


@dp.message(Command("forget"))
async def cmd_forget(message: Message):
    uid = str(message.from_user.id)
    await storage.clear_dialog(uid)
    await storage.clear_facts(uid)
    await message.answer("Окей, забыла диалог и собранные факты. Профиль сохранила.")


@dp.message(Command("reset"))
async def cmd_reset(message: Message):
    uid = str(message.from_user.id)
    await storage.full_reset(uid)
    await message.answer("Полный сброс выполнен. Я тебя не знаю — давай знакомиться заново.")


@dp.message(F.text)
async def on_text(message: Message):
    if not message.text or message.text.startswith("/"):
        return
    uid = str(message.from_user.id)
    name = message.from_user.full_name

    client = await storage.get_client(uid)
    if name and not client.get("name"):
        client["name"] = name
        await storage.upsert_client(uid, name, client["profile"])

    await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    history = await storage.get_dialog(uid, limit=40)
    reply = await mariya.chat(message.text, client, history)

    await storage.add_dialog(uid, "user", message.text)
    await storage.add_dialog(uid, "assistant", reply)

    # Фоновое авто-обучение
    asyncio.create_task(_auto_learn(uid, message.text, reply, client))

    # Без parse_mode, чтобы Telegram не пытался рендерить как HTML
    await message.answer(reply, parse_mode=None)


async def _auto_learn(uid: str, user_msg: str, assistant_msg: str, client: dict):
    try:
        existing = [f["text"] for f in client.get("facts", [])]
        p = client.get("profile", {})
        if p.get("target_kcal"):
            existing.append(f"Целевые калории: {p['target_kcal']}")
        if p.get("allergies"):
            existing.append(f"Аллергии: {', '.join(p['allergies'])}")
        if p.get("dislikes"):
            existing.append(f"Не любит: {', '.join(p['dislikes'])}")
        new_facts = await mariya.extract_facts(user_msg, assistant_msg, existing)
        if new_facts:
            await storage.add_facts(uid, new_facts)
            log.info("Auto-learned for %s: %s", uid, new_facts)
    except Exception as e:
        log.warning("auto_learn error: %s", e)


async def main():
    global storage, mariya

    menu_path = Path(MENU_PATH)
    if not menu_path.exists():
        raise RuntimeError(
            f"Меню не найдено: {menu_path}. "
            "Убедись что menu.json лежит рядом с telegram_bot.py."
        )
    menu_data = json.loads(menu_path.read_text(encoding="utf-8"))
    if "menu" not in menu_data:
        raise RuntimeError(
            f"Файл {menu_path} не содержит ключа 'menu' — это старый recipes.json. "
            "Обнови файл на свежий menu.json (он содержит структуру категорий)."
        )

    storage = Storage(DB_PATH)
    await storage.init()
    mariya = Mariya(
        anthropic_key=ANTHROPIC_API_KEY,
        recipes_data=menu_data,
        model=MODEL,
        learn_model=LEARN_MODEL,
    )

    # Регистрируем обработчики inline-меню
    register_menu(dp, menu_data)

    me = await bot.me()
    cats = len(menu_data["menu"])
    log.info(
        "МарИИя-Telegram @%s готова (model=%s, recipes=%d, категорий=%d, db=%s)",
        me.username, MODEL, menu_data["total"], cats, DB_PATH,
    )
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
