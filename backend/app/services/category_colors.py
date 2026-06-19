"""
Генерация цветов для категорий — стабильно, различимо, иерархично.

Стратегия:
- Корневые категории (parent_id IS NULL) получают hue по golden ratio rotation
  (137.508°) от индекса в отсортированном по id списке. Это математически
  гарантирует, что соседние индексы получают максимально удалённые оттенки.
- Дочерние категории (parent_id != NULL) получают HSL-вариацию цвета родителя:
  hue ± небольшой сдвиг, slight ±sat/light. Визуально образуют «семейство»
  одного оттенка, но различимы.
- saturation/lightness фиксированы для одинаковой воспринимаемой яркости.

Цвет сохраняется в Category.color (HEX) один раз и больше не пересчитывается —
пользователь видит стабильную цветовую схему.
"""
from __future__ import annotations

import colorsys

GOLDEN_ANGLE = 137.508
ROOT_SAT = 0.62      # насыщенность для корневых
ROOT_LIGHT = 0.55    # светлота для корневых
CHILD_HUE_STEP = 14  # ± градусов от родителя для дочерней
CHILD_LIGHT_STEP = 0.06


def _hsl_to_hex(h_deg: float, s: float, l: float) -> str:
    """HSL (h в градусах 0-360, s/l 0-1) → '#RRGGBB'."""
    r, g, b = colorsys.hls_to_rgb((h_deg % 360) / 360.0, l, s)
    return f"#{int(round(r*255)):02x}{int(round(g*255)):02x}{int(round(b*255)):02x}"


def _hex_to_hsl(hex_color: str) -> tuple[float, float, float] | None:
    """'#RRGGBB' → (h_deg, s, l). None если строка кривая."""
    s = (hex_color or "").lstrip("#")
    if len(s) != 6:
        return None
    try:
        r = int(s[0:2], 16) / 255.0
        g = int(s[2:4], 16) / 255.0
        b = int(s[4:6], 16) / 255.0
    except ValueError:
        return None
    h, l, sat = colorsys.rgb_to_hls(r, g, b)
    return (h * 360.0, sat, l)


def root_color(root_index: int) -> str:
    """Цвет корневой категории по её индексу в отсортированном списке корней.
    Golden ratio rotation гарантирует max различимость соседних индексов."""
    h = (root_index * GOLDEN_ANGLE) % 360
    return _hsl_to_hex(h, ROOT_SAT, ROOT_LIGHT)


def child_color(parent_hex: str, child_index: int) -> str:
    """Цвет дочерней категории как вариация родительского.
    child_index — индекс ребёнка среди детей этого родителя (0, 1, 2, …)."""
    hsl = _hex_to_hsl(parent_hex)
    if not hsl:
        # фоллбэк: трактуем как корень с произвольным индексом
        return root_color(child_index + 1)
    ph, ps, pl = hsl
    # чередуем светлее/темнее, hue смещаем влево/вправо
    direction = 1 if child_index % 2 == 0 else -1
    magnitude = 1 + child_index // 2          # 1, 1, 2, 2, 3, 3, …
    h = ph + direction * CHILD_HUE_STEP * magnitude
    l = max(0.35, min(0.72, pl + direction * CHILD_LIGHT_STEP))
    s = max(0.45, min(0.78, ps - 0.04 * (child_index // 2)))
    return _hsl_to_hex(h, s, l)


def assign_colors_to_categories(db) -> int:
    """Проставить color всем категориям где color IS NULL.
    Возвращает количество затронутых записей."""
    from .. import models
    cats = db.query(models.Category).order_by(models.Category.id.asc()).all()
    # сначала корневые, потом дети (чтобы при назначении ребёнка родитель уже имел цвет)
    roots = [c for c in cats if not c.parent_id]
    children = [c for c in cats if c.parent_id]

    updated = 0
    # корневые: индекс = позиция в отсортированном списке корней
    for idx, c in enumerate(roots):
        if c.color:
            continue
        c.color = root_color(idx)
        updated += 1

    # дочерние: индекс среди детей того же родителя
    parents_seen: dict[int, int] = {}
    children.sort(key=lambda c: (c.parent_id or 0, c.id))
    for c in children:
        if c.color:
            parents_seen[c.parent_id] = parents_seen.get(c.parent_id, -1) + 1
            continue
        idx_in_parent = parents_seen.get(c.parent_id, -1) + 1
        parents_seen[c.parent_id] = idx_in_parent
        parent = db.get(models.Category, c.parent_id)
        if parent and parent.color:
            c.color = child_color(parent.color, idx_in_parent)
        else:
            # сирота — даём корневой цвет по своему id
            c.color = root_color(c.id % 64)
        updated += 1

    if updated:
        db.commit()
    return updated


def color_for_new_category(db, parent_id: int | None) -> str:
    """Сгенерировать цвет для НОВОЙ категории.
    Корневая → следующий golden ratio индекс (= количество существующих корней).
    Дочерняя → child_color от родителя, индекс среди существующих сиблингов."""
    from .. import models
    if parent_id:
        parent = db.get(models.Category, parent_id)
        if parent:
            sibling_count = db.query(models.Category).filter_by(parent_id=parent_id).count()
            if not parent.color:
                # родитель без цвета — присвоим ему сначала
                root_idx = db.query(models.Category).filter(
                    models.Category.parent_id.is_(None),
                    models.Category.id <= parent.id).count() - 1
                parent.color = root_color(max(0, root_idx))
                db.commit()
            return child_color(parent.color, sibling_count)
    root_idx = db.query(models.Category).filter(models.Category.parent_id.is_(None)).count()
    return root_color(root_idx)
