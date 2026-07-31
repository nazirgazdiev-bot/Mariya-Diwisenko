"""
Клиент для Продамус (Payform): подпись/проверка запросов и разбор вебхука.

Алгоритм подписи — стандартный для Продамус (HMAC-SHA256 по JSON с
отсортированными по алфавиту ключами). Логика sign/verify/parse
на основе официально рекомендуемой Продамусом библиотеки prodamuspy
(https://github.com/dnagikh/python-prodamus), адаптирована под этот проект.

Документация: https://help.prodamus.ru/payform/integracii/rest-api/instrukcii-dlya-samostoyatelnaya-integracii-servisov
"""

import collections
import hashlib
import hmac
import json
import re
from copy import deepcopy
from urllib.parse import parse_qsl, quote


class ProdamusClient:
    def __init__(self, secret: str, shop_url: str):
        self.secret = secret
        self.shop_url = shop_url.rstrip("/") + "/"

    # ---------- Подпись ----------

    def sign(self, data: dict) -> str:
        obj_json = json.dumps(
            data, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
        # Prodamus подписывает JSON в формате PHP json_encode, где прямые
        # слэши экранированы. Это важно для реальных webhook: например,
        # payment_type содержит "Visa/MasterCard/МИР".
        obj_json = obj_json.replace("/", r"\/")
        return hmac.new(
            self.secret.encode("utf-8"),
            msg=obj_json.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).hexdigest()

    def verify(self, data: dict, sign: str) -> bool:
        if not sign:
            return False
        expected = self.sign(data)
        return hmac.compare_digest(expected, sign.lower())

    # ---------- Разбор входящего вебхука (form-urlencoded, ключи вида a[b][c]) ----------

    def parse(self, body: str) -> dict:
        payload = dict(
            parse_qsl(body, keep_blank_values=True, strict_parsing=False, errors="strict")
        )
        return self._php2dict(payload)

    def _php2dict(self, array: dict) -> dict:
        dct: dict = {}
        for k, v in array.items():
            m = re.fullmatch(r"([^\[]+)(\[.+\])", k)
            if m:
                idx = [m.group(1)] + re.findall(r"\[([^\]]*)\]", m.group(2))
                subdct = self._dict_build(idx, v)
            else:
                subdct = {k: v}
            dct = self._dict_merge(dct, subdct)
        return dct

    def _dict_build(self, idx: list, value):
        value = deepcopy(value)
        if len(idx):
            i = idx.pop(0)
            if re.fullmatch(r"[0-9]+", i):
                return [{} for _ in range(int(i))] + [self._dict_build(idx, value)]
            return {i: self._dict_build(idx, value)}
        return value

    def _dict_merge(self, dct: dict, merge_dct: dict) -> dict:
        dct = deepcopy(dct)
        for k, v in merge_dct.items():
            if not dct.get(k):
                dct[k] = deepcopy(v)
            elif isinstance(dct[k], dict) and isinstance(v, collections.abc.Mapping):
                dct[k] = self._dict_merge(dct[k], v)
            elif isinstance(v, list):
                for li, lv in enumerate(v):
                    if len(dct[k]) <= li:
                        dct[k].append(lv)
                    else:
                        dct[k][li] = self._dict_merge(dct[k][li], lv)
            else:
                dct[k] = v
        return dct

    # ---------- Построение ссылки на оплату ----------

    @staticmethod
    def _stringify(value):
        """Рекурсивно приводит все листовые значения к строкам —
        того требует алгоритм подписи Продамуса."""
        if isinstance(value, dict):
            return {k: ProdamusClient._stringify(v) for k, v in value.items()}
        if isinstance(value, list):
            return [ProdamusClient._stringify(v) for v in value]
        return str(value)

    @staticmethod
    def _flatten(value, prefix=""):
        """Превращает вложенный dict/list в плоский список (key, value)
        в PHP bracket-нотации: products[0][name]=... и т.д."""
        pairs = []
        if isinstance(value, dict):
            for k, v in value.items():
                key = f"{prefix}[{k}]" if prefix else str(k)
                pairs.extend(ProdamusClient._flatten(v, key))
        elif isinstance(value, list):
            for i, v in enumerate(value):
                key = f"{prefix}[{i}]"
                pairs.extend(ProdamusClient._flatten(v, key))
        else:
            pairs.append((prefix, value))
        return pairs

    def build_payment_link(self, data: dict) -> str:
        """data — параметры запроса (do, order_id, products и т.д.),
        без подписи. Возвращает готовую ссылку на оплату."""
        str_data = self._stringify(data)
        signature = self.sign(str_data)
        full = dict(str_data)
        full["signature"] = signature
        pairs = self._flatten(full)
        query = "&".join(f"{quote(k, safe='')}={quote(str(v), safe='')}" for k, v in pairs)
        return f"{self.shop_url}?{query}"
