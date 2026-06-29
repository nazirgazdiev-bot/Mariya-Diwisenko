"""
Inline-меню МарИИи: 3-уровневая навигация.

Структура menu_data["menu"]:
{
  "КАТЕГОРИЯ": {
    "ПОДКАТЕГОРИЯ": [recipe_ids]       # 2 уровня — сразу список
    или
    "ПОДКАТЕГОРИЯ": {                  # 3 уровня
       "ПОДПОДКАТЕГОРИЯ": [recipe_ids]
    }
  }
}

Принципы:
- Всегда работает с ОДНИМ сообщением — каждый клик правит то же сообщение.
- Кнопка «Закрыть» удаляет сообщение.
- Длинные списки рецептов делятся на страницы по 10.

Используем числовые индексы (категория ci, подкатегория si, подподкатегория ssi)
для компактности callback_data (лимит 64 байта в Telegram).
"""

from __future__ import annotations

import logging
from aiogram import Dispatcher, F
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

log = logging.getLogger("menu_ui")

PAGE_SIZE = 10

# Кэш загруженных данных
_MENU_DATA: dict = {}
_CAT_LIST: list[str] = []                              # ["Первые блюда", ...]
_SUBS_BY_CAT: dict[int, list[str]] = {}                # ci → [sub names]
# Для каждой пары (ci, si): либо list[str] (нет 3 уровня), либо list[str] подподкатегорий
_SUBSUBS_BY_PATH: dict[tuple[int, int], list[str] | None] = {}
# Списки id рецептов:
# Если 2 уровня: ключ (ci, si)
# Если 3 уровня: ключ (ci, si, ssi)
_RECIPES_BY_PATH: dict[tuple, list[str]] = {}
_RECIPE_BY_ID: dict[str, dict] = {}


def _init_data(menu_data: dict):
    global _MENU_DATA, _CAT_LIST, _SUBS_BY_CAT, _SUBSUBS_BY_PATH, _RECIPES_BY_PATH, _RECIPE_BY_ID
    _MENU_DATA = menu_data
    _CAT_LIST = list(menu_data["menu"].keys())
    _SUBS_BY_CAT = {}
    _SUBSUBS_BY_PATH = {}
    _RECIPES_BY_PATH = {}
    for ci, cat in enumerate(_CAT_LIST):
        subs = list(menu_data["menu"][cat].keys())
        _SUBS_BY_CAT[ci] = subs
        for si, sub in enumerate(subs):
            val = menu_data["menu"][cat][sub]
            if isinstance(val, list):
                _SUBSUBS_BY_PATH[(ci, si)] = None  # нет 3 уровня
                _RECIPES_BY_PATH[(ci, si)] = list(val)
            elif isinstance(val, dict):
                ssub_list = list(val.keys())
                _SUBSUBS_BY_PATH[(ci, si)] = ssub_list
                for ssi, ssub in enumerate(ssub_list):
                    _RECIPES_BY_PATH[(ci, si, ssi)] = list(val[ssub])
    _RECIPE_BY_ID = {r["id"]: r for r in menu_data["recipes"]}


def _count_in_sub(ci: int, si: int) -> int:
    """Сколько рецептов в подкатегории (со всеми подподкатегориями)."""
    if _SUBSUBS_BY_PATH.get((ci, si)) is None:
        return len(_RECIPES_BY_PATH.get((ci, si), []))
    total = 0
    for ssi in range(len(_SUBSUBS_BY_PATH[(ci, si)])):
        total += len(_RECIPES_BY_PATH.get((ci, si, ssi), []))
    return total


def _count_in_cat(ci: int) -> int:
    return sum(_count_in_sub(ci, si) for si in range(len(_SUBS_BY_CAT.get(ci, []))))


# ============ КЛАВИАТУРЫ ============

