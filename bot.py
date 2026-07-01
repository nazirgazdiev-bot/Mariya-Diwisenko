import asyncio
import json
import logging
import os
import re

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatAction, ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, Message, ReplyKeyboardMarkup,
)
from aiogram.client.default import DefaultBotProperties
from dotenv import load_dotenv

from mariya import Mariya
from storage import Storage

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
DB_PATH = os.environ.get("DB_PATH", "mariya_data.db")
MENU_PATH = os.environ.get("MENU_PATH", "menu.json")
PHOTOS_DIR = os.environ.get("PHOTOS_DIR", "photos")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("recipe-bot")

with open(MENU_PATH, "r", encoding="utf-8") as f:
    data = json.load(f)

MENU = data["menu"]  # произвольная вложенность: dict -> dict -> ... -> list[recipe_id]
RECIPES = {r["id"]: r for r in data["recipes"]}


# ─── Навигация по дереву меню произвольной глубины ──────────────────────────

def get_node(path: list[int]):
    """Возвращает узел дерева MENU по пути индексов, и название текущего узла."""
    node = MENU
    label = None
    for idx in path:
        keys = list(node.keys())
        label = keys[idx]
        node = node[label]
    return node, label

def encode_path(path: list[int]) -> str:
    return ",".join(map(str, path))

def decode_path(s: str) -> list[int]:
    return [int(x) for x in s.split(",")] if s else []


def main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🍽 Рецепты")],
            [KeyboardButton(text="🤖 Спросить МарИИю")],
        ],
        resize_keyboard=True,
    )

PAGE_SIZE = 8

def nav_title(path: list[int], page: int = 0, total_pages: int = 1) -> str:
    node, label = get_node(path)
    if not path:
        return "📂 <b>Выбери категорию:</b>"
    if isinstance(node, dict):
        return f"📁 <b>{label}</b>\n\nВыбери подкатегорию:"
    page_info = f" (стр. {page + 1}/{total_pages})" if total_pages > 1 else ""
    return f"📋 <b>{label}</b>{page_info}\n\nВыбери рецепт:"

def nav_keyboard(path: list[int], page: int = 0) -> InlineKeyboardMarkup:
    node, _ = get_node(path)
    buttons = []

    if isinstance(node, dict):
        for i, key in enumerate(node.keys()):
            child = node[key]
            if isinstance(child, dict):
                cb = f"nav:{encode_path(path + [i])}"
            else:
                cb = f"page:{encode_path(path + [i])}:0"
            buttons.append([InlineKeyboardButton(text=key, callback_data=cb)])
    elif isinstance(node, list):
        start = page * PAGE_SIZE
        chunk = node[start:start + PAGE_SIZE]
        for rid in chunk:
            if rid in RECIPES:
                name = RECIPES[rid]["name"]
                if len(name) > 40:
                    name = name[:37] + "..."
                buttons.append([InlineKeyboardButton(text=name, callback_data=f"rec:{rid}:{encode_path(path)}:{page}")])

        total_pages = max(1, (len(node) - 1) // PAGE_SIZE + 1)
        if total_pages > 1:
            nav_row = []
            if page > 0:
                nav_row.append(InlineKeyboardButton(text="⬅️", callback_data=f"page:{encode_path(path)}:{page - 1}"))
            nav_row.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="noop"))
            if page < total_pages - 1:
                nav_row.append(InlineKeyboardButton(text="➡️", callback_data=f"page:{encode_path(path)}:{page + 1}"))
            buttons.append(nav_row)

    if path:
        back_path = path[:-1]
        buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"nav:{encode_path(back_path)}")])

    return InlineKeyboardMarkup(inline_keyboard=buttons)

def recipe_keyboard(path: list[int], page: int = 0) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад к списку", callback_data=f"page:{encode_path(path)}:{page}")]
    ])


def format_recipe(recipe: dict) -> str:
    name = recipe.get("name", "")
    kcal = recipe.get("kcal", "—")
    protein = recipe.get("protein", "—")
    fat = recipe.get("fat", "—")
    carbs = recipe.get("carbs", "—")
    portion = recipe.get("portion", "")

    ingredients = recipe.get("ingredients", [])
    ingredients_text = "\n".join(f"• {ing}" for ing in ingredients)

    instructions = recipe.get("instructions", "").strip()
    instructions = instructions.replace("ПРИГОТОВЛЕНИЕ", "").strip()

    if "\n" not in instructions and len(instructions) > 200:
        sentences = re.split(r'(?<=[.!?])\s+', instructions)
        chunks = []
        current = ""
        for s in sentences:
            if len(current) + len(s) > 300:
                if current:
                    chunks.append(current.strip())
                current = s
            else:
                current += " " + s
        if current:
            chunks.append(current.strip())
        instructions = "\n\n".join(chunks)

    has_kcal = "kcal" in recipe
    portion_label = {
        "per_100g": "на 100 г",
        "per_portion": "на порцию",
    }.get(portion, "")

    text = f"<b>🍳 {name}</b>\n\n"
    if has_kcal:
        suffix = f" ({portion_label})" if portion_label else ""
        text += f"📊 <b>КБЖУ{suffix}:</b>\n"
        text += f"🔥 {kcal} ккал  |  🥩 Б: {protein}г  |  🧈 Ж: {fat}г  |  🍞 У: {carbs}г\n\n"
    text += f"🛒 <b>Ингредиенты:</b>\n{ingredients_text}\n\n"
    text += f"👨‍🍳 <b>Приготовление:</b>\n{instructions}"

    if len(text) > 1000:
        text = text[:997] + "..."

    return text

