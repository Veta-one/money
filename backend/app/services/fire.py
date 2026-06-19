"""
FIRE-калькулятор и прогноз капитала под РФ-реалии.

Ключевые отличия от классического 4% rule:
- Считаем через РЕАЛЬНУЮ доходность (номинал − инфляция).
- Дефолт инфляции 8% (среднесрочно для РФ), номинала 12% (микс депозит/MOEX/USD).
- Safe Withdrawal Rate понижен с 4% (US) до 2-3-4% (3 сценария).
- Все суммы — в «сегодняшних рублях» (real terms), чтобы FI number не плыл от инфляции.
"""
from __future__ import annotations

from datetime import date, datetime

from dateutil.relativedelta import relativedelta
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import models
from .fx import compute_net_worth, to_rub
from .portfolio import portfolio_yield
from .settings_store import get_setting

_USD_LIKE = {"USD", "USDT", "USDC", "$"}


def _rolling_monthly_avg(db: Session, tx_type: str, months: int) -> float:
    """Средняя месячная сумма расходов/доходов за последние `months` полных месяцев."""
    now = datetime.now()
    end = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    start = end - relativedelta(months=months)
    total = float(db.query(func.coalesce(func.sum(models.Transaction.base_amount_rub), 0.0))
                  .filter(models.Transaction.type == tx_type,
                          models.Transaction.datetime >= start,
                          models.Transaction.datetime < end).scalar() or 0.0)
    return round(total / months, 2) if months > 0 else 0.0


def _annual_expenses(db: Session) -> float:
    """Годовые расходы: rolling 12 → 6 → 3 в зависимости от того, сколько истории."""
    earliest = (db.query(func.min(models.Transaction.datetime))
                .filter(models.Transaction.type == "expense").scalar())
    if not earliest:
        return 0.0
    months_available = max(1, (datetime.now().year - earliest.year) * 12
                           + (datetime.now().month - earliest.month))
    window = 12 if months_available >= 12 else (6 if months_available >= 6 else 3)
    monthly = _rolling_monthly_avg(db, "expense", min(window, months_available))
    return round(monthly * 12, 2)


def _monthly_savings(db: Session) -> float:
    """Сколько откладываем в среднем: max(план−расход, факт_доход−расход).

    Расход — `expected_monthly_expense` (среднее за полные календарные месяцы),
    та же база, что и «можешь откладывать» на Целях, чтобы цифры совпадали
    между экранами. Раньше тут было 6-месячное окно с делением на 6, которое
    захватывало пустые/неполные стартовые месяцы и занижало расход → завышало
    накопления (плановый 233k − заниженный 119k = 114k вместо реальных 72k).

    План — сумма активных Recurring-источников (expected_income_monthly).
    Берём план если он больше факта, чтобы учитывать поступления, которые
    минуют Transaction (Туринвойс → крипта, пособия → вклад жены).
    """
    from .income import expected_income_monthly
    from .planning import expected_monthly_expense
    plan = expected_income_monthly(db)
    exp = expected_monthly_expense(db)               # 3 полных мес — как на Целях
    fact_inc = _rolling_monthly_avg(db, "income", 3)  # симметричное окно
    by_plan = plan - exp
    by_fact = fact_inc - exp
    return round(max(by_plan, by_fact, 0.0), 2)


def ru_pension_age(birth_year: int, gender: str) -> int:
    """Пенсионный возраст РФ (после реформы 2018, переходный период до 2028).

    gender: 'm'|'f'. Для нетипичных годов рождения возвращается ближайшее
    значение из переходной шкалы. Для современных пользователей (1980+) —
    мужчины 65, женщины 60.
    """
    g = (gender or "").lower()
    if g == "f":
        if birth_year >= 1969:
            return 60
        if birth_year == 1968:
            return 60
        if birth_year == 1967:
            return 59
        if birth_year == 1966:
            return 58
        if birth_year == 1965:
            return 57
        if birth_year == 1964:
            return 56
        return 55
    # мужчины и неуказанный пол
    if birth_year >= 1964:
        return 65
    if birth_year == 1963:
        return 65
    if birth_year == 1962:
        return 64
    if birth_year == 1961:
        return 63
    if birth_year == 1960:
        return 62
    if birth_year == 1959:
        return 61
    return 60


