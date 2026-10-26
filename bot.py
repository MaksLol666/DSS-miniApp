import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramForbiddenError, TelegramNetworkError, TelegramServerError
from aiogram.filters import Command, CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo

from config import ADMIN_ID, BOT_TOKEN, MINIAPP_URL, REVIEWS_CHANNEL_ID
from database.db import SessionLocal
from database.models import User

log = logging.getLogger("dss.bot")
BOT_USERNAME: str | None = None
_bot: Bot | None = None


def _shop_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛍 Открыть магазин", web_app=WebAppInfo(url=MINIAPP_URL))]
    ])


async def send_message_safe(user_id: int, text: str) -> bool:
    if _bot is None:
        return False
    try:
        await _bot.send_message(user_id, text, disable_web_page_preview=True)
        return True
    except TelegramForbiddenError:
        async with SessionLocal() as session:
            record = await session.get(User, user_id)
            if record:
                record.is_unreachable = True
                await session.commit()
        return False
    except Exception:
        log.exception("Не удалось отправить сообщение Telegram user_id=%s", user_id)
        return False


async def notify_admin(text: str) -> bool:
    if _bot is None or not ADMIN_ID:
        log.warning("Не могу уведомить админа: Telegram-бот ещё не подключён")
        return False
    try:
        await _bot.send_message(ADMIN_ID, text, reply_markup=_shop_keyboard(), disable_web_page_preview=True)
        return True
    except Exception:
        log.exception("Не удалось уведомить администратора")
        return False


async def publish_review(text: str) -> bool:
    if _bot is None or not REVIEWS_CHANNEL_ID:
        return False
    try:
        try:
            target: int | str = int(REVIEWS_CHANNEL_ID)
        except ValueError:
            target = REVIEWS_CHANNEL_ID
        await _bot.send_message(target, text, disable_web_page_preview=True)
        return True
    except Exception:
        log.exception("Не удалось опубликовать отзыв в канал")
        return False


def _register_handlers(dp: Dispatcher) -> None:
    @dp.message(CommandStart())
    async def start(message: Message) -> None:
        if not message.from_user:
            return
        user_id = message.from_user.id
        username = message.from_user.username or message.from_user.full_name
        parts = (message.text or "").split(maxsplit=1)
        referrer_id = None
        if len(parts) > 1 and parts[1].startswith("ref_"):
            try:
                candidate = int(parts[1][4:])
                if candidate != user_id:
                    referrer_id = candidate
            except ValueError:
                pass

        new_ref = False
        async with SessionLocal() as session:
            record = await session.get(User, user_id)
            if record is None:
                referrer = await session.get(User, referrer_id) if referrer_id else None
                record = User(id=user_id, username=username, referrer_id=referrer.id if referrer else None)
                session.add(record)
                new_ref = bool(referrer)
            else:
                record.username = username
                record.is_unreachable = False
                if record.referrer_id is None and referrer_id and not record.first_purchase_done:
                    referrer = await session.get(User, referrer_id)
                    if referrer:
                        record.referrer_id = referrer.id
                        new_ref = True
            await session.commit()

        if new_ref and referrer_id:
            await send_message_safe(
                referrer_id,
                "🔔 По вашей ссылке пришёл новый пользователь. Бонус появится после его первой подтверждённой покупки.",
            )
        await message.answer(
            "💨 DEAD SMOKE STORE\n\nКаталог, корзина, промокоды и заказы — в магазине ниже.",
            reply_markup=_shop_keyboard(),
        )

    @dp.message(Command("admin"))
    async def admin(message: Message) -> None:
        if not message.from_user or message.from_user.id != ADMIN_ID:
            await message.answer("Эта команда доступна только администратору.")
            return
        await message.answer("🛠 Админ-панель доступна в Mini App.", reply_markup=_shop_keyboard())

    @dp.message(Command("help"))
    async def help_command(message: Message) -> None:
        await message.answer(
            "Команды:\n/start — открыть магазин\n/admin — открыть админ-панель\n/help — помощь"
        )


async def start_bot() -> None:
    global _bot, BOT_USERNAME
    if not BOT_TOKEN:
        log.warning("BOT_TOKEN не задан; polling отключён, API продолжает работу")
        return

    while True:
        client = Bot(token=BOT_TOKEN)
        try:
            me = await client.get_me()
            BOT_USERNAME = me.username
            _bot = client
            dispatcher = Dispatcher()
            _register_handlers(dispatcher)
            log.info("Bot started: @%s", BOT_USERNAME)
            # Do not discard pending Telegram updates on a normal restart.
            await dispatcher.start_polling(client)
        except asyncio.CancelledError:
            raise
        except (TelegramNetworkError, TelegramServerError) as exc:
            log.warning("Telegram временно недоступен: %s; повтор через 5 секунд", exc)
        except Exception:
            log.exception("Polling завершился с ошибкой")
        finally:
            if _bot is client:
                _bot = None
            await client.session.close()
        await asyncio.sleep(5)
