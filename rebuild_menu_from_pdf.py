"""Пересобирает поля рецептов из видимого слоя оригинального PDF.

В исходном PDF некоторые страницы содержат невидимые объекты соседних страниц
за границами листа. Обычный extract_text() захватывает их и перемешивает
рецепты. Здесь сначала отбрасываются все объекты вне видимой области страницы.

ID, названия, страницы, категории и menu_paths берутся из существующего
menu.json. Из PDF заново извлекаются ингредиенты, приготовление и КБЖУ.
Нестандартные страницы без заголовка «ПРИГОТОВЛЕНИЕ», а также страницы
с несколькими рецептами или вариантами КБЖУ задаются ручными правками ниже.
"""

import argparse
import json
import re
from pathlib import Path

import pdfplumber


KBJU_RE = re.compile(
    r"(?P<kcal>\d+(?:[.,]\d+)?)\s*[Кк]кал"
    r"\s*[-–—:]?\s*"
    r"\s*Б\s*/?\s*Ж\s*/?\s*У\s*[-–—:]?\s*"
    r"(?P<protein>\d+(?:[.,]\d+)?)\s*/\s*"
    r"(?P<fat>\d+(?:[.,]\d+)?)\s*/\s*"
    r"(?P<carbs>\d+(?:[.,]\d+)?)",
    re.IGNORECASE,
)

BJU_RE = re.compile(
    r"Б\s*/?\s*Ж\s*/?\s*У\s*[-–—:]?\s*"
    r"(?P<protein>\d+(?:[.,]\d+)?)\s*/\s*"
    r"(?P<fat>\d+(?:[.,]\d+)?)\s*/\s*"
    r"(?P<carbs>\d+(?:[.,]\d+)?)",
    re.IGNORECASE,
)

