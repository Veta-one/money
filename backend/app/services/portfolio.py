"""
Фактическая средневзвешенная доходность портфеля.

Считаем по тому, что реально лежит на счетах и под какую ставку:
- Account.interest_rate (%) → доходность по самому счёту (Binance Earn, накопит. карта)
- Deposit.rate (%)         → доходность по вкладу (через сервис вкладов)
- Долги в капитал не вносят доходности (как и не приносят процентов в нашей модели)

Доходность взвешивается по доле в рублёвом капитале на «сейчас».
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from .. import models
from .fx import to_rub


def portfolio_yield(db: Session) -> dict:
    """{actual_pct, working_share_pct, idle_rub, breakdown[]} — что реально приносит проценты.

    actual_pct        — средневзвешенная ставка по всему капиталу (%)
    working_share_pct — какая доля капитала работает (rate > 0)
    idle_rub          — сколько лежит мёртвым грузом (rate = 0) в рублях
    breakdown         — построчно: source, balance_rub, rate_pct, contribution_pct
    """
    rows: list[dict] = []
    total = 0.0
    working = 0.0
    idle = 0.0
    weighted_rate_sum = 0.0

    # 1) счета — обычные
    for a in db.query(models.Account).filter(models.Account.archived.is_(False)).all():
        bal_rub = to_rub(a.balance or 0.0, a.currency, db)
        if bal_rub <= 0:
            continue
        rate = float(a.interest_rate or 0.0)
        total += bal_rub
        weighted_rate_sum += bal_rub * rate
        if rate > 0:
            working += bal_rub
        else:
            idle += bal_rub
        rows.append({
            "kind": "account",
            "id": a.id,
            "name": a.name,
            "currency": a.currency,
            "balance_rub": round(bal_rub, 2),
            "rate_pct": round(rate, 2),
            "note": a.interest_note or "",
        })

    # 2) вклады — через сервис; value_now хранится в той же валюте, что и principal (обычно RUB)
    try:
        from .deposits import deposit_view
        for d in db.query(models.Deposit).all():
            view = deposit_view(d, db)
            if not view:
                continue
            val = float(view.get("value_now") or 0.0)
            if val <= 0:
                continue
            # счёт у вклада обычно типа deposit с валютой = RUB; на всякий случай — конвертация
            acc_obj = db.get(models.Account, d.account_id) if d.account_id else None
            cur = (acc_obj.currency if acc_obj else "RUB")
            val_rub = to_rub(val, cur, db)
            rate = float(d.rate or 0.0)
            total += val_rub
            weighted_rate_sum += val_rub * rate
            if rate > 0:
                working += val_rub
            rows.append({
                "kind": "deposit",
                "id": d.id,
                "name": f"Вклад · {d.bank or '—'}",
                "currency": cur,
                "balance_rub": round(val_rub, 2),
                "rate_pct": round(rate, 2),
                "note": "капитализация" if d.capitalization else "простой %",
            })
    except Exception:  # noqa: BLE001
        pass

    actual_pct = round(weighted_rate_sum / total, 2) if total > 0 else 0.0
    working_share = round(working / total * 100, 1) if total > 0 else 0.0
    # вклад каждой строки в итоговую ставку
    for r in rows:
        r["contribution_pct"] = round(r["balance_rub"] * r["rate_pct"] / total, 2) if total > 0 else 0.0

    return {
        "actual_pct": actual_pct,
        "working_share_pct": working_share,
        "idle_rub": round(idle, 2),
        "total_rub": round(total, 2),
        "breakdown": sorted(rows, key=lambda r: -r["balance_rub"]),
    }
