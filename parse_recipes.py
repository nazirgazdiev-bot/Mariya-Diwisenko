"""
Парсит PDF-сборник Марии Дивисенко в recipes.json.

Алгоритм:
1. Извлекаем оглавление (страницы 5-15) → список (название, страница)
2. Категории определяем по разделам оглавления
3. Для каждой страницы рецепта парсим: ингредиенты (▪), КБЖУ (Б/Ж/У - ...), инструкцию
4. Сохраняем структурированный JSON
"""

import json
import os
import re
import sys
from pathlib import Path

import pypdf

# Пути относительно расположения скрипта — кладёт recipes.json рядом.
# Можно переопределить через переменные окружения PDF_PATH и OUT_JSON.
PDF_PATH = os.environ.get(
    "PDF_PATH",
    "/sessions/wonderful-adoring-knuth/mnt/uploads/Сборник_полезных_рецептов_обновленный.pdf",
)
OUT_JSON = os.environ.get(
    "OUT_JSON",
    str(Path(__file__).parent / "recipes.json"),
)

# Категории (нормализованные) и слова-маркеры в шапке страницы
CATEGORIES = [
    ("Первые блюда", "ПЕРВЫЕ БЛЮДА", 18, 26),
    ("Блюда из мяса", "БЛЮДА ИЗ МЯСА", 28, 100),
    ("Блюда из рыбы", "БЛЮДА ИЗ МЯСА И РЫБЫ", 101, 117),
    ("Салаты, закуски, завтраки", "САЛАТЫ", 119, 186),
    ("Гарниры и каши", "ГАРНИРЫ И КАШИ", 188, 199),
    ("Десерты и сладкая выпечка", "ДЕСЕРТЫ", 201, 258),
]

# Регулярки
# Ловит и "Б/Ж/У - 5/0.2/3 29 Ккал", и "БЖУ - 9/4/9,2 112 Ккал", и "Б Ж У 5 0.2 3 29 ккал"
KBJU_RE = re.compile(
    r"Б\s*/?\s*Ж\s*/?\s*У\s*[-–—:]?\s*"
    r"([\d.,]+)\s*/\s*([\d.,]+)\s*/\s*([\d.,]+)\s+"
    r"([\d.,]+)\s*[Кк]кал",
    re.IGNORECASE,
)
# Запись типа "Куриное филе - 120 г" или "▪Куриное филе – 120 г"
INGREDIENT_RE = re.compile(r"^\s*▪?\s*(.+?)\s*[-–—]\s*(.+?)\s*$")

PAGE_HEADER_RE = re.compile(
    r"(ПЕРВЫЕ БЛЮДА|БЛЮДА ИЗ МЯСА И РЫБЫ|БЛЮДА ИЗ МЯСА|БЛЮДА ИЗ РЫБЫ|"
    r"САЛАТЫ[^\n]*ВЫПЕЧКА|ГАРНИРЫ И КАШИ|ДЕСЕРТЫ И СЛАДКАЯ ВЫПЕЧКА)\s+\d+"
)

# Маркеры порции — порядок важен (специфичные сначала). Дефолт = per_100g,
# по словам Маши: "В большинстве рецептов КБЖУ указаны на 100 гр готового блюда"
PORTION_PATTERNS = [
    # "на порцию" — самое жёсткое указание
    (re.compile(r"НА\s+ОДНУ\s+ПОРЦИЮ", re.IGNORECASE), "per_portion"),
    (re.compile(r"НА\s+ИТОГОВУЮ\s+ПОРЦИЮ", re.IGNORECASE), "per_portion"),
    (re.compile(r"НА\s+\d+\s+ПОРЦИ", re.IGNORECASE), "per_portion"),
    (re.compile(r"НА\s+ПОРЦИЮ", re.IGNORECASE), "per_portion"),
    (re.compile(r"1\s+ПОРЦИЯ\s*:", re.IGNORECASE), "per_portion"),
    # "на 100г" / "на 100 грамм"
    (re.compile(r"НА\s+100\s*Г(?:Р|РАММ|РАММОВ)?", re.IGNORECASE), "per_100g"),
]


def parse_toc(reader):
    """
    Парсит оглавление: страницы 5-15 (5 — первая с содержанием).
    Возвращает список dict: {name, category, page}.
    """
    # Собираем сырой текст оглавления
    toc_text = ""
    for i in range(4, 16):  # страницы 5-16 в 0-индексе
        toc_text += reader.pages[i].extract_text() + "\n"

    # Парсим: строки вида "▪Название . . . . . . 18"
    # Точки могут быть разделены пробелами — ловим оба варианта.
    pattern = re.compile(
        r"▪\s*(.+?)\s*[\s.]{5,}\s*(\d{1,3})(?=\s|$|\n)",
        re.MULTILINE,
    )
    matches = pattern.findall(toc_text)

    recipes = []
    seen_pages = set()
    for name, page in matches:
        page = int(page)
        if page in seen_pages:
            continue
        seen_pages.add(page)
        name = re.sub(r"\s+", " ", name.strip())
        # Определяем категорию по странице
        category = None
        for cat_name, _, start, end in CATEGORIES:
            if start <= page <= end:
                category = cat_name
                break
        recipes.append({"name": name, "page": page, "category": category or "Другое"})
    return recipes


