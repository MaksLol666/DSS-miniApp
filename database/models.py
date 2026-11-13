from datetime import datetime
import json

from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, func
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
    orders_adjustment: Mapped[int] = mapped_column(Integer, default=0)
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


class ClubSeason(Base):
    __tablename__ = "club_seasons"
    season_number: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(96))
    starts_at: Mapped[datetime] = mapped_column(DateTime)
    ends_at: Mapped[datetime] = mapped_column(DateTime)


class ClubSeasonPoint(Base):
    __tablename__ = "club_season_points"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    season_number: Mapped[int] = mapped_column(ForeignKey("club_seasons.season_number"), index=True)
    event_key: Mapped[str] = mapped_column(String(128), unique=True)
    amount: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(160))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ClubSeasonReward(Base):
    __tablename__ = "club_season_rewards"
    __table_args__ = (UniqueConstraint("user_id", "season_number", "reward_key", name="uq_club_season_reward"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    season_number: Mapped[int] = mapped_column(ForeignKey("club_seasons.season_number"), index=True)
    reward_key: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(160))
    rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ClubSetting(Base):
    __tablename__ = "club_settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="{}")
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class ClubCosmetic(Base):
    __tablename__ = "club_cosmetics"
    key: Mapped[str] = mapped_column(String(48), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(96))
    description: Mapped[str] = mapped_column(String(256), default="")
    icon: Mapped[str] = mapped_column(String(32), default="✦")
    rarity: Mapped[str] = mapped_column(String(16), default="common")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    card_primary: Mapped[str] = mapped_column(String(16), default="#26133b")
    card_secondary: Mapped[str] = mapped_column(String(16), default="#0b0812")
    card_accent: Mapped[str] = mapped_column(String(16), default="#c49aff")
    card_ornament: Mapped[str] = mapped_column(String(16), default="#ffffff")
    card_text: Mapped[str] = mapped_column(String(16), default="#f8f5ff")
    card_animation: Mapped[str] = mapped_column(String(16), default="pulse")
    requirement_type: Mapped[str] = mapped_column(String(24), default="none")
    requirement_value: Mapped[int] = mapped_column(Integer, default=0)
    card_font: Mapped[str] = mapped_column(String(24), default="orbitron")
    max_supply: Mapped[int] = mapped_column(Integer, default=0)
    issued_count: Mapped[int] = mapped_column(Integer, default=0)


class ClubMonthlyCoupon(Base):
    __tablename__ = "club_monthly_coupons"
    __table_args__ = (UniqueConstraint("user_id", "month_key", name="uq_club_monthly_coupon_user_month"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    month_key: Mapped[str] = mapped_column(String(7))
    code: Mapped[str] = mapped_column(String(16), unique=True)
    level_code: Mapped[str] = mapped_column(String(16))
    discount: Mapped[int] = mapped_column(Integer)
    min_order: Mapped[int] = mapped_column(Integer)
    notified: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ClubUserCosmetic(Base):
    __tablename__ = "club_user_cosmetics"
    __table_args__ = (UniqueConstraint("user_id", "cosmetic_key", name="uq_club_user_cosmetic"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    cosmetic_key: Mapped[str] = mapped_column(String(48), ForeignKey("club_cosmetics.key"))
    granted_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ClubCosmeticLoadout(Base):
    __tablename__ = "club_cosmetic_loadouts"
    __table_args__ = (UniqueConstraint("user_id", "kind", name="uq_club_cosmetic_loadout_user_kind"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    cosmetic_key: Mapped[str] = mapped_column(String(48), ForeignKey("club_cosmetics.key"))
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
