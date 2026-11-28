"""Approved synthetic fryer batch boundaries; not a real equipment specification."""
from decimal import Decimal

RULE_VERSION = "tonkatsu-batch-v2"
RATED_KG = Decimal("4.0")
MAX_BATCH_G = 2500
WEIGHT_TOLERANCE_G = 5
PROFILE = {"THIN": (18, 135), "STANDARD": (15, 160), "THICK": (12, 200)}


def batch(profile: str, weights_g: list[int], mode: str) -> dict:
    if profile not in PROFILE or mode not in {"REFERENCE", "PARTIAL"}:
        raise ValueError("지원되는 두께 프로파일과 배치 유형이 필요합니다.")
    maximum, target = PROFILE[profile]
    if not weights_g or len(weights_g) > maximum or any(
            type(weight) is not int or not target - WEIGHT_TOLERANCE_G <= weight <= target + WEIGHT_TOLERANCE_G
            for weight in weights_g
    ):
        raise ValueError("두께별 개수와 제품별 목표 중량의 ±5g 범위를 확인해 주세요.")
    grams = sum(weights_g)
    if grams > MAX_BATCH_G or (mode == "REFERENCE" and grams < 2300) or \
            (mode == "PARTIAL" and grams >= 2300):
        raise ValueError("기준 배치는 2.3~2.5kg, 부분 배치는 2.3kg 미만이어야 합니다.")
    mass = Decimal(grams) / 1000
    return {"rule_version": RULE_VERSION, "mode": mode, "product_weights_g": weights_g,
            "product_count": len(weights_g), "total_mass_kg": str(mass),
            "rated_capacity_kg": str(RATED_KG),
            "load_percent": str((mass / RATED_KG * 100).quantize(Decimal("0.01")))}
