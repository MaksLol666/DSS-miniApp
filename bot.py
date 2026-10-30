import asyncio
import logging
from datetime import datetime

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramForbiddenError, TelegramNetworkError, TelegramServerError
from aiogram.filters import Command, CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo, CallbackQuery

from sqlalchemy import delete, func, select

from config import ADMIN_ID, BOT_TOKEN, MINIAPP_URL, REVIEWS_CHANNEL_ID
from database.db import SessionLocal
from database.models import User, Product, Category, Order, OrderItem, Promocode, CartPromo

log = logging.getLogger("dss.bot")
BOT_USERNAME: str | None = None
_bot: Bot | None = None
ADMIN_FLOW: dict[int, dict] = {}


def _shop_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛍 Открыть магазин", web_app=WebAppInfo(url=MINIAPP_URL))]
    ])


def _is_admin(message_or_user) -> bool:
    user = getattr(message_or_user, "from_user", message_or_user)
    return bool(ADMIN_ID and user and user.id == ADMIN_ID)


def _admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📦 Товары", callback_data="adm:products"), InlineKeyboardButton(text="➕ Добавить товар", callback_data="adm:add")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="adm:stats"), InlineKeyboardButton(text="🎟 Промокоды", callback_data="adm:promos")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="adm:broadcast"), InlineKeyboardButton(text="🛍 Открыть Mini App", web_app=WebAppInfo(url=MINIAPP_URL))],
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


async def notify_admin(text: str, order_id: int | None = None) -> bool:
    if _bot is None or not ADMIN_ID:
        log.warning("Не могу уведомить админа: ADMIN_ID или бот не настроен")
        return False
    keyboard = None
    if order_id:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Принять", callback_data=f"order:approve:{order_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"order:reject:{order_id}"),
        ], [InlineKeyboardButton(text="🛍 Открыть админку", web_app=WebAppInfo(url=MINIAPP_URL))]])
    try:
        await _bot.send_message(ADMIN_ID, text, reply_markup=keyboard, disable_web_page_preview=True)
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


async def _stats_text() -> str:
    async with SessionLocal() as session:
        users = (await session.execute(select(func.count(User.id)))).scalar() or 0
        orders = (await session.execute(select(func.count(Order.id)).where(Order.status == "approved"))).scalar() or 0
        pending = (await session.execute(select(func.count(Order.id)).where(Order.status == "pending"))).scalar() or 0
        revenue = (await session.execute(select(func.coalesce(func.sum(Order.total), 0)).where(Order.status == "approved"))).scalar() or 0
        products = (await session.execute(select(func.count(Product.id)).where(Product.in_stock.is_(True), Product.stock_qty > 0))).scalar() or 0
        top_rows = (await session.execute(select(OrderItem.product_id, func.count(OrderItem.id).label("n")).group_by(OrderItem.product_id).order_by(func.count(OrderItem.id).desc()).limit(5))).all()
        top = []
        for product_id, count in top_rows:
            p = await session.get(Product, product_id)
            if p:
                top.append(f"• {p.brand} — {p.flavor}: {count} шт.")
    return ("📊 СТАТИСТИКА DSS\n\n"
        f"👥 Пользователей: {users}\n📦 Подтверждено заказов: {orders}\n⏳ Ожидают решения: {pending}\n"
        f"💰 Выручка: {float(revenue):.0f} ₽\n🛒 Активных позиций каталога: {products}\n\n"
        "🏆 ТОП-ТОВАРЫ\n" + ("\n".join(top) if top else "Пока нет продаж."))