def kb_root() -> InlineKeyboardMarkup:
    rows = []
    for ci, cat in enumerate(_CAT_LIST):
        count = _count_in_cat(ci)
        rows.append([InlineKeyboardButton(
            text=f"{cat} ({count})",
            callback_data=f"m:c:{ci}",
        )])
    rows.append([InlineKeyboardButton(text="✕ Закрыть меню", callback_data="m:close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_subs(ci: int) -> InlineKeyboardMarkup:
    """Список подкатегорий в категории."""
    rows = []
    for si, sub in enumerate(_SUBS_BY_CAT.get(ci, [])):
        count = _count_in_sub(ci, si)
        if count == 0:
            continue
        # Если у этой подкатегории есть 3 уровень — идём на подподкатегории
        if _SUBSUBS_BY_PATH.get((ci, si)) is not None:
            cb = f"m:s:{ci}:{si}"
        else:
            # 2 уровня — сразу идём на список рецептов
            cb = f"m:l:{ci}:{si}:_:0"  # _ = нет подподкатегории
        rows.append([InlineKeyboardButton(
            text=f"{sub} ({count})",
            callback_data=cb,
        )])
    rows.append([InlineKeyboardButton(text="← К категориям", callback_data="m:root")])
    rows.append([InlineKeyboardButton(text="✕ Закрыть меню", callback_data="m:close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_subsubs(ci: int, si: int) -> InlineKeyboardMarkup:
    """Список подподкатегорий."""
    rows = []
    ssubs = _SUBSUBS_BY_PATH.get((ci, si)) or []
    for ssi, ssub in enumerate(ssubs):
        ids = _RECIPES_BY_PATH.get((ci, si, ssi), [])
        if not ids:
            continue
        rows.append([InlineKeyboardButton(
            text=f"{ssub} ({len(ids)})",
            callback_data=f"m:l:{ci}:{si}:{ssi}:0",
        )])
    rows.append([InlineKeyboardButton(text="← К подкатегориям", callback_data=f"m:c:{ci}")])
    rows.append([InlineKeyboardButton(text="✕ Закрыть меню", callback_data="m:close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_recipe_list(ci: int, si: int, ssi_raw: str, page: int) -> InlineKeyboardMarkup:
    """Список рецептов конкретной (sub) или (sub,subsub) с пагинацией."""
    if ssi_raw == "_":
        rids = _RECIPES_BY_PATH.get((ci, si), [])
    else:
        ssi = int(ssi_raw)
        rids = _RECIPES_BY_PATH.get((ci, si, ssi), [])

    total = len(rids)
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * PAGE_SIZE
    end = min(start + PAGE_SIZE, total)

    rows = []
    for rid in rids[start:end]:
        r = _RECIPE_BY_ID.get(rid)
        if not r:
            continue
        rows.append([InlineKeyboardButton(
            text=r["name"],
            callback_data=f"m:r:{rid}:{ci}:{si}:{ssi_raw}:{page}",
        )])

    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="← Стр", callback_data=f"m:l:{ci}:{si}:{ssi_raw}:{page-1}"))
        nav.append(InlineKeyboardButton(text=f"{page+1}/{total_pages}", callback_data="m:noop"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton(text="Стр →", callback_data=f"m:l:{ci}:{si}:{ssi_raw}:{page+1}"))
        rows.append(nav)

    # Кнопка «назад»: если 3 уровня — назад к подподкатегориям, иначе к подкатегориям
    if ssi_raw == "_":
        back_cb = f"m:c:{ci}"
        back_text = "← К подкатегориям"
    else:
        back_cb = f"m:s:{ci}:{si}"
        back_text = "← К подподкатегориям"
    rows.append([InlineKeyboardButton(text=back_text, callback_data=back_cb)])
    rows.append([InlineKeyboardButton(text="⌂ В главное меню", callback_data="m:root")])
    rows.append([InlineKeyboardButton(text="✕ Закрыть меню", callback_data="m:close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_recipe(rid: str, ci: int, si: int, ssi_raw: str, page: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="← К списку рецептов",
                              callback_data=f"m:l:{ci}:{si}:{ssi_raw}:{page}")],
        [InlineKeyboardButton(text="⌂ В главное меню", callback_data="m:root")],
        [InlineKeyboardButton(text="✕ Закрыть меню", callback_data="m:close")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ============ ТЕКСТЫ ============

def text_root() -> str:
    return (
        "📖 МЕНЮ РЕЦЕПТОВ\n\n"
        f"Всего рецептов: {_MENU_DATA['total']}\n"
        "Выбери категорию:"
    )


def text_subs(ci: int) -> str:
    cat = _CAT_LIST[ci]
    total = _count_in_cat(ci)
    return f"📂 {cat}\n\nВсего: {total}\nВыбери подкатегорию:"


def text_subsubs(ci: int, si: int) -> str:
    cat = _CAT_LIST[ci]
    sub = _SUBS_BY_CAT[ci][si]
    total = _count_in_sub(ci, si)
    return f"📂 {cat} → {sub}\n\nВсего: {total}\nВыбери подкатегорию:"


def text_recipe_list(ci: int, si: int, ssi_raw: str, page: int) -> str:
    cat = _CAT_LIST[ci]
    sub = _SUBS_BY_CAT[ci][si]
    if ssi_raw == "_":
        rids = _RECIPES_BY_PATH.get((ci, si), [])
        crumb = f"📂 {cat} → {sub}"
    else:
        ssi = int(ssi_raw)
        ssub = _SUBSUBS_BY_PATH[(ci, si)][ssi]
        rids = _RECIPES_BY_PATH.get((ci, si, ssi), [])
        crumb = f"📂 {cat} → {sub} → {ssub}"
    total = len(rids)
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    page_label = f" (стр. {page+1}/{total_pages})" if total_pages > 1 else ""
    return f"{crumb}\n\nРецептов: {total}{page_label}\nВыбери рецепт:"


def text_recipe(rid: str) -> str:
    r = _RECIPE_BY_ID.get(rid)
    if not r:
        return "Рецепт не найден."

    parts = [f"🍳 {r['name']}", ""]

    if "kcal" in r:
        portion = r.get("portion", "per_100g")
        label = "на порцию" if portion == "per_portion" else "на 100г"
        parts.append(f"📊 КБЖУ {label}:")
        parts.append(
            f"🔥 {r['kcal']:g} ккал  |  🥩 Б: {r['protein']:g}г  |  "
            f"🧈 Ж: {r['fat']:g}г  |  🍞 У: {r['carbs']:g}г"
        )
    else:
        parts.append("📊 КБЖУ: не указан (универсальное блюдо)")
    parts.append("")

    ings = r.get("ingredients") or []
    if ings:
        parts.append("🛒 Ингредиенты:")
        for ing in ings:
            parts.append(f"• {ing}")
        parts.append("")

    instr = (r.get("instructions") or "").strip()
    if instr:
        parts.append("👨‍🍳 ПРИГОТОВЛЕНИЕ")
        parts.append("")
        parts.append(instr)

    text = "\n".join(parts).strip()
    if len(text) > 4000:
        text = text[:3900] + "\n\n…(текст обрезан)"
    return text


# ============ НАВИГАЦИЯ ============

async def send_root_menu(message: Message):
    await message.answer(text_root(), reply_markup=kb_root(), parse_mode=None)


async def _safe_edit(query: CallbackQuery, text: str, kb: InlineKeyboardMarkup):
    try:
        await query.message.edit_text(text, reply_markup=kb, parse_mode=None)
    except Exception as e:
        log.warning("edit_text failed (%s) — sending new", e)
        try:
            await query.message.answer(text, reply_markup=kb, parse_mode=None)
        except Exception as e2:
            log.error("send new also failed: %s", e2)


# ============ HANDLERS ============

async def _on_root(query: CallbackQuery):
    await _safe_edit(query, text_root(), kb_root())
    await query.answer()


async def _on_cat(query: CallbackQuery):
    # data = "m:c:CI"
    try:
        _, _, ci = query.data.split(":")
        ci = int(ci)
    except (ValueError, IndexError):
        await query.answer("Сломанная кнопка", show_alert=True)
        return
    if ci < 0 or ci >= len(_CAT_LIST):
        await query.answer("Категория не найдена", show_alert=True)
        return
    await _safe_edit(query, text_subs(ci), kb_subs(ci))
    await query.answer()


async def _on_sub(query: CallbackQuery):
    # data = "m:s:CI:SI" — переход на подподкатегории
    try:
        _, _, ci, si = query.data.split(":")
        ci, si = int(ci), int(si)
    except (ValueError, IndexError):
        await query.answer("Сломанная кнопка", show_alert=True)
        return
    await _safe_edit(query, text_subsubs(ci, si), kb_subsubs(ci, si))
    await query.answer()


async def _on_list(query: CallbackQuery):
    # data = "m:l:CI:SI:SSI:PAGE" (SSI может быть "_" если 2 уровня)
    try:
        parts = query.data.split(":")
        _, _, ci, si, ssi_raw, page = parts
        ci, si, page = int(ci), int(si), int(page)
    except (ValueError, IndexError):
        await query.answer("Сломанная кнопка", show_alert=True)
        return
    await _safe_edit(query, text_recipe_list(ci, si, ssi_raw, page),
                     kb_recipe_list(ci, si, ssi_raw, page))
    await query.answer()


async def _on_recipe(query: CallbackQuery):
    # data = "m:r:RID:CI:SI:SSI:PAGE"
    try:
        parts = query.data.split(":")
        _, _, rid, ci, si, ssi_raw, page = parts
        ci, si, page = int(ci), int(si), int(page)
    except (ValueError, IndexError):
        await query.answer("Сломанная кнопка", show_alert=True)
        return
    await _safe_edit(query, text_recipe(rid), kb_recipe(rid, ci, si, ssi_raw, page))
    await query.answer()


async def _on_close(query: CallbackQuery):
    try:
        await query.message.delete()
    except Exception as e:
        log.warning("delete on close failed: %s", e)
        try:
            await query.message.edit_text("Меню закрыто.", reply_markup=None, parse_mode=None)
        except Exception:
            pass
    await query.answer("Меню закрыто")


async def _on_noop(query: CallbackQuery):
    await query.answer()


# ============ РЕГИСТРАЦИЯ ============

def register_menu(dp: Dispatcher, menu_data: dict):
    _init_data(menu_data)
    dp.callback_query.register(_on_root, F.data == "m:root")
    dp.callback_query.register(_on_close, F.data == "m:close")
    dp.callback_query.register(_on_noop, F.data == "m:noop")
    dp.callback_query.register(_on_cat, F.data.startswith("m:c:"))
    dp.callback_query.register(_on_sub, F.data.startswith("m:s:"))
    dp.callback_query.register(_on_list, F.data.startswith("m:l:"))
    dp.callback_query.register(_on_recipe, F.data.startswith("m:r:"))
    log.info(
        "Меню зарегистрировано: %d категорий, %d рецептов",
        len(_CAT_LIST), len(_RECIPE_BY_ID),
    )