def parse_kbju(text):
    """Возвращает {kcal, p, f, c} или None."""
    m = KBJU_RE.search(text)
    if not m:
        return None
    try:
        p = float(m.group(1).replace(",", "."))
        f = float(m.group(2).replace(",", "."))
        c = float(m.group(3).replace(",", "."))
        k = float(m.group(4).replace(",", "."))
        return {"kcal": k, "protein": p, "fat": f, "carbs": c}
    except ValueError:
        return None


def detect_portion(text):
    """
    Возвращает 'per_portion' если КБЖУ указан на всю порцию,
    'per_100g' если на 100г или маркер не найден (это дефолт у Маши).
    """
    for pattern, kind in PORTION_PATTERNS:
        if pattern.search(text):
            return kind
    return "per_100g"  # дефолт по словам Маши (страница 4 сборника)


def parse_ingredients(text):
    """Все строки начинающиеся с ▪ — ингредиенты."""
    ings = []
    for line in text.split("\n"):
        line = line.strip()
        if not line.startswith("▪"):
            continue
        body = line.lstrip("▪").strip()
        if len(body) < 3:
            continue
        # Пропускаем содержание (где есть много точек) и заголовки
        if "....." in body:
            continue
        ings.append(body)
    return ings


def extract_recipe_block(page_text, recipe_name):
    """
    Из текста страницы вырезает блок, относящийся к конкретному рецепту.
    На странице может быть несколько рецептов.
    """
    # Простой случай: один рецепт на страницу — возвращаем весь текст
    # Сложный случай: ищем название (название в верхнем регистре)
    # Пока возвращаем весь текст страницы — большинство рецептов одни на странице
    return page_text


def parse_instructions(text, ingredients):
    """Текст приготовления = весь текст минус строки ингредиентов и КБЖУ."""
    lines = text.split("\n")
    out_lines = []
    skip_words = ("ПРИГОТОВЛЕНИЕ", "ИНГРЕДИЕНТЫ", "НА ОДНУ ПОРЦИЮ", "На одну порцию", "НА 100", "ИНГРЕД")
    for line in lines:
        s = line.strip()
        if not s or s.startswith("▪"):
            continue
        # Пропускаем заголовки и КБЖУ
        if KBJU_RE.search(s):
            # вытаскиваем что после КБЖУ если есть
            kbju_m = KBJU_RE.search(s)
            cleaned = (s[:kbju_m.start()] + s[kbju_m.end():]).strip()
            if cleaned and len(cleaned) > 5:
                out_lines.append(cleaned)
            continue
        # Пропускаем шапку категории
        if PAGE_HEADER_RE.search(s):
            cleaned = PAGE_HEADER_RE.sub("", s).strip()
            if cleaned and len(cleaned) > 5:
                out_lines.append(cleaned)
            continue
        # Пропускаем явные служебные строки
        if any(w in s for w in skip_words) and len(s) < 30:
            continue
        # Если строка состоит только из заглавных и в ней нет инструкции — это название
        if s.isupper() and len(s) < 80:
            continue
        out_lines.append(s)
    text_out = " ".join(out_lines)
    # Чистим
    text_out = re.sub(r"\s{2,}", " ", text_out).strip()
    return text_out


def main():
    reader = pypdf.PdfReader(PDF_PATH)
    print(f"Открыт PDF, страниц: {len(reader.pages)}")

    recipes_meta = parse_toc(reader)
    print(f"Из оглавления извлечено рецептов: {len(recipes_meta)}")

    # Категории — статистика
    by_cat = {}
    for r in recipes_meta:
        by_cat.setdefault(r["category"], 0)
        by_cat[r["category"]] += 1
    print("\nПо категориям:")
    for c, n in by_cat.items():
        print(f"  {c}: {n}")

    # Парсим каждый рецепт
    recipes = []
    no_kbju = []
    for i, meta in enumerate(recipes_meta, 1):
        page_idx = meta["page"] - 1  # PDF страницы 1-индекс, питон 0-индекс
        if page_idx >= len(reader.pages):
            print(f"  WARN: страница {meta['page']} вне диапазона")
            continue
        page_text = reader.pages[page_idx].extract_text() or ""
        block = extract_recipe_block(page_text, meta["name"])

        kbju = parse_kbju(block)
        ingredients = parse_ingredients(block)
        instructions = parse_instructions(block, ingredients)
        portion = detect_portion(block)

        recipe = {
            "id": f"R{i:03d}",
            "name": meta["name"],
            "category": meta["category"],
            "page": meta["page"],
            "ingredients": ingredients,
            "instructions": instructions,
            "portion": portion,
        }
        if kbju:
            recipe.update(kbju)
        else:
            no_kbju.append(meta["name"])

        recipes.append(recipe)

    # Сохраняем
    out = {
        "title": "Сборник полезных рецептов — Мария Дивисенко",
        "categories": list(by_cat.keys()),
        "total": len(recipes),
        "recipes": recipes,
    }
    Path(OUT_JSON).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nСохранено {len(recipes)} рецептов в {OUT_JSON}")
    print(f"Без распознанного КБЖУ: {len(no_kbju)}")
    if no_kbju:
        print("Примеры без КБЖУ:")
        for n in no_kbju[:10]:
            print(f"  - {n}")


if __name__ == "__main__":
    main()
