"""Telegram 机器人 - 群消息监听、头像下载、性别检测"""
import asyncio
import logging
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes

from config import BOT_TOKEN, ADMIN_IDS, STORAGE_PATH, LOG_PATH
from opencv_gender import detect_gender

# 头像缓存时长（天）
AVATAR_CACHE_DAYS = 1

# 日志格式
LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
logging.basicConfig(
    format=LOG_FORMAT,
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# 异常日志写入目录下的文件（按大小轮转，最多保留 3 个备份）
log_file = LOG_PATH / "error.log"
file_handler = RotatingFileHandler(
    log_file,
    maxBytes=5 * 1024 * 1024,  # 5MB
    backupCount=3,
    encoding="utf-8",
)
file_handler.setLevel(logging.ERROR)
file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
logging.getLogger().addHandler(file_handler)


def get_timestamp() -> str:
    """获取当前时间戳字符串"""
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _get_user_avatar_path(user_id: int) -> Path | None:
    """查找用户已有的头像文件（格式：*_user_id.jpg）"""
    suffix = f"_{user_id}.jpg"
    for f in STORAGE_PATH.glob(f"*{suffix}"):
        if f.name.startswith("temp_"):
            continue
        return f
    return None


def _is_avatar_cached(avatar_path: Path) -> bool:
    """检查头像是否在缓存有效期内（1天）"""
    mtime = datetime.fromtimestamp(avatar_path.stat().st_mtime)
    return datetime.now() - mtime < timedelta(days=AVATAR_CACHE_DAYS)


def _remove_old_avatars(user_id: int) -> None:
    """删除该用户的所有旧头像"""
    suffix = f"_{user_id}.jpg"
    for f in STORAGE_PATH.glob(f"*{suffix}"):
        if f.name.startswith("temp_"):
            continue
        try:
            f.unlink()
            logger.debug(f"已删除旧头像: {f.name}")
        except OSError as e:
            logger.warning(f"删除旧头像失败 {f}: {e}")


async def process_user_avatar(
    user_id: int,
    username: str | None,
    application: Application,
) -> Path | None:
    """
    获取用户头像、下载、检测性别、保存
    每个用户只保留一张头像，缓存 1 天，过期后重新获取
    返回最终保存的文件路径，失败返回 None
    """
    bot = application.bot
    try:
        # 检查缓存：已有头像且在有效期内则跳过
        existing = _get_user_avatar_path(user_id)
        if existing and _is_avatar_cached(existing):
            logger.debug(f"用户 {user_id} 头像已缓存，跳过")
            return existing

        photos = await bot.get_user_profile_photos(user_id, limit=1)
        if not photos or not photos.photos:
            logger.info(f"用户 {user_id} 没有头像")
            return None

        # 获取最大尺寸的头像
        photo_sizes = photos.photos[0]
        largest = max(photo_sizes, key=lambda p: p.width * p.height)
        file = await bot.get_file(largest.file_id)

        # 临时保存
        timestamp = get_timestamp()
        temp_path = STORAGE_PATH / f"temp_{user_id}_{timestamp}.jpg"
        await file.download_to_drive(custom_path=str(temp_path))

        # 调用 OpenCV DNN 本地检测性别
        result = detect_gender(temp_path)
        prefix_map = {"male": "男性", "female": "女性", "other": "其他", "failure": "失败"}
        prefix = prefix_map.get(result, "失败")

        # 每个用户固定一个文件名，覆盖旧头像
        final_name = f"{prefix}_{user_id}.jpg"
        final_path = STORAGE_PATH / final_name
        _remove_old_avatars(user_id)
        temp_path.rename(final_path)

        logger.info(f"已保存: {final_path} (用户 {user_id}, 性别: {prefix})")
        return final_path

    except Exception as e:
        logger.exception(f"处理用户 {user_id} 头像失败: {e}")
        return None


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理群消息：当用户发言时获取其头像并检测"""
    if not update.message or not update.effective_user:
        return

    user = update.effective_user
    user_id = user.id

    # 在后台异步处理，不阻塞回复
    async def _process():
        path = await process_user_avatar(
            user_id,
            user.username,
            context.application,
        )
        if path and update.effective_chat:
            # 可选：在群里回复结果（仅管理员可触发回复，避免刷屏）
            # 这里改为静默处理，只记录日志。如需回复可取消注释：
            # if user_id in ADMIN_IDS:
            #     await context.bot.send_message(
            #         update.effective_chat.id,
            #         f"已处理 @{user.username or user_id} 的头像 -> {path.name}",
            #     )
            pass

    asyncio.create_task(_process())


async def post_init(application: Application) -> None:
    """启动成功后向管理员私聊发送你好"""
    for admin_id in ADMIN_IDS:
        try:
            await application.bot.send_message(chat_id=admin_id, text="你好")
        except Exception as e:
            logger.warning(f"向管理员 {admin_id} 发送启动消息失败: {e}")


def main() -> None:
    if not BOT_TOKEN:
        raise ValueError("请在 .env 中设置 BOT_TOKEN")

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # 仅监听群组中的文本消息
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.ChatType.GROUPS,
            handle_message,
        )
    )

    logger.info("机器人已启动，正在监听消息...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
