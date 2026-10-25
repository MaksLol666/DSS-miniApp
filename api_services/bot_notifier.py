from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

_bot: Bot | None = None


def set_bot(bot: Bot):
    global _bot
    _bot = bot


async def _send(user_id: int, text: str, **kwargs):
    if not _bot:
        return
    try:
        await _bot.send_message(user_id, text, **kwargs)
    except Exception as e:
        print(f"notify error: {e}")


async def notify_admin_new_order(
    order_id: int, username, user_id, items, subtotal,
    percent, fixed, delivery_type, delivery_name, delivery_phone,
    delivery_address, delivery_price, final,
):
    from config import ADMIN_ID
    lines = "\n".join(f"• {i['brand']} — {i['flavor']} — {int(i['price'])}₽" for i in items)
    text = f"🆕 <b>Новый заказ №{order_id}</b>\n"
    text += f"От: @{username or user_id} (<code>{user_id}</code>)\n\n{lines}\n"
    if percent:
        text += f"\n🎟 Скидка {int(percent)}%: −{int(subtotal * percent / 100)}₽"
    if fixed:
        text += f"\n🎟 Промо: −{int(fixed)}₽"
    if delivery_type == "cdek":
        text += f"\n\n🚚 <b>СДЭК (+{delivery_price}₽)</b>"
        text += f"\n👤 {delivery_name}"
        text += f"\n📱 {delivery_phone}"
        text += f"\n📍 {delivery_address}"
    else:
        text += f"\n\n🏠 <b>Самовывоз</b>"
    text += f"\n\n<b>К оплате: {int(final)}₽</b>"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Принять", callback_data=f"order:approve:{order_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"order:reject:{order_id}"),
        ]
    ])
    await _send(ADMIN_ID, text, reply_markup=kb, parse_mode="HTML")


async def notify_order_approved(user_id: int, order_id: int):
    await _send(user_id, f"✅ Заказ №{order_id} подтверждён! Спасибо за покупку 💚")


async def notify_order_rejected(user_id: int, order_id: int):
    await _send(user_id, f"❌ Заказ №{order_id} отклонён администратором.")
