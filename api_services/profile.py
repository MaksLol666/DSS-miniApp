from fastapi import APIRouter, Depends
from sqlalchemy import select, func
from database.db import SessionLocal
from database.models import User, Order, Promocode
from api_services.auth import get_current_user
from api_services.schemas import ProfileOut, TopUserOut
from api_services.schemas import PromoOut

router = APIRouter(prefix="/api", tags=["profile"])


@router.get("/profile", response_model=ProfileOut)
async def get_profile(user=Depends(get_current_user)):
    uid = user["id"]
    async with SessionLocal() as s:
        u = await s.get(User, uid)
        if not u:
            return ProfileOut(
                user_id=uid, username=user.get("username"),
                balance=0, orders_count=0, referrals_count=0,
                ref_link="", active_promos=0,
            )

        orders_count = (await s.execute(
            select(func.count(Order.id)).where(
                Order.user_id == uid, Order.status == "approved"
            )
        )).scalar() or 0

        promos = (await s.execute(
            select(Promocode).where(
                Promocode.owner_id == uid, Promocode.used == False
            )
        )).scalars().all()

        from config import BOT_TOKEN  # not needed, just for ref
        # строим ссылку через username бота (можно сохранить в config)
        from bot import BOT_USERNAME
        ref_link = f"https://t.me/{BOT_USERNAME}?start=ref_{uid}" if BOT_USERNAME else ""

        return ProfileOut(
            user_id=uid,
            username=u.username,
            balance=u.balance or 0,
            orders_count=orders_count,
            referrals_count=u.referrals_count or 0,
            ref_link=ref_link,
            active_promos=len(promos),
        )


@router.get("/promos", response_model=list[PromoOut])
async def my_promos(user=Depends(get_current_user)):
    uid = user["id"]
    async with SessionLocal() as s:
        promos = (await s.execute(
            select(Promocode).where(
                Promocode.owner_id == uid, Promocode.used == False
            )
        )).scalars().all()
        return [PromoOut(code=p.code, kind=p.kind, amount=p.amount) for p in promos]


@router.get("/top", response_model=list[TopUserOut])
async def top_buyers(user=Depends(get_current_user)):
    async with SessionLocal() as s:
        users = (await s.execute(
            select(User).where(User.balance > 0).order_by(User.balance.desc()).limit(10)
        )).scalars().all()
        return [TopUserOut(username=u.username, user_id=u.id, value=u.balance) for u in users]


@router.get("/topref", response_model=list[TopUserOut])
async def top_refs(user=Depends(get_current_user)):
    async with SessionLocal() as s:
        users = (await s.execute(
            select(User).where(User.referrals_count > 0)
            .order_by(User.referrals_count.desc()).limit(10)
        )).scalars().all()
        return [TopUserOut(username=u.username, user_id=u.id, value=u.referrals_count) for u in users]
