import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON, CheckConstraint, DateTime, Enum, ForeignKey, Index, Integer, String,
    UniqueConstraint, text,
)
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------- catalog

class Drop(Base):
    __tablename__ = "drops"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True)
    drop_id: Mapped[int] = mapped_column(ForeignKey("drops.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    price_cents: Mapped[int] = mapped_column(Integer)
    image_emoji: Mapped[str] = mapped_column(String(8), default="👟")


class Inventory(Base):
    __tablename__ = "inventory"
    __table_args__ = (
        # Last line of defence: the DB itself refuses to go negative.
        CheckConstraint("available >= 0", name="ck_available_nonneg"),
    )

    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), primary_key=True)
    total: Mapped[int] = mapped_column(Integer)
    available: Mapped[int] = mapped_column(Integer)


# ---------------------------------------------------------------- users

class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(254), unique=True)
    password_hash: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---------------------------------------------------------------- reservations

class ReservationStatus(str, enum.Enum):
    ACTIVE = "active"        # stock is held, waiting for payment
    CONVERTED = "converted"  # paid, the stock is sold
    EXPIRED = "expired"      # TTL passed, stock returned
    RELEASED = "released"    # cancelled or refunded, stock returned


class Reservation(Base):
    __tablename__ = "reservations"
    _live = text("status IN ('ACTIVE','CONVERTED')")
    __table_args__ = (
        # Same user + same key = same reservation. This is the idempotency guarantee.
        UniqueConstraint("user_id", "idempotency_key", name="uq_user_idem"),
        # One purchase per customer per product, enforced by the database so
        # concurrent requests with different keys cannot both slip through.
        Index(
            "uq_one_purchase_per_product", "user_id", "product_id", unique=True,
            sqlite_where=_live, postgresql_where=_live,
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    qty: Mapped[int] = mapped_column(Integer)
    status: Mapped[ReservationStatus] = mapped_column(
        Enum(ReservationStatus), default=ReservationStatus.ACTIVE, index=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---------------------------------------------------------------- waiting room

class QueueStatus(str, enum.Enum):
    WAITING = "waiting"
    ADMITTED = "admitted"
    EXPIRED = "expired"   # admitted but never finished within the window
    DONE = "done"         # bought something or left


class QueueEntry(Base):
    __tablename__ = "queue_entries"
    __table_args__ = (UniqueConstraint("drop_id", "user_id", name="uq_queue_user"),)

    # Autoincrement id IS the queue order: first come, first served.
    id: Mapped[int] = mapped_column(primary_key=True)
    drop_id: Mapped[int] = mapped_column(ForeignKey("drops.id"), index=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[QueueStatus] = mapped_column(
        Enum(QueueStatus), default=QueueStatus.WAITING, index=True
    )
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    admitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    admit_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ---------------------------------------------------------------- orders and payments

class OrderStatus(str, enum.Enum):
    PENDING_PAYMENT = "pending_payment"
    PAID = "paid"
    EXPIRED = "expired"
    REFUNDED = "refunded"


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    # unique: one order per reservation, which makes order creation idempotent.
    reservation_id: Mapped[str] = mapped_column(ForeignKey("reservations.id"), unique=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    qty: Mapped[int] = mapped_column(Integer)
    amount_cents: Mapped[int] = mapped_column(Integer)
    status: Mapped[OrderStatus] = mapped_column(
        Enum(OrderStatus), default=OrderStatus.PENDING_PAYMENT, index=True
    )
    payment_intent_id: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WebhookEvent(Base):
    """Every gateway event id we have seen. The primary key is the dedupe."""
    __tablename__ = "webhook_events"

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    type: Mapped[str] = mapped_column(String(64))
    outcome: Mapped[str] = mapped_column(String(64), default="processing")
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---------------------------------------------------------------- outbox and consumers

class OutboxEvent(Base):
    __tablename__ = "outbox_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    topic: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )


class ProcessedEvent(Base):
    """(consumer, event) pairs already handled, so redelivery is harmless."""
    __tablename__ = "processed_events"

    consumer: Mapped[str] = mapped_column(String(64), primary_key=True)
    event_id: Mapped[int] = mapped_column(Integer, primary_key=True)


class Notification(Base):
    """Stand-in for an email provider: what the email consumer 'sent'."""
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(64))
    body: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SalesStat(Base):
    """Read model maintained by the analytics consumer."""
    __tablename__ = "sales_stats"

    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), primary_key=True)
    orders: Mapped[int] = mapped_column(Integer, default=0)
    units: Mapped[int] = mapped_column(Integer, default=0)
    revenue_cents: Mapped[int] = mapped_column(Integer, default=0)
