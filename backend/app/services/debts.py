"""
Долговые операции: человекочитаемая метка операции, привязанной к долгу,
и сводная активность по долгам за период (неделя/месяц/год).

Направление денег зашито в знак base_amount_rub (см. reclassify): приход (+),
расход (−). Вместе с направлением самого долга это даёт 4 типа операции:
  i_owe      + приход  → «Взял в долг»   (borrow)
  i_owe      − расход  → «Погасил долг»  (repay)
  owed_to_me − расход  → «Дал в долг»    (lend)
  owed_to_me + приход  → «Вернули долг»  (getback)
"""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy.orm import Session

from .. import models

_LABEL = {
    "borrow":  "Взял в долг",
    "repay":   "Погасил долг",
    "lend":    "Дал в долг",
    "getback": "Вернули долг",
}


def op_meta(tx, debt) -> dict | None:
    """Метка долговой операции для отображения. None — если связи с долгом нет."""
    if not debt:
        return None
    incoming = (tx.base_amount_rub or 0) > 0 or tx.type == "income"
    if debt.direction == "i_owe":
        kind = "borrow" if incoming else "repay"
    else:  # owed_to_me
        kind = "getback" if incoming else "lend"
    return {"debt_id": debt.id, "kind": kind, "label": _LABEL[kind],
            "incoming": incoming, "counterparty": debt.counterparty,
            "debt_status": debt.status}


def debt_map_for(db: Session, txs) -> dict:
    """tx.id → op_meta для всех операций списка, привязанных к долгу (батч-фетч долгов)."""
    ids = {t.debt_id for t in txs if getattr(t, "debt_id", None)}
    if not ids:
        return {}
    debts = {d.id: d for d in db.query(models.Debt).filter(models.Debt.id.in_(ids)).all()}
    out = {}
    for t in txs:
        did = getattr(t, "debt_id", None)
        if did and did in debts:
            m = op_meta(t, debts[did])
            if m:
                out[t.id] = m
    return out


def _period_start(period: str) -> date:
    today = date.today()
    if period == "week":
        return today - timedelta(days=7)
    if period == "year":
        return today.replace(month=1, day=1)
    return today.replace(day=1)  # month по умолчанию


def debt_activity(db: Session, recent: int = 60) -> dict:
    """Сводка по долговым операциям: суммы за нед/мес/год + последние операции."""
    txs = (db.query(models.Transaction)
           .filter(models.Transaction.debt_id.isnot(None))
           .order_by(models.Transaction.datetime.desc()).all())
    dmap = debt_map_for(db, txs)
    _key = {"borrow": "borrowed", "repay": "repaid", "lend": "lent", "getback": "got_back"}

    def bucket(since: date) -> dict:
        b = {"borrowed": 0.0, "repaid": 0.0, "lent": 0.0, "got_back": 0.0, "count": 0}
        for t in txs:
            m = dmap.get(t.id)
            if not m or t.datetime.date() < since:
                continue
            b[_key[m["kind"]]] += abs(t.base_amount_rub or t.amount or 0.0)
            b["count"] += 1
        return {k: (v if k == "count" else round(v)) for k, v in b.items()}

    periods = {p: bucket(_period_start(p)) for p in ("week", "month", "year")}
    ops = []
    for t in txs:
        m = dmap.get(t.id)
        if not m:
            continue
        ops.append({"id": t.id, "dt": t.datetime.isoformat(),
                    "amount": round(abs(t.base_amount_rub or t.amount or 0.0)),
                    "incoming": m["incoming"], "label": m["label"],
                    "kind": m["kind"], "counterparty": m["counterparty"],
                    "debt_status": m["debt_status"]})
        if len(ops) >= recent:
            break
    return {**periods, "ops": ops}
