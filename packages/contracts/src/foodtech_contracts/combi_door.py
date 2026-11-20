"""Debounced door observations during active combi-oven cooking."""


def assess(states: list[str]) -> dict:
    if len(states) < 2 or any(state not in ("OPEN", "CLOSED") for state in states):
        raise ValueError("valid door states are required for the entire interval")
    if states[:2] != ["CLOSED", "CLOSED"]:
        raise ValueError("the initial loading door close must be confirmed for two seconds")
    confirmed, candidate, candidate_at, open_start = "CLOSED", None, None, None
    durations, changes = [], []
    for second, state in enumerate(states[2:], 3):
        if state == confirmed:
            candidate = candidate_at = None
        elif candidate == state:
            confirmed = state
            changes.append({"state": state, "observed_second": candidate_at, "confirmed_second": second})
            if state == "OPEN":
                open_start = candidate_at
            else:
                durations.append(candidate_at - open_start)
                open_start = None
            candidate = candidate_at = None
        else:
            candidate, candidate_at = state, second
    if open_start is not None:
        # A final unconfirmed close may later backdate the end of the opening.
        end = candidate_at if candidate == "CLOSED" else len(states) + 1
        durations.append(end - open_start)
    total, maximum = sum(durations), max(durations, default=0)
    status = ("FAILED" if total >= 30 or maximum >= 15 else "INSUFFICIENT_DATA" if candidate is not None
              else "WARNING" if total >= 10 else "NORMAL")
    reason = "DOOR_OPEN_FAILURE" if status == "FAILED" else "DOOR_OPEN_WARNING" if total >= 10 else None
    return {"status": status, "reason_code": reason, "door_open_seconds": total,
            "door_max_open_seconds": maximum, "door_changes": changes,
            "door_final_state_pending": candidate is not None}
