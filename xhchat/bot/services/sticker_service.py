# -*- coding: utf-8 -*-
"""贴纸服务：暖群和回复共用同一套贴纸，无类型区分"""
from bot.models.database import get_sticker_ids as _db_get_sticker_ids
from config.settings import WARM_STICKER_IDS


def get_sticker_ids() -> list[str]:
    """获取贴纸 file_id 列表。数据库为空时回退到 .env 的 WARM_STICKER_IDS"""
    ids = _db_get_sticker_ids()
    if ids:
        return ids
    return WARM_STICKER_IDS or []