MANUAL_OVERRIDES = {
    "R056": {
        "ingredients": [],
        "kbju_variants": [
            {
                "label": "Свинина",
                "kcal": 328,
                "protein": 20.2,
                "fat": 25,
                "carbs": 0.9,
            },
            {
                "label": "Говядина",
                "kcal": 254,
                "protein": 25.4,
                "fat": 16.5,
                "carbs": 0.9,
            },
        ],
    },
    "R090": {
        "ingredients": [
            "Филе трески или минтая (или фарш) – 500 гр",
            "Яйцо – 1 шт",
            "Сливочное масло – 15 гр",
            "Кукурузный крахмал – 1 ст/л (при необходимости)",
            "ДЛЯ ПАНИРОВКИ:",
            "Яйца – 2 шт",
            "Мука пшеничная",
            "Панировочные сухари",
            "Соль, паприка",
            "ДЛЯ СОУСА:",
            "Натуральный йогурт/сметана",
            "Маринованные огурчики",
            "Укроп",
            "Сок лимона",
        ],
        "instructions": (
            "Рыбное филе пропустить через мясорубку (или взять готовый "
            "рыбный фарш), добавить натертое на крупной терке сливочное "
            "масло (желательно предварительно его подержать в морозилке), "
            "яйцо, соль и специи по вкусу, все перемешать.\n\n"
            "Из подготовленного фарша сформировать палочки примерно 2,5 см. "
            "толщиной (форму удобно придавать широкой стороной ножа). Если "
            "фарш немного разваливается, добавить крахмал.\n\n"
            "Для панировки приготовить 3 отдельные мисочки: со взбитыми "
            "(немного) яйцами, мукой с солью и сухарями с паприкой. Каждую "
            "палочку обвалять со всех сторон сперва в муке, затем окунуть "
            "в яйца и в конце обвалять в сухарях. Подготовленные таким "
            "образом заготовки выложить в форму или на плоскую тарелку, "
            "застеленные фольгой, и оставить в холоде на 30 мин. (это "
            "необходимо, чтобы панировка стала плотной и хорошо держалась).\n\n"
            "Палочки запекаем в духовке 20-25 минут на 200 градусах."
        ),
        "kbju_label": "НА 100 ГР (БЕЗ СОУСА)",
    },
    "R114": {
        "kbju_variants": [
            {
                "label": "На 100 г",
                "kcal": 257,
                "protein": 12,
                "fat": 13,
                "carbs": 22,
            },
            {
                "label": "НА 1 ЧЕБУРЕК",
                "kcal": 363,
                "protein": 16,
                "fat": 18,
                "carbs": 31,
            },
        ],
    },
    "R143": {
        "ingredients": [
            "ПП-МАЙОНЕЗ ИЗ ЙОГУРТА (СМЕТАНЫ):",
            "Йогурт натуральный (сметана не более 15%) — 100 г",
            "Дижонская горчица — 1 ч. л.",
            "Лимонный сок — 1 ч. л.",
            "Соль — по вкусу",
            "ПП-МАЙОНЕЗ ИЗ МЯГКОГО ТВОРОГА:",
            "Творог мягкий — 150 г",
            "Горчица — 1 ч. л.",
            "Лимонный сок — 1 ч. л.",
            "Оливковое масло – 1 ч. л.",
            "Соль — по вкусу",
        ],
        "instructions": (
            "В блендере смешайте все ингредиенты, я размешиваю просто "
            "вилкой) Такая заправка отлично подойдет к легким овощным "
            "салатам.\n\n"
            "Все ингредиенты смешать блендером (можно и так), по желанию "
            "можно добавить сушеные травы и чеснок. Эта заправка подойдет "
            "как отличная замену покупному майонезу в любом салате, где "
            "нужен именно майонез за счет своей густой консистенции, а на "
            "вкус и с точки зрения пользы гораздо лучше!)"
        ),
    },
    "R178": {
        "ingredients": [],
        "instructions": (
            "Начинка в овсяный и рисовый блинчик может быть совершенно "
            "любая! Как сытная, так и сладкая ягодно-фруктовая. Или же "
            "блинчики можно подать просто с вареньем, сметаной или медом. "
            "Для вашего удобства я подготовила варианты начинок для "
            "пп-блинчиков.\n\n"
            "Сытные:\n"
            "Ветчина, сыр, листья салата\n"
            "Куриное филе, сыр, помидоры\n"
            "Консервированный тунец, сыр/творожный сыр, листья салата, зелень\n"
            "Консервированный тунец, отварное яйцо, зелень\n"
            "Красная малосоленая рыба, творожный сыр, зелень, листья салата\n"
            "Помидоры, сыр\n"
            "Фарш, лук, сыр\n"
            "Шампиньоны, лук, сыр\n"
            "Слабосоленая рыба, творожный сыр, авокадо\n"
            "Творог, чеснок, зелень\n"
            "Свежие овощи, творожный сыр\n\n"
            "Сладкие:\n"
            "Творог, банан, натуральный йогурт/сметана\n"
            "Мягкий творог, ягоды, мед\n"
            "Творог, варенье\n"
            "Банан, ореховая паста\n"
            "Сыр рикотта, груша, мед\n"
            "Натуральный йогурт, ягоды/фрукты\n"
            "Яблочное пюре, корица\n"
            "Мягкий творог, сухофрукты, орехи"
        ),
    },
    "R179": {
        "ingredients": [
            "Банан – 120 г (1 шт)",
            "Рисовая мука - 2 ст. ложки",
            "Яйца – 2 шт",
            "Сах. зам",
        ],
        "instructions": (
            "Банан размять вилкой или в блендере. Яйца взбить вилкой и "
            "соединить с банановым пюре. Добавить муку и смешать до "
            "однородности. Раскалить сковороду. Жарить панкейки на сухой "
            "сковороде по 2-3 минуты с каждой стороны."
        ),
        "kcal": 126,
        "protein": 7,
        "fat": 5.5,
        "carbs": 11,
        "kbju_label": "На 100 г",
    },
    "R182": {
        "ingredients": [
            "Яичный белок - 100 г",
            "Кокосовая стружка - 80 г",
            "Сахарозаменитель - 4-6 г",
        ],
        "instructions": (
            "Яичные белки взбить до пиков с сахарозаменителем. Ввести "
            "кокосовую стружку, постепенно перемешивая венчиком. Дальше "
            "ложечкой или руками лепим шарики или пирамидки и выкладываем "
            "на силиконовый коврик/пергамент. Запекаем при 180 градусах "
            "10-12 минут, как зарумянятся - готово. Всего получилось "
            "12 штучек, а значит в одной 36 ккал."
        ),
        "kcal": 310,
        "protein": 13,
        "fat": 24,
        "carbs": 9,
        "kbju_label": "На 100 г",
    },
    "R183": {
        "name": "Панкейки (мини-панкейки)",
        "ingredients": [
            "Яйцо - 2 шт",
            "Сах. зам",
            "Молоко - 125 г",
            "Мука - 180 г",
            "Разрыхлитель - 10 г",
            "Ванилин - 10 г",
        ],
        "instructions": (
            "Все ингредиенты смешать и взболтать венчиком. Жарить на "
            "антипригарной сковороде без добавления масла на среднем огне. "
            "Чтобы получились мини-панкейки (их очень любят дети) – "
            "выкладывайте смесь на сковороду чайной ложечкой)."
        ),
        "kcal": 202,
        "protein": 8,
        "fat": 4,
        "carbs": 33,
        "kbju_label": "На 100 г",
    },
    "R195": {
        "ingredients": [
            "Хлебцы рисовые (любые, у меня Dr Korner) - 4 шт",
            "Протеин (любой) - 30 гр / или мягкий творог - 150 гр",
            "Банан/ягоды - по желанию",
            "Кофе черный любой",
        ],
        "instructions": (
            "ДВА ВАРИАНТА КРЕМА:\n"
            "Разводим протеин с небольшим количеством воды так, чтобы "
            "получилась консистенция густой сметаны (можно развести с "
            "обезжиренным кефиром!)\n"
            "Мягкий нежирный творог смешиваем с сахарозаменителем.\n\n"
            "Завариваем, варим или разводим черный кофе. Обмакиваем хлебцы "
            "в кофе и выкладываем на дно первый слой (у меня на первый слой "
            "ушло 2 хлебца - можно делать один хлебец - один слой, тогда "
            "получится 4 слоя и тортик будет повыше. Промазываем первый "
            "слой кремом, выкладываем нарезанный кружочками банан или ягоды "
            "(по желанию). Обмакиваем оставшиеся 2 хлебца в кофе и "
            "выкладываем второй слой. Так же промазываем сверху кремом и "
            "выкладываем банан/ягоды по желанию. Сверху можно посыпать "
            "орешками/тертым шоколадом/лепестками миндаля.\n\n"
            "Даем нашему тортику немного пропитаться в холодильнике.\n\n"
            "Приятного аппетита - это неожиданно очень сильно вкусно"
        ),
        "kbju_label": "На всю порцию (с творогом)",
    },
}


