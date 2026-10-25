from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, func, delete
from database.db import SessionLocal
from database.models import (
    Category, Product, Order, OrderItem, User, Promocode
)
from api_services.auth import require_admin
from api_services.schemas import (
    AdminProductCreate, AdminProductUpdate, AdminUserOut,
    AdminStats, AdminOrderActionResponse
)
from api_services.bot_notifier import notify_order_approved, notify_order_rejected
from utils.promo import gen_promo  # переиспользуем функцию
from config import REFERRAL_MILESTONE, REFERRAL_MILESTONE_DISCOUNT

router = APIRouter(prefix="/api/admin", tags=["admin"])


# ============ ТОВАРЫ ============

@router.get("/products")
async def admin_products(user=Depends(require_admin)):
    async with SessionLocal() as s:
        products = (await s.execute(select(Product).order_by(Product.category_id, Product.number))).scalars().all()
        return [
            {
                "id": p.id, "category_id": p.category_id, "number": p.number,
                "brand": p.brand, "flavor": p.flavor, "price": p.price,
                "in_stock": p.in_stock,
            } for p in products
        ]


@router.post("/products")
async def admin_create_product(data: AdminProductCreate, user=Depends(require_admin)):
    async with SessionLocal() as s:
        max_num = (await s.execute(
            select(func.max(Product.number)).where(Product.category_id == data.category_id)
        )).scalar() or 0
        p = Product(
            category_id=data.category_id, number=max_num + 1,
            brand=data.brand, flavor=data.flavor, price=data.price, in_stock=True,
        )
        s.add(p)
        await s.commit()
        return {"id": p.id, "number": p.number}


@router.put("/products/{product_id}")
async def admin_update_product(product_id: int, data: AdminProductUpdate, user=Depends(require_admin)):
    async with SessionLocal() as s:
        p = await s.get(Product, product_id)
        if not p:
            raise HTTPException(404, "Товар не найден")
        if data.brand is not None:
            p.brand = data.brand
        if data.flavor is not None:
            p.flavor = data.flavor
        if data.price is not None:
            p.price = data.price
        await s.commit()
        return {"ok": True}


@router.delete("/products/{product_id}")
async def admin_delete_product(product_id: int, user=Depends(require_admin)):
    async with SessionLocal() as s:
        p = await s.get(Product, product_id)
        if not p:
            raise HTTPException(404, "Товар не найден")
        await s.delete(p)
        await s.commit()
        return {"ok": True}


# ============ ЗАКАЗЫ ============

@router.get("/orders")
async def admin_orders(user=Depends(require_admin), status: str | None = None):
    async with SessionLocal() as s:
        q = select(Order).order_by(Order.created_at.desc()).limit(100)
        if status:
            q = q.where(Order.status == status)
        orders = (await s.execute(q)).scalars().all()

        result = []
        for o in orders:
            buyer = await s.get(User, o.user_id)
            items = (await s.execute(
                select(OrderItem, Product).join(Product, OrderItem.product_id == Product.id)
                .where(OrderItem.order_id == o.id)
            )).all()
            result.append({
                "id": o.id, "total": o.total, "status": o.status,
                "username": buyer.username if buyer else None,
                "user_id": o.user_id,
                "delivery_type": o.delivery_type,
                "delivery_name": o.delivery_name,
                "delivery_phone": o.delivery_phone,
                "delivery_address": o.delivery_address,
                "created_at": o.created_at.isoformat() if o.created_at else "",
                "items": [{"brand": p.brand, "flavor": p.flavor, "price": oi.price} for oi, p in items],
            })
        return result


@router.post("/orders/{order_id}/approve", response_model=AdminOrderActionResponse)
async def admin_approve_order(order_id: int, user=Depends(require_admin)):
    async with SessionLocal() as s:
        order = await s.get(Order, order_id)
        if not order or order.status != "pending":
            raise HTTPException(400, "Заказ не в статусе pending")

        items = (await s.execute(
            select(OrderItem, Product).join(Product, OrderItem.product_id == Product.id)
            .where(OrderItem.order_id == order_id)
        )).all()

        order.status = "approved"
        for _, p in items:
            await s.delete(p)

        buyer = await s.get(User, order.user_id)
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
                buyer.first_purchase_done = True

        if order.promo_codes:
            for pc in order.promo_codes.split(","):
                pr = await s.get(Promocode, pc)
                if pr:
                    pr.used_count += 1
                    if pr.used_count >= pr.max_uses:
                        pr.used = True

        await s.commit()

        if buyer:
            await notify_order_approved(buyer.id, order_id)

        return AdminOrderActionResponse(status="approved", order_id=order_id)


