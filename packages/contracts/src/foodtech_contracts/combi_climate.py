"""Compare reported climate setpoints with synthetic program settings.

Setpoints are not measured humidity; no failure duration is inferred here.
"""
from decimal import Decimal


def observe(values: list[Decimal | None], target: Decimal) -> dict:
    if not values or target not in (Decimal('100'), Decimal('10')):
        raise ValueError('invalid climate observation interval or target')
    if any(value is not None and (not isinstance(value, Decimal) or not value.is_finite() or
                                  not Decimal('0') <= value <= Decimal('100')) for value in values):
        raise ValueError('invalid climate setpoint')
    observed = sum(value is not None for value in values)
    mismatches = [second for second, value in enumerate(values, 1) if value is not None and value != target]
    status = ('DEVIATION_OBSERVED' if mismatches else 'NOT_MEASURED' if not observed else
              'INCOMPLETE' if observed != len(values) else 'MATCHED')
    return {'status': status, 'target_percent': str(target), 'observed_seconds': observed,
            'missing_seconds': len(values) - observed, 'deviation_seconds': len(mismatches),
            'first_deviation_second': mismatches[0] if mismatches else None,
            'reason_code': 'CLIMATE_SETPOINT_DEVIATION' if mismatches else None}