def get_photo_path(recipe_id: str) -> str | None:
    path = os.path.join(PHOTOS_DIR, f"{recipe_id}.jpg")
    return path if os.path.exists(path) else None


bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
storage: Storage = None
mariya: Mariya = None


async def safe_delete(message: Message):
    try:
        await message.delete()
    except Exception:
        pass


@dp.message(CommandStart())
async def cmd_start(message: Message):
    if storage:
        uid = str(message.from_user.id)
        name = message.from_user.first_name
        client = await storage.get_client(uid)
        if not client.get("name"):
            await storage.upsert_client(uid, name, client.get("profile", {}))
    await message.answer(
        "👋 Привет! Это сборник полезных рецептов.\n\n"
        "🍽 <b>Рецепты</b> — рецепты по категориям\n"
        "🤖 <b>Спросить МарИИю</b> — AI-нутрициолог\n\n"
        "Выбирай 👇",
        reply_markup=main_keyboard(),
    )

@dp.message(Command("profile"))
async def cmd_profile(message: Message):
    if not storage:
        return
    uid = str(message.from_user.id)
    client = await storage.get_client(uid)
    profile = client.get("profile", {})
    facts = client.get("facts", [])
    lines = [f"👤 <b>Профиль {client.get('name') or 'клиента'}</b>\n"]
    if profile.get("goal"):
        lines.append(f"Цель: {profile['goal']}")
    if profile.get("target_kcal"):
        lines.append(f"Калории: {profile['target_kcal']} ккал/день")
    if facts:
        lines.append("\n📝 <b>Что я помню:</b>")
        for f in facts[:10]:
            lines.append(f"• {f['text']}")
    if len(lines) == 1:
        lines.append("(пока ничего не знаю о тебе)")
    await message.answer("\n".join(lines))

@dp.message(Command("forget"))
async def cmd_forget(message: Message):
    if storage:
        await storage.clear_dialog(str(message.from_user.id))
    await message.answer("История диалога очищена.")

@dp.message(Command("reset"))
async def cmd_reset(message: Message):
    if storage:
        await storage.full_reset(str(message.from_user.id))
    await message.answer("Полный сброс выполнен.")


@dp.message(F.text == "🍽 Рецепты")
async def show_categories(message: Message):
    await message.answer(nav_title([]), reply_markup=nav_keyboard([]))

@dp.callback_query(F.data.startswith("nav:"))
async def navigate(callback: CallbackQuery):
    path = decode_path(callback.data[4:])
    await safe_delete(callback.message)
    await callback.message.answer(nav_title(path), reply_markup=nav_keyboard(path))
    await callback.answer()

@dp.callback_query(F.data == "noop")
async def noop(callback: CallbackQuery):
    await callback.answer()

@dp.callback_query(F.data.startswith("page:"))
async def navigate_page(callback: CallbackQuery):
    rest = callback.data[5:]
    path_str, page_str = rest.rsplit(":", 1)
    path = decode_path(path_str)
    page = int(page_str)
    node, _ = get_node(path)
    total_pages = max(1, (len(node) - 1) // PAGE_SIZE + 1) if isinstance(node, list) else 1
    await safe_delete(callback.message)
    await callback.message.answer(
        nav_title(path, page, total_pages),
        reply_markup=nav_keyboard(path, page),
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("rec:"))
async def show_recipe(callback: CallbackQuery):
    rest = callback.data[4:]
    parts = rest.split(":")
    recipe_id = parts[0]
    page = int(parts[-1])
    path = decode_path(":".join(parts[1:-1]))

    recipe = RECIPES.get(recipe_id)
    if not recipe:
        await callback.answer("Рецепт не найден")
        return

    text = format_recipe(recipe)
    keyboard = recipe_keyboard(path, page)
    photo_path = get_photo_path(recipe_id)

    await safe_delete(callback.message)

    if photo_path:
        await callback.message.answer_photo(
            photo=FSInputFile(photo_path),
            caption=text,
            reply_markup=keyboard,
            protect_content=True,
        )
    else:
        await callback.message.answer(
            text,
            reply_markup=keyboard,
            protect_content=True,
        )
    await callback.answer()


@dp.message(F.text == "🤖 Спросить МарИИю")
async def mariya_intro(message: Message):
    await message.answer(
        "🤖 <b>МарИИя — AI-нутрициолог</b>\n\n"
        "Я помогу:\n"
        "• Посчитать твою норму КБЖУ\n"
        "• Составить рацион по целям\n"
        "• Подобрать рецепты под аллергии\n\n"
        "Просто напиши что нужно 👇\n\n"
        "<i>/profile — профиль | /forget — очистить диалог</i>"
    )

@dp.message(F.text)
async def handle_text(message: Message):
    if not mariya or not storage:
        await message.answer("Ассистент временно недоступен.")
        return

    uid = str(message.from_user.id)
    user_text = message.text.strip()

    await bot.send_chat_action(message.chat.id, ChatAction.TYPING)

    client = await storage.get_client(uid)
    history = await storage.get_dialog(uid, limit=40)

    reply = await mariya.chat(user_text, client, history)

    await storage.add_dialog(uid, "user", user_text)
    await storage.add_dialog(uid, "assistant", reply)

    await message.answer(reply, parse_mode=None)

    existing_facts = [f["text"] for f in client.get("facts", [])]
    new_facts = await mariya.extract_facts(user_text, reply, existing_facts)
    if new_facts:
        await storage.add_facts(uid, new_facts)


async def main():
    global storage, mariya
    storage = Storage(DB_PATH)
    await storage.init()
    mariya = Mariya(
        anthropic_key=ANTHROPIC_API_KEY,
        recipes_data=data,
        model=os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6"),
    )
    log.info("Бот запущен")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
