import asyncio
import logging

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message, CallbackQuery, WebAppInfo,
    InlineKeyboardMarkup, InlineKeyboardButton
)

from config import BOT_TOKEN, MINIAPP_URL
from database.db import SessionLocal
from database.models import User, Order, OrderItem, Product, Promocode
from sqlalchemy import select

log = logging.getLogger("bot")
BOT_USERNAME: str | None = None


def build_router(dp: Dispatcher):
    @dp.message(CommandStart())
    async def cmd_start(message: Message):
        tg_id = message.from_user.id
        username = message.from_user.username or message.from_user.full_name

        args = message.text.split(maxsplit=1)
        referrer_id = None
        if len(args) > 1 and args[1].startswith("ref_"):
            try:
                referrer_id = int(args[1][4:])
                if referrer_id == tg_id:
                    referrer_id = None
            except ValueError:
                pass

        async with SessionLocal() as s:
            user = await s.get(User, tg_id)
            if not user:
                user = User(id=tg_id, username=username, referrer_id=referrer_id)
                s.add(user)
                await s.commit()
                if referrer_id:
                    try:
                        await message.bot.send_message(
                            referrer_id,
                            f"🔔 По вашей ссылке зашёл новый пользователь: @{username or tg_id}"
                        )
                    except Exception:
                        pass
            else:
                if user.is_unreachable:
                    user.is_unreachable = False
                    await s.commit()

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🛍 Открыть магазин", web_app=WebAppInfo(url=MINIAPP_URL))]
        ])
        await message.answer(
            "💨 <b>Dead Smoke Store</b>\n\n"
            "Нажми кнопку ниже, чтобы открыть магазин:",
            reply_markup=kb,
            parse_mode="HTML",
        )

    @dp.callback_query(F.data.startswith("order:"))
    async def process_order(cb: CallbackQuery):
        from config import ADMIN_ID, REFERRAL_MILESTONE, REFERRAL_MILESTONE_DISCOUNT
        from utils.promo import gen_promo
        from api_services.bot_notifier import notify_order_approved, notify_order_rejected
        from keyboards.inline import admin_order_kb  # используем старое

        if cb.from_user.id != ADMIN_ID:
            await cb.answer("Не для тебя 🙂", show_alert=True)
            return

        _, action, order_id = cb.data.split(":")
        order_id = int(order_id)

        async with SessionLocal() as s:
            order = await s.get(Order, order_id)
            if not order or order.status != "pending":
                await cb.answer("Заказ уже обработан.", show_alert=True)
                return

            items = (await s.execute(
                select(OrderItem, Product).join(Product, OrderItem.product_id == Product.id)
                .where(OrderItem.order_id == order_id)
            )).all()
            buyer = await s.get(User, order.user_id)

            if action == "approve":
                order.status = "approved"
                for _, p in items:
                    await s.delete(p)
                if buyer:
                    buyer.balance = (buyer.balance or 0) + order.total
                    if buyer.referrer_id and not buyer.first_purchase_done:
                        bonus = round(order.total * 0.15, 2)
                        code = gen_promo()
                        while await s.get(Promocode, code):
                            code = gen_promo()
                        s.add(Promocode(code=code, owner_id=buyer.referrer_id,
                                        kind="fixed", amount=bonus, created_by="ref"))
                        ref_user = await s.get(User, buyer.referrer_id)
                        if ref_user:
                            ref_user.referrals_count += 1
                            if ref_user.referrals_count >= ref_user.next_bonus_at:
                                mc = gen_promo()
                                while await s.get(Promocode, mc):
                                    mc = gen_promo()
                                s.add(Promocode(code=mc, owner_id=buyer.referrer_id,
                                                kind="percent", amount=REFERRAL_MILESTONE_DISCOUNT,
                                                created_by="ref"))
                                ref_user.next_bonus_at += REFERRAL_MILESTONE
                                try:
                                    await cb.bot.send_message(
                                        buyer.referrer_id,
                                        f"🎉 Ты пригласил {ref_user.referrals_count} купивших друзей!\n"
                                        f"Промокод на −{int(REFERRAL_MILESTONE_DISCOUNT)}%:\n<code>{mc}</code>",
                                        parse_mode="HTML"
                                    )
                                except Exception:
                                    pass
                        try:
                            await cb.bot.send_message(
                                buyer.referrer_id,
                                f"🎁 Твой реферал совершил первую покупку!\n"
                                f"Промокод на <b>{int(bonus)}₽</b>:\n<code>{code}</code>",
                                parse_mode="HTML"
                            )
                        except Exception:
                            pass
                    buyer.first_purchase_done = True

                if order.promo_codes:
                    for pc in order.promo_codes.split(","):
                        pr = await s.get(Promocode, pc)
                        if pr:
                            pr.used_count += 1
                            if pr.used_count >= pr.max_uses:
                                pr.used = True

                if buyer:
                    await notify_order_approved(buyer.id, order_id)
                await cb.message.edit_text(
                    cb.message.html_text + "\n\n✅ <b>Подтверждён</b>",
                    parse_mode="HTML"
                )
            else:
                order.status = "rejected"
                for _, p in items:
                    p.in_stock = True
                if buyer:
                    await notify_order_rejected(buyer.id, order_id)
                await cb.message.edit_text(
                    cb.message.html_text + "\n\n❌ <b>Отклонён</b>",
                    parse_mode="HTML"
                )

            await s.commit()


async def start_bot():
    """Запускает polling бота. Вызывается из api.py через asyncio.create_task."""
    global BOT_USERNAME

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()

    # получаем username бота (для реф-ссылок)
    me = await bot.get_me()
    BOT_USERNAME = me.username
    log.info(f"Bot started: @{BOT_USERNAME}")

    # подключаем notifier (для API)
    from api_services.bot_notifier import set_bot
    set_bot(bot)

    build_router(dp)

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)
