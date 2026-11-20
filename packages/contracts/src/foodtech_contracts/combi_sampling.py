"""Validate representative synthetic mackerel safety samples against the loaded inventory."""

from collections import Counter
from decimal import Decimal
from itertools import combinations_with_replacement


def selection_issues(products: list[dict], chosen_ids: set[str], note: str | None) -> list[str]:
    selected = [product for product in products if product["product_id"] in chosen_ids]
    if len(selected) != min(3, len(products)):
        return ["SAMPLE_COUNT_INCOMPLETE"]
    racks = {product["rack_id"] for product in products}
    issues = []
    if len(racks) == 2:
        selected_counts = Counter(product["rack_id"] for product in selected)
        if set(selected_counts) != racks:
            issues.append("RACK_COVERAGE_INCOMPLETE")
        weights = {rack: sum(Decimal(p["mass_kg"]) for p in products if p["rack_id"] == rack) for rack in racks}
        if len(set(weights.values())) > 1:
            heavier = max(weights, key=weights.get)
            if selected_counts[heavier] != 2:
                issues.append("RACK_LOAD_SELECTION_INCOMPLETE")
        elif not note or not note.strip():
            issues.append("EQUAL_RACK_SELECTION_REASON_REQUIRED")
        if len(set(weights.values())) == 1:
            low, high = min(p["shelf"] for p in products), max(p["shelf"] for p in products)
            def zone(shelf):
                return "LOW" if shelf == low else "HIGH" if shelf == high else "MIDDLE"
            available = {rack: Counter(zone(p["shelf"]) for p in products if p["rack_id"] == rack) for rack in racks}
            best = 0
            for rack in racks:
                other = next(item for item in racks if item != rack)
                for first, second in combinations_with_replacement(available[rack], 2):
                    if first == second and available[rack][first] < 2:
                        continue
                    for third in available[other]:
                        best = max(best, len({first, second, third}))
            if len({zone(p["shelf"]) for p in selected}) < best:
                issues.append("RACK_HEIGHT_COVERAGE_INCOMPLETE")
    else:
        shelves = sorted({p["shelf"] for p in products})
        selected_shelves = Counter(p["shelf"] for p in selected)
        if len(shelves) <= 2 and max(Decimal(p["thickness_cm"]) for p in selected) < max(Decimal(p["thickness_cm"]) for p in products):
            issues.append("THICKEST_PRODUCT_NOT_SELECTED")
        if len(shelves) >= 3:
            middle = {shelves[(len(shelves) - 1) // 2], shelves[len(shelves) // 2]}
            if not ({shelves[0], shelves[-1]} <= set(selected_shelves) and middle & set(selected_shelves)):
                issues.append("SHELF_COVERAGE_INCOMPLETE")
        elif len(shelves) == 2:
            weights = {shelf: sum(Decimal(p["mass_kg"]) for p in products if p["shelf"] == shelf) for shelf in shelves}
            heavy = max(shelves, key=lambda shelf: (weights[shelf], shelf))
            if selected_shelves[heavy] != 2 or selected_shelves[next(s for s in shelves if s != heavy)] != 1:
                issues.append("SHELF_LOAD_SELECTION_INCOMPLETE")
            heavy_positions = [p.get("position") for p in selected if p["shelf"] == heavy]
            if len(heavy_positions) != 2 or None in heavy_positions or len(set(heavy_positions)) != 2:
                issues.append("SPATIAL_SEPARATION_UNCONFIRMED")
        elif len(products) >= 3 and {p.get("position") for p in selected} != {"LEFT", "CENTER", "RIGHT"}:
            issues.append("SINGLE_SHELF_POSITIONS_INCOMPLETE")
    return issues