async def _show_products(message: Message):
    async with SessionLocal() as session:
        rows = (await session.execute(select(Product).order_by(Product.category_id, Product.number).limit(30))).scalars().all()
        categories = {c.id: c.name for c in (await session.execute(select(Category))).scalars().all()}
    if not rows:
        await message.answer("Товаров пока нет.", reply_markup=_admin_menu())
        return
    lines = ["📦 ТОВАРЫ (первые 30):"]
    buttons = []
    for p in rows:
        lines.append(f"#{p.id} · {categories.get(p.category_id, 'Категория')} · {p.brand} — {p.flavor} · {p.price:.0f} ₽ · остаток {p.stock_qty} шт.")
        buttons.append([
            InlineKeyboardButton(text=f"✏️ Изменить #{p.id}", callback_data=f"adm:edit:{p.id}"),
            InlineKeyboardButton(text=f"🗑 Удалить #{p.id}", callback_data=f"adm:delete:{p.id}")
        ])
    buttons.append([InlineKeyboardButton(text="➕ Добавить товар", callback_data="adm:add")])
    buttons.append([InlineKeyboardButton(text="⬅️ Меню", callback_data="adm:menu")])
    await message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


async def _create_product_from_flow(message: Message, flow: dict):
    async with SessionLocal() as session:
        category = await session.get(Category, int(flow["category_id"]))
        if not category:
            await message.answer("Категория не найдена. Начни заново: /admin")
            return
        n = (await session.execute(select(func.max(Product.number)).where(Product.category_id == category.id))).scalar() or 0
        p = Product(category_id=category.id, number=n + 1, brand=flow["brand"], flavor=flow["flavor"],
                    price=float(flow["price"]), stock_qty=int(flow["stock_qty"]), in_stock=int(flow["stock_qty"]) > 0)
        session.add(p)
        await session.commit()
        await session.refresh(p)
    await message.answer(f"✅ Товар добавлен: #{p.id}\n{p.brand} — {p.flavor}\nЦена: {p.price:.0f} ₽\nОстаток: {p.stock_qty} шт.", reply_markup=_admin_menu())


