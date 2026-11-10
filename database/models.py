from datetime import datetime
import json

from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Legacy name: cumulative spending on approved orders.
    balance: Mapped[float] = mapped_column(Float, default=0.0)
    referrer_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=True)
    first_purchase_done: Mapped[bool] = mapped_column(Boolean, default=False)
    referrals_count: Mapped[int] = mapped_column(Integer, default=0)
    next_bonus_at: Mapped[int] = mapped_column(Integer, default=10)
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False)
    is_unreachable: Mapped[bool] = mapped_column(Boolean, default=False)


class Category(Base):
    __tablename__ = "categories"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)


class Product(Base):
    __tablename__ = "products"
    id: Mapped[int] = mapped_column(primary_key=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id"))
    number: Mapped[int] = mapped_column(Integer)
    brand: Mapped[str] = mapped_column(String(64))
    flavor: Mapped[str] = mapped_column(String(64))
    price: Mapped[float] = mapped_column(Float)
    cost_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    in_stock: Mapped[bool] = mapped_column(Boolean, default=True)
    stock_qty: Mapped[int] = mapped_column(Integer, default=1)
    image_urls: Mapped[str] = mapped_column(String(4096), default="[]")

    @property
    def images(self) -> list[str]:
        try:
            value = json.loads(self.image_urls or "[]")
            return value if isinstance(value, list) else []
        except (TypeError, ValueError):
            return []


class Favorite(Base):
    __tablename__ = "favorites"
    __table_args__ = (UniqueConstraint("user_id", "product_id", name="uq_favorites_user_product"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class CartItem(Base):
    __tablename__ = "cart_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    reserved: Mapped[bool] = mapped_column(Boolean, default=False)
    quantity: Mapped[int] = mapped_column(Integer, default=1)


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    total: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(24), default="accepted")
    promo_codes: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    delivery_type: Mapped[str] = mapped_column(String(16), default="pickup")
    delivery_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    delivery_phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    delivery_address: Mapped[str | None] = mapped_column(String(512), nullable=True)
    dcp_spent: Mapped[int] = mapped_column(Integer, default=0)
    club_discount: Mapped[float] = mapped_column(Float, default=0.0)


class OrderStatusHistory(Base):
    __tablename__ = "order_status_history"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    status: Mapped[str] = mapped_column(String(24))
    changed_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    note: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class OrderItem(Base):
    __tablename__ = "order_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"))
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    price: Mapped[float] = mapped_column(Float)
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    cost_price_snapshot: Mapped[float | None] = mapped_column(Float, nullable=True)
    brand_snapshot: Mapped[str | None] = mapped_column(String(64), nullable=True)
    flavor_snapshot: Mapped[str | None] = mapped_column(String(64), nullable=True)


class Promocode(Base):
    __tablename__ = "promocodes"
    code: Mapped[str] = mapped_column(String(16), primary_key=True)
    owner_id: Mapped[int] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(8), default="fixed")
    amount: Mapped[float] = mapped_column(Float)
    used: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[str] = mapped_column(String(8), default="system")
    max_uses: Mapped[int] = mapped_column(Integer, default=1)
    used_count: Mapped[int] = mapped_column(Integer, default=0)
    target_type: Mapped[str] = mapped_column(String(16), default="all")
    target_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    min_order: Mapped[float] = mapped_column(Float, default=0.0)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class CartPromo(Base):
    __tablename__ = "cart_promos"
    __table_args__ = (UniqueConstraint("user_id", "code", name="uq_cart_promos_user_code"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    code: Mapped[str] = mapped_column(ForeignKey("promocodes.code"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class CartClubRedemption(Base):
    __tablename__ = "cart_club_redemptions"
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    points: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())




class ClubAccount(Base):
    __tablename__ = "club_accounts"
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), primary_key=True)
    balance: Mapped[int] = mapped_column(Integer, default=0)
    lifetime_earned: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class ClubTransaction(Base):
    __tablename__ = "club_transactions"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    event_key: Mapped[str] = mapped_column(String(128), unique=True)
    kind: Mapped[str] = mapped_column(String(16))
    amount: Mapped[int] = mapped_column(Integer)
    balance_after: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ClubAchievementUnlock(Base):
    __tablename__ = "club_achievement_unlocks"
    __table_args__ = (UniqueConstraint("user_id", "achievement_key", name="uq_club_achievement_user_key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    achievement_key: Mapped[str] = mapped_column(String(48))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ClubTaskConfig(Base):
    __tablename__ = "club_task_configs"
    key: Mapped[str] = mapped_column(String(48), primary_key=True)
    title: Mapped[str] = mapped_column(String(96))
    description: Mapped[str] = mapped_column(String(256))
    period: Mapped[str] = mapped_column(String(12))
    event_type: Mapped[str] = mapped_column(String(24))
    target: Mapped[int] = mapped_column(Integer, default=1)
    reward: Mapped[int] = mapped_column(Integer, default=30)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class ClubTaskProgress(Base):
    __tablename__ = "club_task_progress"
    __table_args__ = (UniqueConstraint("user_id", "task_key", "period_key", name="uq_club_task_user_period"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    task_key: Mapped[str] = mapped_column(String(48))
    period_key: Mapped[str] = mapped_column(String(16))
    progress: Mapped[int] = mapped_column(Integer, default=0)
    claimed: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class ClubTitleSelection(Base):
    __tablename__ = "club_title_selections"
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), primary_key=True)
    title_key: Mapped[str] = mapped_column(String(48))
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class Review(Base):
    __tablename__ = "reviews"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), unique=True)
    rating: Mapped[int] = mapped_column(Integer)
    text: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    photo_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    has_photo: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    published_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
