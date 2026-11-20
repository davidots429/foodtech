"""Summarize reported fan levels without inventing a process-failure threshold."""


def observe(levels: list[int | None], target: int) -> dict:
    if not levels or target not in (3, 4):
        raise ValueError("invalid fan observation interval or target")
    if any(value is not None and (type(value) is not int or not 0 <= value <= 10) for value in levels):
        raise ValueError("invalid fan level")
    observed = sum(value is not None for value in levels)
    mismatches = [second for second, value in enumerate(levels, 1) if value is not None and value != target]
    status = ("DEVIATION_OBSERVED" if mismatches else "NOT_MEASURED" if not observed else
              "INCOMPLETE" if observed != len(levels) else "MATCHED")
    return {"status": status, "target_level": target, "observed_seconds": observed,
            "missing_seconds": len(levels) - observed, "deviation_seconds": len(mismatches),
            "first_deviation_second": mismatches[0] if mismatches else None,
            "reason_code": "FAN_DEVIATION" if mismatches else None}
