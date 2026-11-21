"""Synthetic comparison bands for combi meal ingredient mass, not oven safety limits."""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

RULE_VERSION = 'combi-quality-load-v1'
REFERENCE_MASS_KG = {
    ('RICE', 'MEDIUM_FIXED_RACK_10'): Decimal('44'),
    ('RICE', 'LARGE_ROLL_IN_RACK_20'): Decimal('88'),
    ('MACKEREL', 'MEDIUM_FIXED_RACK_10'): Decimal('15'),
    ('MACKEREL', 'LARGE_ROLL_IN_RACK_20'): Decimal('30'),
}


@dataclass(frozen=True)
class CombiQualityLoad:
    band: str
    actual_mass_kg: Decimal
    reference_mass_kg: Decimal
    ratio: Decimal
    rule_version: str = RULE_VERSION


def classify(menu: str, size: str, mass_kg: str | Decimal) -> CombiQualityLoad:
    reference = REFERENCE_MASS_KG.get((menu, size))
    if reference is None or isinstance(mass_kg, bool) or not isinstance(mass_kg, (str, Decimal)):
        raise ValueError('unsupported synthetic load input')
    try:
        mass = Decimal(mass_kg)
    except InvalidOperation:
        raise ValueError('invalid synthetic load mass') from None
    if not mass.is_finite() or not Decimal('0') < mass <= Decimal('1000'):
        raise ValueError('invalid synthetic load mass')
    ratio = mass / reference
    band = 'PARTIAL' if ratio <= Decimal('0.5') else 'REFERENCE' if ratio <= 1 else 'OVER_RECOMMENDED'
    return CombiQualityLoad(band, mass, reference, ratio)