@router.post("/orders/{order_id}/reject", response_model=AdminOrderActionResponse)
async def admin_reject_order(order_id: int, user=Depends(require_admin)):
    async with SessionLocal() as s:
        order = await s.get(Order, order_id)
        if not order or order.status != "pending":
            raise HTTPException(400, "Заказ не в статусе pending")

        items = (await s.execute(
            select(OrderItem, Product).join(Product, OrderItem.product_id == Product.id)
            .where(OrderItem.order_id == order_id)
        )).all()

        order.status = "rejected"
        for _, p in items:
            p.in_stock = True

        await s.commit()
        await notify_order_rejected(order.user_id, order_id)
        return AdminOrderActionResponse(status="rejected", order_id=order_id)


# ============ ЮЗЕРЫ ============

@router.get("/users")
async def admin_users(user=Depends(require_admin), q: str | None = None):
    async with SessionLocal() as s:
        sel = select(User).order_by(User.id.desc()).limit(200)
        if q:
            if q.startswith("@"):
                sel = sel.where(User.username == q[1:])
            elif q.isdigit():
                sel = sel.where(User.id == int(q))
            else:
                sel = sel.where(User.username.ilike(f"%{q}%"))
        users = (await s.execute(sel)).scalars().all()
        return [AdminUserOut(
            id=u.id, username=u.username, balance=u.balance or 0,
            referrals_count=u.referrals_count or 0, is_banned=u.is_banned,
        ) for u in users]


@router.post("/users/{user_id}/ban")
async def admin_ban_user(user_id: int, user=Depends(require_admin)):
    async with SessionLocal() as s:
        u = await s.get(User, user_id)
        if not u:
            raise HTTPException(404, "Юзер не найден")
        u.is_banned = True
        await s.commit()
        return {"ok": True}


@router.post("/users/{user_id}/unban")
async def admin_unban_user(user_id: int, user=Depends(require_admin)):
    async with SessionLocal() as s:
        u = await s.get(User, user_id)
        if not u:
            raise HTTPException(404, "Юзер не найден")
        u.is_banned = False
        await s.commit()
        return {"ok": True}


@router.post("/users/{user_id}/reset")
async def admin_reset_user(user_id: int, user=Depends(require_admin)):
    async with SessionLocal() as s:
        u = await s.get(User, user_id)
        if not u:
            raise HTTPException(404, "Юзер не найден")
        u.balance = 0
        u.referrals_count = 0
        u.next_bonus_at = 10
        u.first_purchase_done = False
        await s.execute(delete(Promocode).where(Promocode.owner_id == user_id))
        await s.commit()
        return {"ok": True}


# ============ СТАТИСТИКА ============

@router.get("/stats", response_model=AdminStats)
async def admin_stats(user=Depends(require_admin)):
    now = datetime.utcnow()
    day_ago = now - timedelta(days=1)
    week_ago = now - timedelta(days=7)

    async with SessionLocal() as s:
        total_users = (await s.execute(select(func.count(User.id)))).scalar() or 0
        total_orders = (await s.execute(
            select(func.count(Order.id)).where(Order.status == "approved")
        )).scalar() or 0
        pending = (await s.execute(
            select(func.count(Order.id)).where(Order.status == "pending")
        )).scalar() or 0
        total_revenue = (await s.execute(
            select(func.coalesce(func.sum(Order.total), 0)).where(Order.status == "approved")
        )).scalar() or 0
        revenue_day = (await s.execute(
            select(func.coalesce(func.sum(Order.total), 0))
            .where(Order.status == "approved", Order.created_at >= day_ago)
        )).scalar() or 0
        revenue_week = (await s.execute(
            select(func.coalesce(func.sum(Order.total), 0))
            .where(Order.status == "approved", Order.created_at >= week_ago)
        )).scalar() or 0
        avg_check = (total_revenue / total_orders) if total_orders else 0

        top_products = (await s.execute(
            select(OrderItem.product_id, func.count(OrderItem.id).label("cnt"))
            .group_by(OrderItem.product_id)
            .order_by(func.count(OrderItem.id).desc())
            .limit(3)
        )).all()
        top_list = []
        for pid, cnt in top_products:
            p = await s.get(Product, pid)
            if p:
                top_list.append({"brand": p.brand, "flavor": p.flavor, "count": cnt})

    return AdminStats(
        total_users=total_users, total_orders=total_orders,
        pending_orders=pending, total_revenue=total_revenue,
        revenue_day=revenue_day, revenue_week=revenue_week,
        avg_check=avg_check, top_products=top_list,
)