def _params(db: Session) -> dict:
    """Параметры пользователя из настроек (с дефолтами под РФ)."""
    # авто-расчёт лет до пенсии по дате рождения + полу
    years_default = 25
    birth_date = (get_setting(db, "birth_date") or "").strip()
    gender = (get_setting(db, "gender") or "").strip().lower()
    pension_age = None
    if birth_date:
        try:
            bd = date.fromisoformat(birth_date)
            pension_age = ru_pension_age(bd.year, gender or "m")
            today = date.today()
            age_now = today.year - bd.year - ((today.month, today.day) < (bd.month, bd.day))
            years_default = max(1, pension_age - age_now)
        except Exception:  # noqa: BLE001
            pass
    # явное переопределение «лет до пенсии» имеет приоритет (если задано)
    explicit = get_setting(db, "fire_years_to_retire")
    years_to_retire = int(float(explicit)) if explicit else years_default
    return {
        "inflation": float(get_setting(db, "fire_inflation") or 8.0) / 100.0,
        "nominal": float(get_setting(db, "fire_nominal_return") or 12.0) / 100.0,
        "custom_expenses": float(get_setting(db, "fire_fi_expenses") or 0),
        "target_alloc_rub": float(get_setting(db, "target_alloc_rub") or 70.0),
        "years_to_retire": years_to_retire,
        "birth_date": birth_date or None,
        "gender": gender or None,
        "pension_age": pension_age,
        "years_to_retire_auto": birth_date and pension_age is not None and not explicit,
    }


def _years_to_fi(net_worth: float, fi_target: float,
                 monthly_savings: float, real_annual_return: float) -> float | None:
    """Сколько лет до цели при текущей норме сбережений и реальной доходности."""
    # аналитическая формула FV аннуитета — даёт ответ для любого срока,
    # пользователь видит динамику (101 → 99 → ...) даже когда цель далека.
    import math
    if fi_target <= 0 or net_worth >= fi_target:
        return 0.0
    r_m = max(real_annual_return, 0.0) / 12.0
    # без сбережений и без процентов — никогда
    if monthly_savings <= 0 and r_m <= 0:
        return None
    # без процентов — линейный рост
    if r_m <= 0:
        return round((fi_target - net_worth) / monthly_savings / 12.0, 1)
    # без сбережений — только проценты
    if monthly_savings <= 0:
        try:
            n_months = math.log(fi_target / net_worth) / math.log(1 + r_m)
        except (ValueError, ZeroDivisionError):
            return None
        return round(max(0.0, n_months) / 12.0, 1)
    # общий случай: FV = P*(1+r)^n + M*((1+r)^n - 1)/r → solve for n
    A = monthly_savings / r_m
    try:
        x = (fi_target + A) / (net_worth + A)
        if x <= 1:
            return 0.0
        n_months = math.log(x) / math.log(1 + r_m)
    except (ValueError, ZeroDivisionError):
        return None
    return round(n_months / 12.0, 1)