def visible_page(page):
    """Оставляет только реально видимые внутри страницы PDF-объекты."""
    return page.filter(
        lambda obj: (
            obj.get("x0", 0) >= 0
            and obj.get("x1", page.width) <= page.width
            and obj.get("top", 0) >= 0
            and obj.get("bottom", page.height) <= page.height
        )
    )


def normalize_block(text: str) -> str:
    """Склеивает переносы макета, не меняя содержание рецепта."""
    text = re.sub(r"(?<=\w)-\s*\n\s*(?=\w)", "", text)
    text = re.sub(r"\s*\n\s*", " ", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def _kbju_label(text: str, match: re.Match) -> str:
    line_start = text.rfind("\n", 0, match.start()) + 1
    prefix = text[line_start : match.start()].strip(" :–—-")
    candidates = [prefix]
    before = text[:line_start].rstrip()
    if before:
        candidates.append(before.split("\n")[-1].strip(" :–—-"))

    for candidate in candidates:
        if re.match(
            r"(?i)^(?:на\s+(?:100|1\b|один|одну|всю|весь|все|"
            r"полученную|порцию|7\b)|в\s+100\b)",
            candidate,
        ):
            return candidate.rstrip(":")
    return "На 100 г"


def parse_kbju(text: str) -> dict:
    matches = list(KBJU_RE.finditer(text))
    if not matches:
        match = BJU_RE.search(text)
        if not match:
            return {}

        def bju_number(name: str) -> float:
            return float(match.group(name).replace(",", "."))

        return {
            "protein": bju_number("protein"),
            "fat": bju_number("fat"),
            "carbs": bju_number("carbs"),
            "kbju_label": _kbju_label(text, match),
        }
    match = matches[0]

    def number(name: str) -> float:
        return float(match.group(name).replace(",", "."))

    return {
        "kcal": number("kcal"),
        "protein": number("protein"),
        "fat": number("fat"),
        "carbs": number("carbs"),
        "kbju_label": _kbju_label(text, match),
    }


def detect_portion(text: str) -> str:
    upper = text.upper()
    if re.search(r"НА\s+100\s*Г", upper):
        return "per_100g"
    if re.search(
        r"(НА\s+(ОДНУ|ОДИН|1|ВСЮ|ВСЕ|ИТОГОВУЮ)\s+"
        r"(ПОРЦИЮ|ЧЕБУРЕК|БЛЮДО|БРАУНИ|ПИРОГ|ТОРТ)|НА\s+ПОРЦИЮ)",
        upper,
    ):
        return "per_portion"
    return "per_100g"


def ingredient_blocks(page, prep_top: float) -> list[str]:
    """Извлекает маркированные ингредиенты с учётом двух колонок."""
    words = page.extract_words(x_tolerance=2, y_tolerance=3)
    bullets = [
        word
        for word in words
        if word["text"] == "▪" and word["top"] < prep_top
    ]
    if not bullets:
        return []

    column_groups: list[list[dict]] = []
    for bullet in sorted(bullets, key=lambda item: item["x0"]):
        if not column_groups:
            column_groups.append([bullet])
            continue
        center = sum(item["x0"] for item in column_groups[-1]) / len(
            column_groups[-1]
        )
        if abs(bullet["x0"] - center) > 35:
            column_groups.append([bullet])
        else:
            column_groups[-1].append(bullet)

    centers = [
        sum(item["x0"] for item in group) / len(group)
        for group in column_groups
    ]
    extracted = []
    for column_index, group in enumerate(column_groups):
        left = max(0, centers[column_index] - 5)
        right = (
            page.width
            if column_index + 1 == len(column_groups)
            else centers[column_index + 1] - 5
        )
        group = sorted(group, key=lambda item: item["top"])
        for index, bullet in enumerate(group):
            bottom = (
                group[index + 1]["top"] - 1
                if index + 1 < len(group)
                else prep_top - 1
            )
            if bottom <= bullet["top"]:
                continue
            block = page.crop(
                (
                    left,
                    max(0, bullet["top"] - 1),
                    right,
                    min(page.height, bottom),
                )
            )
            text = normalize_block(
                block.extract_text(x_tolerance=2, y_tolerance=3) or ""
            ).strip(" ▪")
            if text:
                extracted.append((bullet["top"], bullet["x0"], text))

    # На двухколоночных макетах читаем строками: сверху вниз, слева направо.
    return [text for _, _, text in sorted(extracted)]


def rebuild(pdf_path: Path, menu_path: Path, output_path: Path, report_path: Path):
    menu = json.loads(menu_path.read_text(encoding="utf-8"))
    manual_review = []

    with pdfplumber.open(pdf_path) as pdf:
        for recipe in menu["recipes"]:
            menu_paths = recipe.get("menu_paths", [])
            if menu_paths:
                category, separator, _ = menu_paths[0].partition("/")
                if separator:
                    recipe["category"] = category

            page_number = int(recipe["page"])
            page = visible_page(pdf.pages[page_number - 1])
            text = page.extract_text(x_tolerance=2, y_tolerance=3) or ""
            words = page.extract_words(x_tolerance=2, y_tolerance=3)
            prep_words = [
                word
                for word in words
                if "ПРИГОТОВЛЕНИЕ" in word["text"].upper()
            ]

            if prep_words:
                prep_top = min(word["top"] for word in prep_words)
                ingredients = ingredient_blocks(page, prep_top)
                _, instructions = re.split(
                    r"ПРИГОТОВЛЕНИЕ", text, maxsplit=1, flags=re.IGNORECASE
                )
                recipe["ingredients"] = ingredients
                recipe["instructions"] = normalize_block(instructions)
            elif recipe["id"] not in MANUAL_OVERRIDES:
                manual_review.append(
                    {
                        "id": recipe["id"],
                        "page": page_number,
                        "name": recipe["name"],
                        "reason": "Нет заголовка ПРИГОТОВЛЕНИЕ",
                    }
                )

            for field in ("kcal", "protein", "fat", "carbs"):
                recipe.pop(field, None)
            recipe.pop("kbju_label", None)
            recipe.pop("kbju_variants", None)
            recipe.update(parse_kbju(text))
            recipe["portion"] = detect_portion(text)
            override = MANUAL_OVERRIDES.get(recipe["id"])
            if override:
                recipe.update(override)
                if "kbju_variants" in override:
                    for field in (
                        "kcal",
                        "protein",
                        "fat",
                        "carbs",
                        "kbju_label",
                    ):
                        recipe.pop(field, None)

    menu["total"] = len(menu["recipes"])
    output_path.write_text(
        json.dumps(menu, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8",
    )
    report_path.write_text(
        json.dumps(manual_review, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf", required=True, type=Path)
    parser.add_argument("--menu", default=Path("menu.json"), type=Path)
    parser.add_argument("--output", default=Path("menu.json"), type=Path)
    parser.add_argument(
        "--report", default=Path("menu_manual_review.json"), type=Path
    )
    args = parser.parse_args()
    rebuild(args.pdf, args.menu, args.output, args.report)


if __name__ == "__main__":
    main()