async def _broadcast(text: str):
    async with SessionLocal() as session:
        ids = list((await session.execute(select(User.id).where(User.is_unreachable.is_(False)))).scalars().all())
    sent = failed = 0
    for uid in ids:
        if await send_message_safe(uid, text):
            sent += 1
        else:
            failed += 1
        await asyncio.sleep(0.07)
    log.info("Broadcast finished sent=%s failed=%s total=%s", sent, failed, len(ids))
    if _bot and ADMIN_ID:
        await _bot.send_message(ADMIN_ID, f"✅ Рассылка завершена. Доставлено: {sent}, не доставлено: {failed}.")


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
            await send_message_safe(referrer_id, "🔔 По вашей ссылке пришёл новый пользователь. Бонус появится после его первой подтверждённой покупки.")
        await message.answer("💨 DEAD SMOKE STORE\n\nКаталог, корзина, промокоды и заказы — в магазине ниже.", reply_markup=_shop_keyboard())

    @dp.message(Command("myid"))
    async def myid(message: Message):
        if message.from_user:
            await message.answer(f"Твой Telegram ID: `{message.from_user.id}`", parse_mode="Markdown")

    @dp.message(Command("admin"))
    async def admin(message: Message):
        if not _is_admin(message):
            await message.answer("Эта команда доступна только администратору.")
            return
        ADMIN_FLOW.pop(message.from_user.id, None)
        await message.answer("👑 DEAD SMOKE STORE — управление магазином\n\nВыбери нужный раздел:", reply_markup=_admin_menu())

    @dp.message(Command("stats"))
    async def stats(message: Message):
        if not _is_admin(message):
            await message.answer("Команда доступна только администратору.")
            return
        await message.answer(await _stats_text(), reply_markup=_admin_menu())

    @dp.message(Command("broadcast"))
    async def broadcast_start(message: Message):
        if not _is_admin(message):
            await message.answer("Команда доступна только администратору.")
            return
        ADMIN_FLOW[message.from_user.id] = {"step": "broadcast"}
        await message.answer("📢 Отправь следующим сообщением текст рассылки.\n/cancel — отмена.")

    @dp.message(Command("newpromo"))
    async def newpromo(message: Message):
        if not _is_admin(message):
            await message.answer("Команда доступна только администратору.")
            return
        args = (message.text or "").split()
        if len(args) < 4:
            await message.answer("Формат:\n/newpromo КОД fixed|percent СУММА owner_id max_uses\n\nПример: /newpromo DSS10 percent 10 0 100\nКод можно заменить на AUTO для генерации.")
            return
        _, code, kind, amount_s, *tail = args
        try:
            amount = float(amount_s.replace(",", "."))
            owner_id = int(tail[0]) if tail else 0
            max_uses = int(tail[1]) if len(tail) > 1 else 1
            if kind not in ("fixed", "percent") or amount <= 0 or (kind == "percent" and amount > 100) or owner_id < 0 or max_uses < 1:
                raise ValueError()
            async with SessionLocal() as session:
                if code.upper() == "AUTO":
                    from api import _create_promo
                    result_code = await _create_promo(session, owner_id, kind, amount, max_uses, "admin")
                else:
                    result_code = code.upper()
                    if len(result_code) > 16 or await session.get(Promocode, result_code):
                        await message.answer("Такой код уже существует или превышает 16 символов.")
                        return
                    session.add(Promocode(code=result_code, owner_id=owner_id, kind=kind, amount=amount, used=False, created_by="admin", max_uses=max_uses, used_count=0))
                await session.commit()
            await message.answer(f"✅ Промокод создан: {result_code}\nСкидка: {'−'+str(amount)+'%' if kind == 'percent' else f'−{amount:.0f} ₽'}\nВладелец: {owner_id} · активаций: {max_uses}")
        except (ValueError, TypeError):
            await message.answer("Неверные параметры. Пример: /newpromo DSS10 percent 10 0 100")

    @dp.message(Command("allpromos"))
    async def allpromos(message: Message):
        if not _is_admin(message):
            await message.answer("Команда доступна только администратору.")
            return
        async with SessionLocal() as session:
            rows = (await session.execute(select(Promocode).where(Promocode.used.is_(False), Promocode.used_count < Promocode.max_uses).order_by(Promocode.code).limit(100))).scalars().all()
        if not rows:
            await message.answer("Активных промокодов нет.")
            return
        lines = ["🎟 АКТИВНЫЕ ПРОМОКОДЫ:"]
        for p in rows:
            discount = f"−{p.amount:g}%" if p.kind == "percent" else f"−{p.amount:.0f} ₽"
            lines.append(f"• {p.code} — {discount}; владелец {p.owner_id}; {p.used_count}/{p.max_uses}")
        await message.answer("\n".join(lines))

    @dp.message(Command("user"))
    async def user_command(message: Message):
        if not _is_admin(message):
            await message.answer("Команда доступна только администратору.")
            return
        args = (message.text or "").split()
        if len(args) < 2:
            await message.answer("Формат:\n/user ID — статистика\n/user ban ID\n/user unban ID\n/user reset ID")
            return
        try:
            if args[1] in ("ban", "unban", "reset"):
                action, uid = args[1], int(args[2])
            else:
                action, uid = "info", int(args[1])
        except (ValueError, IndexError):
            await message.answer("Укажи корректный Telegram ID.")
            return
        if uid == ADMIN_ID and action in ("ban", "reset"):
            await message.answer("Нельзя заблокировать или сбросить самого администратора.")
            return
        async with SessionLocal() as session:
            u = await session.get(User, uid)
            if not u:
                await message.answer("Пользователь не найден в базе.")
                return
            if action == "ban":
                u.is_banned = True
            elif action == "unban":
                u.is_banned = False
            elif action == "reset":
                u.balance = 0
                u.referrals_count = 0
                u.next_bonus_at = 10
                u.first_purchase_done = False
                await session.execute(delete(Promocode).where(Promocode.owner_id == uid))
                await session.execute(delete(CartPromo).where(CartPromo.user_id == uid))
            await session.commit()
            count = (await session.execute(select(func.count(Order.id)).where(Order.user_id == uid, Order.status == "approved"))).scalar() or 0
            text = f"👤 Пользователь: {u.username or 'без username'}\nID: {uid}\nПотрачено: {u.balance:.0f} ₽\nПокупок: {count}\nРефералов: {u.referrals_count}\nСтатус: {'🔴 Заблокирован' if u.is_banned else '🟢 Активен'}"
        if action == "ban":
            await send_message_safe(uid, "❌ Доступ к магазину заблокирован администратором.")
        elif action == "unban":
            await send_message_safe(uid, "✅ Доступ к магазину восстановлен.")
        await message.answer({"ban": "🔴 Пользователь заблокирован.", "unban": "🟢 Пользователь разблокирован.", "reset": "♻️ Статистика пользователя сброшена. История заказов сохранена.", "info": text}[action])

    @dp.message(Command("wipeusers"))
    async def wipeusers(message: Message):
        if not _is_admin(message):
            await message.answer("Команда доступна только администратору.")
            return
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="⚠️ Да, сбросить статистику", callback_data="adm:wipe:yes"),
            InlineKeyboardButton(text="Отмена", callback_data="adm:menu")
        ]])
        await message.answer("⚠️ Сбросить статистику ВСЕХ пользователей, кроме администратора?\n\nБудут обнулены траты, рефералы и промокоды. История заказов и аккаунты сохранятся.", reply_markup=kb)

    @dp.message(Command("cancel"))
    async def cancel(message: Message):
        if not _is_admin(message):
            return
        ADMIN_FLOW.pop(message.from_user.id, None)
        await message.answer("Текущее действие отменено.", reply_markup=_admin_menu())

    @dp.message(Command("help"))
    async def help_command(message: Message):
        await message.answer("Команды:\n/start — открыть магазин\n/admin — меню управления\n/stats — статистика\n/broadcast — рассылка\n/newpromo — создать промокод\n/allpromos — активные промокоды\n/user — управление пользователем\n/wipeusers — сброс статистики пользователей\n/cancel — отмена действия")

    @dp.callback_query(F.data.startswith("adm:"))
    async def admin_callbacks(callback: CallbackQuery):
        if not _is_admin(callback):
            await callback.answer("Нет доступа", show_alert=True)
            return
        data = callback.data.split(":")
        action = data[1]
        if action == "menu":
            await callback.message.edit_text("👑 Управление Dead Smoke Store", reply_markup=_admin_menu())
        elif action == "products":
            await _show_products(callback.message)
        elif action == "stats":
            await callback.message.answer(await _stats_text())
        elif action == "promos":
            await callback.message.answer("🎟 Команды промокодов:\n/newpromo КОД fixed|percent СУММА owner_id max_uses\n/allpromos — список активных")
        elif action == "broadcast":
            ADMIN_FLOW[callback.from_user.id] = {"step": "broadcast"}
            await callback.message.answer("📢 Отправь следующим сообщением текст рассылки.\n/cancel — отмена.")
        elif action == "add":
            async with SessionLocal() as session:
                cats = (await session.execute(select(Category).order_by(Category.id))).scalars().all()
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=c.name, callback_data=f"adm:cat:{c.id}")] for c in cats])
            await callback.message.answer("Выбери категорию нового товара:", reply_markup=kb)
        elif action == "cat":
            ADMIN_FLOW[callback.from_user.id] = {"step": "brand", "category_id": int(data[2])}
            await callback.message.answer("Введи бренд товара:")
        elif action == "edit":
            pid = int(data[2])
            ADMIN_FLOW[callback.from_user.id] = {"step": "edit_product", "product_id": pid}
            await callback.message.answer(f"Введи данные товара #{pid} одной строкой:\nБренд | вариант/вкус | цена | количество")
        elif action == "delete":
            pid = int(data[2])
            async with SessionLocal() as session:
                p = await session.get(Product, pid)
                if p:
                    used = (await session.execute(select(OrderItem.id).where(OrderItem.product_id == pid).limit(1))).first()
                    if used:
                        p.stock_qty = 0
                        p.in_stock = False
                    else:
                        await session.execute(delete(CartItem).where(CartItem.product_id == pid))
                        await session.delete(p)
                    await session.commit()
            await callback.message.answer(f"🗑 Товар #{pid} удалён или скрыт из каталога.")
        elif action == "wipe" and data[2] == "yes":
            async with SessionLocal() as session:
                users = (await session.execute(select(User).where(User.id != ADMIN_ID))).scalars().all()
                for u in users:
                    u.balance = 0
                    u.referrals_count = 0
                    u.next_bonus_at = 10
                    u.first_purchase_done = False
                await session.execute(delete(Promocode).where(Promocode.owner_id != ADMIN_ID))
                await session.execute(delete(CartPromo))
                await session.commit()
            await callback.message.answer("♻️ Статистика всех пользователей сброшена. Аккаунты и история заказов сохранены.")
        await callback.answer()

    @dp.callback_query(F.data.startswith("order:"))
    async def order_callbacks(callback: CallbackQuery):
        if not _is_admin(callback):
            await callback.answer("Нет доступа", show_alert=True)
            return
        try:
            _, decision, order_id_s = callback.data.split(":")
            order_id = int(order_id_s)
            from api import _process_order
            result = await _process_order(order_id, decision == "approve")
            await callback.message.edit_reply_markup(reply_markup=None)
            await callback.answer("Заказ принят" if decision == "approve" else "Заказ отклонён")
            await callback.message.answer(f"{'✅ Заказ' if decision == 'approve' else '❌ Заказ'} №{order_id} обработан.")
        except Exception as exc:
            log.exception("Order callback failed")
            await callback.answer(str(getattr(exc, "detail", "Не удалось обработать заказ.")), show_alert=True)

    @dp.message()
    async def admin_flow_message(message: Message):
        if not message.from_user or not _is_admin(message):
            return
        flow = ADMIN_FLOW.get(message.from_user.id)
        if not flow:
            return
        text = (message.text or "").strip()
        step = flow.get("step")
        if step == "broadcast":
            if not text:
                await message.answer("Нужен текст. Попробуй ещё раз или /cancel.")
                return
            ADMIN_FLOW.pop(message.from_user.id, None)
            await message.answer("⏳ Рассылка запущена. По завершении пришлю отчёт.")
            asyncio.create_task(_broadcast(text))
        elif step == "brand":
            flow["brand"] = text
            flow["step"] = "flavor"
            await message.answer("Введи вкус или вариант товара:")
        elif step == "flavor":
            flow["flavor"] = text
            flow["step"] = "price"
            await message.answer("Введи цену в рублях (число):")
        elif step == "price":
            try:
                price = float(text.replace(",", "."))
                if price <= 0: raise ValueError()
            except ValueError:
                await message.answer("Цена должна быть числом больше нуля. Повтори или /cancel.")
                return
            flow["price"] = price
            flow["step"] = "stock_qty"
            await message.answer("Сколько штук есть на складе? Введи целое число, можно 0:")
        elif step == "stock_qty":
            try:
                qty = int(text)
                if qty < 0 or qty > 1000000: raise ValueError()
            except ValueError:
                await message.answer("Количество должно быть целым числом от 0 до 1 000 000.")
                return
            flow["stock_qty"] = qty
            ADMIN_FLOW.pop(message.from_user.id, None)
            await _create_product_from_flow(message, flow)
        elif step == "edit_product":
            parts = [x.strip() for x in text.split("|")]
            if len(parts) != 4:
                await message.answer("Формат: Бренд | вкус | цена | количество. Например: Brand | Mint | 500 | 12")
                return
            try:
                price, qty = float(parts[2].replace(",", ".")), int(parts[3])
                if price <= 0 or qty < 0: raise ValueError()
            except ValueError:
                await message.answer("Цена должна быть больше 0, количество — целым числом от 0.")
                return
            async with SessionLocal() as session:
                p = await session.get(Product, flow["product_id"])
                if not p:
                    await message.answer("Товар не найден.")
                else:
                    p.brand, p.flavor, p.price = parts[0], parts[1], price
                    p.stock_qty, p.in_stock = qty, qty > 0
                    await session.commit()
                    await message.answer(f"✅ Товар #{p.id} обновлён. Остаток: {qty} шт.", reply_markup=_admin_menu())
            ADMIN_FLOW.pop(message.from_user.id, None)


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
