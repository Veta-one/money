"""
Финансовый чекап: сводка «здоровья» + правила-рекомендации.
Норма сбережений, подушка (мес расходов), валютное распределение, подписки.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from .analytics import subscriptions
from .capital import capital_overview
from .income import expected_income_monthly
from .planning import avg_monthly_expense


def financial_checkup(db: Session) -> dict:
    income_m = expected_income_monthly(db)
    expense_m = avg_monthly_expense(db)
    savings_m = round(income_m - expense_m)
    rate = round(savings_m / income_m * 100) if income_m > 0 else 0

    cap = capital_overview(db)
    cushion = cap.get("emergency", {}).get("months")
    alloc = cap.get("allocation_currency", [])
    total = sum(a["sum"] for a in alloc) or 1
    rub = next((a["sum"] for a in alloc if a["name"] == "RUB"), 0)
    rub_share = round(rub / total * 100)
    subs = subscriptions(db)["total"]

    recs: list[dict] = []
    if cushion is None:
        recs.append({"l": "info", "t": "Подушка посчитается, когда накопятся снимки капитала и реальные расходы."})
    elif cushion < 3:
        recs.append({"l": "bad", "t": f"Подушка {cushion} мес — мало. Приоритет: довести до 3–6 месяцев расходов."})
    elif cushion < 6:
        recs.append({"l": "warn", "t": f"Подушка {cushion} мес — почти. Добей до 6."})
    else:
        recs.append({"l": "good", "t": f"Подушка {cushion} мес — отлично, форс-мажор закрыт."})

    if income_m > 0:
        if rate < 10:
            recs.append({"l": "bad", "t": f"Норма сбережений {rate}% — низко. Урежь крупнейшую категорию расходов."})
        elif rate < 30:
            recs.append({"l": "warn", "t": f"Норма сбережений {rate}% — ок, но есть куда расти."})
        else:
            recs.append({"l": "good", "t": f"Норма сбережений {rate}% — сильно, так держать."})

    if total > 1 and rub_share < 5:
        recs.append({"l": "warn", "t": "Рублёвой ликвидности почти нет — держи ~1 мес расходов в ₽ на текущие траты."})
    if income_m > 0 and subs > 0.1 * income_m:
        recs.append({"l": "warn", "t": f"Подписки {round(subs)} ₽/мес — заметная доля дохода, пройдись по списку."})

    return {
        "income_m": round(income_m), "expense_m": round(expense_m), "savings_m": savings_m,
        "savings_rate": rate, "cushion_months": cushion,
        "rub_share": rub_share, "usd_share": (100 - rub_share) if total > 1 else None,
        "subscriptions": round(subs), "recommendations": recs[:4],
    }
