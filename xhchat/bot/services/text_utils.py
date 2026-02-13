# -*- coding: utf-8 -*-
"""文本工具"""
import re

# emoji/全角数字 0-9 → ASCII 0-9
_EMOJI_DIGIT_MAP = str.maketrans({
    "０": "0", "１": "1", "２": "2", "３": "3", "４": "4",
    "５": "5", "６": "6", "７": "7", "８": "8", "９": "9",
    "⓪": "0", "①": "1", "②": "2", "③": "3", "④": "4",
    "⑤": "5", "⑥": "6", "⑦": "7", "⑧": "8", "⑨": "9",
    "⑴": "1", "⑵": "2", "⑶": "3", "⑷": "4", "⑸": "5",
    "⑹": "6", "⑺": "7", "⑻": "8", "⑼": "9",
})


def replace_emoji_digits(text: str) -> str:
    """将 emoji/全角数字 0-9 替换为 ASCII 0-9"""
    if not text:
        return text
    text = text.translate(_EMOJI_DIGIT_MAP)
    text = re.sub(r"(\d)\uFE0F?\u20E3", r"\1", text)  # 0️⃣ 1️⃣ 等 keycap emoji
    return text
