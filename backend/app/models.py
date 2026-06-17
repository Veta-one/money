"""
Схема БД (см. SPEC.md §3). Всё в одном файле — для одного пользователя это
проще поддерживать, чем десяток модулей.

Денежные суммы храним во float (для личного учёта достаточно; при желании
позже перейдём на Numeric). Любая сумма имеет валюту + base_amount_rub
(сведение к рублю по курсу на момент операции).
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (Boolean, Date, DateTime, Float, ForeignKey, Integer,
                        String, Text, UniqueConstraint, func)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    tg_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Account(Base):
    __tablename__ = "accounts"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    type: Mapped[str] = mapped_column(String(16))          # card|cash|deposit|crypto|external
    currency: Mapped[str] = mapped_column(String(8), default="RUB")
    owner: Mapped[str] = mapped_column(String(16), default="me")  # me|wife
    is_external: Mapped[bool] = mapped_column(Boolean, default=False)
    balance: Mapped[float] = mapped_column(Float, default=0.0)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Category(Base):
    __tablename__ = "categories"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"))
    type: Mapped[str] = mapped_column(String(16))          # expense|income|transfer
    icon: Mapped[str | None] = mapped_column(String(32))
    archived: Mapped[bool] = mapped_column(Boolean, default=False)

    children: Mapped[list["Category"]] = relationship()


class Transaction(Base):
    __tablename__ = "transactions"
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"))
    datetime: Mapped[datetime] = mapped_column(DateTime, index=True)
    amount: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(8), default="RUB")
    base_amount_rub: Mapped[float] = mapped_column(Float)
    fx_rate: Mapped[float] = mapped_column(Float, default=1.0)
    type: Mapped[str] = mapped_column(String(16))          # expense|income|transfer|debt
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"))
    merchant: Mapped[str | None] = mapped_column(String(256))
    counterparty_account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"))
    source: Mapped[str] = mapped_column(String(16))        # receipt|statement|text|voice|photo
    note: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="confirmed")  # confirmed|needs_review
    dedup_key: Mapped[str | None] = mapped_column(String(128), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    items: Mapped[list["TransactionItem"]] = relationship(
        back_populates="transaction", cascade="all, delete-orphan"
    )
    receipt: Mapped["Receipt | None"] = relationship(
        back_populates="transaction", uselist=False, cascade="all, delete-orphan"
    )


class TransactionItem(Base):
    __tablename__ = "transaction_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    transaction_id: Mapped[int] = mapped_column(ForeignKey("transactions.id"))
    name: Mapped[str] = mapped_column(String(256))
    name_normalized: Mapped[str | None] = mapped_column(String(256), index=True)
    qty: Mapped[float] = mapped_column(Float, default=1.0)
    price: Mapped[float] = mapped_column(Float, default=0.0)
    sum: Mapped[float] = mapped_column(Float, default=0.0)
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"))

    transaction: Mapped["Transaction"] = relationship(back_populates="items")


class Receipt(Base):
    """Фискальные данные чека ФНС (источник item-level)."""
    __tablename__ = "receipts"
    id: Mapped[int] = mapped_column(primary_key=True)
    transaction_id: Mapped[int] = mapped_column(ForeignKey("transactions.id"), unique=True)
    fn: Mapped[str] = mapped_column(String(32))
    fd: Mapped[str] = mapped_column(String(32))
    fp: Mapped[str] = mapped_column(String(32))
    t: Mapped[str] = mapped_column(String(20))
    s: Mapped[str] = mapped_column(String(20))
    n: Mapped[int] = mapped_column(Integer, default=1)
    raw_qr: Mapped[str | None] = mapped_column(Text)
    fns_json: Mapped[str | None] = mapped_column(Text)
    kkt_owner: Mapped[str | None] = mapped_column(String(256))
    inn: Mapped[str | None] = mapped_column(String(16), index=True)
    retail_place: Mapped[str | None] = mapped_column(String(256))

    __table_args__ = (UniqueConstraint("fn", "fd", "fp", name="uq_fiscal_key"),)

    transaction: Mapped["Transaction"] = relationship(back_populates="receipt")


class CategoryRule(Base):
    """Выученное правило категоризации (магазин/товар → категория)."""
    __tablename__ = "category_rules"
    id: Mapped[int] = mapped_column(primary_key=True)
    match_inn: Mapped[str | None] = mapped_column(String(16), index=True)
    match_item_pattern: Mapped[str | None] = mapped_column(String(256))
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id"))
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    hits: Mapped[int] = mapped_column(Integer, default=1)
    auto: Mapped[bool] = mapped_column(Boolean, default=False)


class Recurring(Base):
    """Регулярный доход/расход со СРОКОМ действия (субсидии, подписки)."""
    __tablename__ = "recurring"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"))
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"))
    amount: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(8), default="RUB")
    period: Mapped[str] = mapped_column(String(16), default="monthly")  # monthly|weekly
    day: Mapped[int | None] = mapped_column(Integer)
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)   # nullable = бессрочно
    type: Mapped[str] = mapped_column(String(16))         # income|expense
    reminder: Mapped[bool] = mapped_column(Boolean, default=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Deposit(Base):
    __tablename__ = "deposits"
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"))
    bank: Mapped[str | None] = mapped_column(String(128))
    principal: Mapped[float] = mapped_column(Float, default=0.0)
    rate: Mapped[float] = mapped_column(Float, default=0.0)
    term_start: Mapped[date | None] = mapped_column(Date)
    term_end: Mapped[date | None] = mapped_column(Date)
    capitalization: Mapped[bool] = mapped_column(Boolean, default=False)
    monthly_topup: Mapped[float] = mapped_column(Float, default=0.0)
    owner: Mapped[str] = mapped_column(String(16), default="me")


class Goal(Base):
    __tablename__ = "goals"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    target_amount: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(8), default="RUB")
    target_date: Mapped[date | None] = mapped_column(Date)
    current_amount: Mapped[float] = mapped_column(Float, default=0.0)
    monthly_plan: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(16), default="active")


class Debt(Base):
    __tablename__ = "debts"
    id: Mapped[int] = mapped_column(primary_key=True)
    counterparty: Mapped[str] = mapped_column(String(128))
    direction: Mapped[str] = mapped_column(String(16))    # i_owe|owed_to_me
    amount: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(8), default="RUB")
    date: Mapped[date | None] = mapped_column(Date)
    due_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(16), default="open")  # open|closed


class NetWorthSnapshot(Base):
    __tablename__ = "net_worth_snapshots"
    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    total_rub: Mapped[float] = mapped_column(Float)
    breakdown_json: Mapped[str | None] = mapped_column(Text)


class FxRate(Base):
    __tablename__ = "fx_rates"
    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[date] = mapped_column(Date)
    currency: Mapped[str] = mapped_column(String(8))
    rate_rub: Mapped[float] = mapped_column(Float)
    __table_args__ = (UniqueConstraint("date", "currency", name="uq_fx_date_cur"),)


class ExternalReport(Base):
    """Отчёт по внешнему счёту (жена) для сверки/поиска утечек."""
    __tablename__ = "external_reports"
    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[date] = mapped_column(Date)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    reported_balance: Mapped[float | None] = mapped_column(Float)
    reported_income: Mapped[float | None] = mapped_column(Float)
    reported_spend: Mapped[float | None] = mapped_column(Float)
    note: Mapped[str | None] = mapped_column(Text)


class Setting(Base):
    """Гибкие настройки key-value (дайджесты, safe-to-spend и т.п.)."""
    __tablename__ = "settings"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    value: Mapped[str | None] = mapped_column(Text)