def fire_metrics(db: Session) -> dict:
    """Полный FIRE-снимок: 3 сценария, Years to FI, Coast FI, Runway, прогресс."""
    p = _params(db)
    real_return = p["nominal"] - p["inflation"]
    annual_exp = p["custom_expenses"] if p["custom_expenses"] > 0 else _annual_expenses(db)
    monthly_savings = _monthly_savings(db)
    net_worth = compute_net_worth(db)

    # 3 сценария SWR: 2% / 3% / 4%. Multiple = 1/SWR (50× / 33× / 25×).
    scenarios = []
    for key, label, swr in [("safe", "Осторожный", 0.02),
                             ("base", "Базовый РФ", 0.03),
                             ("aggr", "Оптимистичный", 0.04)]:
        fi_num = annual_exp / swr if swr > 0 else 0.0
        scenarios.append({
            "key": key, "label": label,
            "swr_pct": round(swr * 100, 1),
            "multiple": round(1 / swr, 1),
            "fi_number": round(fi_num),
            "progress_pct": round(net_worth / fi_num * 100, 1) if fi_num > 0 else 0.0,
            "years_to_fi": _years_to_fi(net_worth, fi_num, monthly_savings, real_return),
        })

    # Coast FI: какой капитал СЕЙЧАС вырастет компаундом до FI к пенсии без новых вложений.
    base_fi = scenarios[1]["fi_number"]
    if real_return > 0:
        coast_today = base_fi / ((1 + real_return) ** p["years_to_retire"])
    else:
        coast_today = base_fi

    # Runway: на сколько месяцев хватит при остановке доходов и текущем темпе трат (без инфляции).
    monthly_exp = annual_exp / 12.0
    runway = round(net_worth / monthly_exp, 1) if monthly_exp > 0 else None

    # фактическая средневзвешенная доходность портфеля (под какую ставку лежат деньги)
    py = portfolio_yield(db)
    fact_nominal = py["actual_pct"] / 100.0
    fact_real = fact_nominal - p["inflation"]
    # «недополучаешь в год» = (план − факт) × капитал, в рублях
    yield_gap_rub = round((real_return - fact_real) * net_worth, 0) if net_worth > 0 else 0

    return {
        "net_worth": round(net_worth),
        "annual_expenses": round(annual_exp),
        "monthly_savings": round(monthly_savings),
        "monthly_expenses": round(monthly_exp),
        "inflation_pct": round(p["inflation"] * 100, 1),
        "nominal_return_pct": round(p["nominal"] * 100, 1),
        "real_return_pct": round(real_return * 100, 1),
        "scenarios": scenarios,
        "coast_fi_today": round(coast_today),
        "coast_fi_reached": net_worth >= coast_today,
        "years_to_retire": p["years_to_retire"],
        "years_to_retire_auto": bool(p.get("years_to_retire_auto")),
        "pension_age": p.get("pension_age"),
        "birth_date": p.get("birth_date"),
        "gender": p.get("gender"),
        "runway_months": runway,
        "savings_rate_pct": round(monthly_savings / (monthly_savings + monthly_exp) * 100, 1)
                            if (monthly_savings + monthly_exp) > 0 else 0.0,
        # фактическая доходность портфеля + дельта к плану
        "portfolio_yield_pct": py["actual_pct"],
        "portfolio_real_pct": round(fact_real * 100, 1),
        "portfolio_working_share_pct": py["working_share_pct"],
        "portfolio_idle_rub": py["idle_rub"],
        "yield_gap_rub_per_year": yield_gap_rub,
    }


def net_worth_forecast(db: Session, years: int | None = None) -> dict:
    """Прогноз капитала на N лет, в «сегодняшних рублях» (real terms).

    Допущения: ежемесячно копится `monthly_savings` (real), весь капитал растёт
    с реальной доходностью. Линия пересечения 3 FI-целей — отмечается.
    Если years не задан — горизонт = max(20, лет до пенсии + 5), чтобы график
    дотягивался до пенсии с запасом «жизни после».
    """
    p = _params(db)
    if years is None:
        years = max(20, int(p.get("years_to_retire") or 0) + 5)
    years = max(1, min(years, 50))
    real_return = p["nominal"] - p["inflation"]
    r_m = real_return / 12.0
    annual_exp = p["custom_expenses"] if p["custom_expenses"] > 0 else _annual_expenses(db)
    monthly_savings = _monthly_savings(db)
    cap0 = compute_net_worth(db)
    today = date.today()

    # фактическая доходность портфеля (по реальным ставкам счетов/вкладов)
    py = portfolio_yield(db)
    fact_nominal = py["actual_pct"] / 100.0
    fact_real = fact_nominal - p["inflation"]
    r_m_fact = fact_real / 12.0

    # FI-цели для отметок на графике
    fi_targets = {
        "safe": annual_exp / 0.02 if annual_exp > 0 else 0,
        "base": annual_exp / 0.03 if annual_exp > 0 else 0,
        "aggr": annual_exp / 0.04 if annual_exp > 0 else 0,
    }
    crossings = {k: None for k in fi_targets}

    points = []         # план: при заявленной номинальной доходности
    points_fact = []    # факт: при текущей средневзвешенной ставке портфеля
    cap = cap0
    cap_fact = cap0
    for m in range(years * 12 + 1):
        d = today + relativedelta(months=m)
        if m == 0 or m % 12 == 0:
            points.append({"month": m, "year_offset": m // 12,
                            "date": d.isoformat(), "value": round(cap)})
            points_fact.append({"month": m, "year_offset": m // 12,
                                "date": d.isoformat(), "value": round(cap_fact)})
        # фиксируем пересечение FI-целей (по плану)
        for k, target in fi_targets.items():
            if crossings[k] is None and target > 0 and cap >= target:
                crossings[k] = {"month": m, "year_offset": round(m / 12, 1),
                                "date": d.isoformat(), "target": round(target)}
        cap = cap * (1 + r_m) + monthly_savings
        cap_fact = cap_fact * (1 + r_m_fact) + monthly_savings

    return {
        "years": years,
        "monthly_savings": round(monthly_savings),
        "real_return_pct": round(real_return * 100, 1),
        "nominal_return_pct": round(p["nominal"] * 100, 1),
        "inflation_pct": round(p["inflation"] * 100, 1),
        "annual_expenses": round(annual_exp),
        "points": points,
        "points_fact": points_fact,
        "fact_real_return_pct": round(fact_real * 100, 1),
        "fact_nominal_return_pct": round(py["actual_pct"], 1),
        "portfolio_working_share_pct": py["working_share_pct"],
        "fi_targets": {k: round(v) for k, v in fi_targets.items()},
        "fi_crossings": crossings,
    }


