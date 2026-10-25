from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, delete
from database.db import SessionLocal
from database.models import (
    CartItem, Product, Order, OrderItem, User, Promocode
)
from api_services.auth import get_current_user
from api_services.schemas import (
    CheckoutRequest, CartItemOut, OrderOut
)
from api_services.cart import _render_cart, pop_applied
from api_services.bot_notifier import notify_admin_new_order
from config import DELIVERY_PRICE

router = APIRouter(prefix="/api", tags=["orders"])


@router.post("/checkout", response_model=OrderOut)
async def checkout(req: CheckoutRequest, user=Depends(get_current_user)):
    uid = user["id"]

    if req.delivery_type not in ("pickup", "cdek"):
        raise HTTPException(400, "Неверный тип доставки")
    if req.delivery_type == "cdek":
        if not (req.delivery_name and req.delivery_phone and req.delivery_address):
            raise HTTPException(400, "Заполни все поля доставки")

    cart = await _render_cart(uid)
    if not cart.items:
        raise HTTPException(400, "Корзина пуста")

    delivery_price = DELIVERY_PRICE if req.delivery_type == "cdek" else 0
    final = cart.total + delivery_price
    applied_codes = pop_applied(uid)

    async with SessionLocal() as s:
        order = Order(
            user_id=uid,
            total=final,
            status="pending",
            promo_codes=",".join(applied_codes) if applied_codes else None,
            delivery_type=req.delivery_type,
            delivery_name=req.delivery_name,
            delivery_phone=req.delivery_phone,
            delivery_address=req.delivery_address,
        )
        s.add(order)
        await s.flush()

        for it in cart.items:
            s.add(OrderItem(order_id=order.id, product_id=it.product_id, price=it.price))

        # удаляем из наличия + из корзины
        for it in cart.items:
            p = await s.get(Product, it.product_id)
            if p:
                p.in_stock = False
        await s.execute(delete(CartItem).where(CartItem.user_id == uid))

        buyer = await s.get(User, uid)
        await s.commit()

        # отправляем уведомление админу через бота
        items_for_msg = [
            {"brand": it.brand, "flavor": it.flavor, "price": it.price}
            for it in cart.items
        ]
        await notify_admin_new_order(
            order_id=order.id,
            username=buyer.username if buyer else None,
            user_id=uid,
            items=items_for_msg,
            subtotal=cart.subtotal,
            percent=cart.percent_discount,
            fixed=cart.fixed_discount,
            delivery_type=req.delivery_type,
            delivery_name=req.delivery_name,
            delivery_phone=req.delivery_phone,
            delivery_address=req.delivery_address,
            delivery_price=delivery_price,
            final=final,
        )

        return OrderOut(
            id=order.id,
            total=order.total,
            status=order.status,
            delivery_type=order.delivery_type,
            delivery_name=order.delivery_name,
            delivery_phone=order.delivery_phone,
            delivery_address=order.delivery_address,
            created_at=order.created_at.isoformat() if order.created_at else "",
            items=cart.items,
        )


@router.get("/orders", response_model=list[OrderOut])
async def my_orders(user=Depends(get_current_user)):
    uid = user["id"]
    async with SessionLocal() as s:
        orders = (await s.execute(
            select(Order).where(Order.user_id == uid).order_by(Order.created_at.desc()).limit(50)
        )).scalars().all()

        result = []
        for o in orders:
            items = (await s.execute(
                select(OrderItem, Product).join(Product, OrderItem.product_id == Product.id)
                .where(OrderItem.order_id == o.id)
            )).all()
            items_out = [
                CartItemOut(
                    product_id=p.id, brand=p.brand, flavor=p.flavor, price=oi.price
                ) for oi, p in items
            ]
            result.append(OrderOut(
                id=o.id, total=o.total, status=o.status,
                delivery_type=o.delivery_type,
                delivery_name=o.delivery_name,
                delivery_phone=o.delivery_phone,
                delivery_address=o.delivery_address,
                created_at=o.created_at.isoformat() if o.created_at else "",
                items=items_out,
            ))
    return result
