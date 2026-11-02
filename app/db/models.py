"""Business data (customers, products, orders) + accountability data (messages, audit, actions, escalations)."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


# ---------- business data ----------
class Customer(Base):
    __tablename__ = "customers"
    id: Mapped[str] = mapped_column(String, primary_key=True)          # CUST-001
    name: Mapped[str] = mapped_column(String)
    email: Mapped[str] = mapped_column(String, unique=True, index=True)
    orders: Mapped[list["Order"]] = relationship(back_populates="customer")


class Product(Base):
    __tablename__ = "products"
    sku: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    category: Mapped[str] = mapped_column(String)
    price: Mapped[float] = mapped_column(Float)                         # current selling price, SGD
    original_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    is_sale: Mapped[bool] = mapped_column(Boolean, default=False)


ORDER_STATUSES = ("processing", "shipped", "delivered", "cancelled")


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[str] = mapped_column(String, primary_key=True)          # ORD-10021
    customer_id: Mapped[Optional[str]] = mapped_column(ForeignKey("customers.id"), nullable=True)  # null = guest
    email: Mapped[str] = mapped_column(String, index=True)             # used for guest verification
    status: Mapped[str] = mapped_column(String)                         # see ORDER_STATUSES
    placed_at: Mapped[datetime] = mapped_column(DateTime)
    shipped_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    delivered_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    carrier: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    tracking_no: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    total: Mapped[float] = mapped_column(Float)
    customer: Mapped[Optional[Customer]] = relationship(back_populates="orders")
    items: Mapped[list["OrderItem"]] = relationship(back_populates="order")


class OrderItem(Base):
    __tablename__ = "order_items"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    sku: Mapped[str] = mapped_column(ForeignKey("products.sku"))
    name: Mapped[str] = mapped_column(String)                           # snapshot at purchase
    qty: Mapped[int] = mapped_column(Integer, default=1)
    unit_price: Mapped[float] = mapped_column(Float)                    # price paid
    was_on_sale: Mapped[bool] = mapped_column(Boolean, default=False)   # sale status AT purchase time
    order: Mapped[Order] = relationship(back_populates="items")


class Policy(Base):
    __tablename__ = "policies"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    title: Mapped[str] = mapped_column(String)
    text: Mapped[str] = mapped_column(Text)


# ---------- accountability data ----------
class Conversation(Base):
    __tablename__ = "conversations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    customer_id: Mapped[Optional[str]] = mapped_column(ForeignKey("customers.id"), nullable=True)
    guest_email: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    mode: Mapped[str] = mapped_column(String, default="lab")   # lab | live: which model writes follow-ups
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ChatMessage(Base):
    """Neutral Message (app.llm.types) stored as JSON, so history replays on any provider."""
    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"), index=True)
    role: Mapped[str] = mapped_column(String)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class AuditEvent(Base):
    """Append-only log of every LLM call, tool call, guardrail hit and decision."""
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[Optional[int]] = mapped_column(ForeignKey("conversations.id"), index=True, nullable=True)
    kind: Mapped[str] = mapped_column(String)          # llm_call | tool_call | guardrail | action_decision
    name: Mapped[str] = mapped_column(String)
    input: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    output: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    provider: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    model: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


# action status: awaiting_approval -> approved | rejected ; or straight to done (auto-approved)
ACTION_STATUSES = ("awaiting_approval", "done", "approved", "rejected", "failed")


class Action(Base):
    """A business side effect (refund / exchange / cancel). Unique idempotency_key blocks duplicates."""
    __tablename__ = "actions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[Optional[int]] = mapped_column(ForeignKey("conversations.id"), nullable=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    kind: Mapped[str] = mapped_column(String)          # refund | exchange | cancel
    skus: Mapped[list] = mapped_column(JSON, default=list)
    amount: Mapped[float] = mapped_column(Float, default=0.0)
    reason: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String)
    idempotency_key: Mapped[str] = mapped_column(String, unique=True)
    decided_by: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Escalation(Base):
    __tablename__ = "escalations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[Optional[int]] = mapped_column(ForeignKey("conversations.id"), nullable=True)
    order_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    action_id: Mapped[Optional[int]] = mapped_column(ForeignKey("actions.id"), nullable=True)
    category: Mapped[str] = mapped_column(String)      # approval_needed | tool_failure | out_of_policy | customer_request | other
    reason: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String, default="open")   # open | resolved
    resolution_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)   # shared with the customer
    resolved_by: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    followup_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class EscalationNote(Base):
    """Internal staff comments on a ticket (never shown to the customer)."""
    __tablename__ = "escalation_notes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    escalation_id: Mapped[int] = mapped_column(ForeignKey("escalations.id"), index=True)
    author: Mapped[str] = mapped_column(String)
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