def rolling_savings_rate(db: Session) -> dict:
    """Норма сбережений: текущий месяц + скользящие 3/6/12."""
    now = datetime.now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    def _sum(tx_type: str, since: datetime) -> float:
        return float(db.query(func.coalesce(func.sum(models.Transaction.base_amount_rub), 0.0))
                     .filter(models.Transaction.type == tx_type,
                             models.Transaction.datetime >= since).scalar() or 0.0)

    out: dict = {}
    cur_inc = _sum("income", month_start)
    cur_exp = _sum("expense", month_start)
    out["current"] = round((cur_inc - cur_exp) / cur_inc * 100) if cur_inc > 0 else 0
    for w in (3, 6, 12):
        start = (now - relativedelta(months=w)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        inc = _sum("income", start)
        exp = _sum("expense", start)
        out[f"r{w}m"] = round((inc - exp) / inc * 100) if inc > 0 else 0
    return out


def allocation_target(db: Session) -> dict:
    """Дрейф фактической валютной аллокации от целевой."""
    target_rub = float(get_setting(db, "target_alloc_rub") or 70.0)
    target_usd = round(100 - target_rub, 1)
    accounts = db.query(models.Account).filter(models.Account.archived.is_(False)).all()
    usd, rub = 0.0, 0.0
    for a in accounts:
        v = to_rub(a.balance or 0.0, a.currency, db)
        if (a.currency or "RUB").upper() in _USD_LIKE:
            usd += v
        else:
            rub += v
    total = usd + rub
    if total <= 0:
        return {"target_rub": target_rub, "target_usd": target_usd,
                "actual_rub": 0, "actual_usd": 0, "drift_pct": 0,
                "verdict": "neutral", "advice": "Нет данных по счетам."}
    actual_rub_pct = round(rub / total * 100, 1)
    actual_usd_pct = round(usd / total * 100, 1)
    drift = round(actual_usd_pct - target_usd, 1)   # − значит USD меньше цели
    abs_drift = abs(drift)
    if abs_drift <= 5:
        verdict, advice = "ok", "В пределах нормы (±5%)."
    elif abs_drift <= 10:
        verdict = "warn"
        advice = ("USD-доля выше целевой — можно зафиксировать часть прибыли в RUB."
                  if drift > 0 else "USD-доля ниже целевой — стоит докупить.")
    else:
        verdict = "bad"
        advice = ("Сильный дрейф в USD — ребалансировка." if drift > 0
                  else "Сильный дрейф в RUB — есть валютный риск.")
    return {
        "target_rub": target_rub, "target_usd": target_usd,
        "actual_rub": actual_rub_pct, "actual_usd": actual_usd_pct,
        "drift_pct": drift, "verdict": verdict, "advice": advice,
    }
