"""
Фактическая средневзвешенная доходность портфеля.

Только Deposit-инструменты несут процент. Сами по себе счета (карта, крипто-кошелёк,
наличные) доходности не дают — чтобы деньги работали, нужно явно открыть вклад/стейкинг
через раздел «Вклады»: с этого момента сумма списывается со счёта-источника и
становится частью «работающего» капитала.

Доходность взвешивается по доле в рублёвом капитале на «сейчас».
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from .. import models
from .fx import to_rub


def portfolio_yield(db: Session) -> dict:
    """{actual_pct, working_share_pct, idle_rub, breakdown[]} — что реально приносит проценты.

    actual_pct        — средневзвешенная ставка по всему капиталу (%)
    working_share_pct — какая доля капитала работает (через Deposits с rate > 0)
    idle_rub          — балансы счетов (не вложено ни во что доходное), в рублях
    breakdown         — построчно: source, balance_rub, rate_pct, contribution_pct
    """
    rows: list[dict] = []
    total = 0.0
    working = 0.0
    idle = 0.0
    weighted_rate_sum = 0.0

    # 1) сами счета — это «idle» по определению: они доходности не приносят
    for a in db.query(models.Account).filter(models.Account.archived.is_(False)).all():
        bal_rub = to_rub(a.balance or 0.0, a.currency, db)
        if bal_rub <= 0:
            continue
        total += bal_rub
        idle += bal_rub
        rows.append({
            "kind": "account",
            "id": a.id,
            "name": a.name,
            "currency": a.currency,
            "balance_rub": round(bal_rub, 2),
            "rate_pct": 0.0,
            "note": "лежит без процентов",
        })

    # 2) вклады/инвестиции — единственный источник реальной доходности
    try:
        from .deposits import deposit_view
        for d in db.query(models.Deposit).all():
            view = deposit_view(d, db)
            if not view:
                continue
            val = float(view.get("value_now") or 0.0)
            if val <= 0:
                continue
            cur = (d.currency or "RUB").upper() if hasattr(d, "currency") else "RUB"
            val_rub = to_rub(val, cur, db)
            rate = float(d.rate or 0.0)
            total += val_rub
            weighted_rate_sum += val_rub * rate
            if rate > 0:
                working += val_rub
            rows.append({
                "kind": "deposit",
                "id": d.id,
                "name": f"{d.bank or 'Вклад'}",
                "currency": cur,
                "balance_rub": round(val_rub, 2),
                "rate_pct": round(rate, 2),
                "note": "капитализация" if d.capitalization else "простой %",
            })
    except Exception:  # noqa: BLE001
        pass

    actual_pct = round(weighted_rate_sum / total, 2) if total > 0 else 0.0
    working_share = round(working / total * 100, 1) if total > 0 else 0.0
    for r in rows:
        r["contribution_pct"] = round(r["balance_rub"] * r["rate_pct"] / total, 2) if total > 0 else 0.0

    return {
        "actual_pct": actual_pct,
        "working_share_pct": working_share,
        "idle_rub": round(idle, 2),
        "total_rub": round(total, 2),
        "breakdown": sorted(rows, key=lambda r: -r["balance_rub"]),
    }
