"""Deterministic five-second sequence restoration for one source/device/batch."""

from dataclasses import dataclass, field, replace


@dataclass(frozen=True)
class Received:
    ingestion_event_id: str
    raw: str
    source_id: str
    device_id: str
    batch_id: str
    sequence_number: int
    received_at: float
    sequence_status: str = "IN_ORDER"
    gap_before: tuple[int, ...] = ()


@dataclass
class StreamState:
    expected: int = 1
    held: dict[int, list[Received]] = field(default_factory=dict)
    gap_deadline: float | None = None
    closed_gaps: list[tuple[int, int]] = field(default_factory=list)


class Resequencer:
    def __init__(self, gap_seconds: float = 5.0):
        self.gap_seconds = gap_seconds
        self.streams: dict[tuple[str, str, str], StreamState] = {}

    def accept(self, event: Received, now: float) -> list[Received]:
        key = (event.source_id, event.device_id, event.batch_id)
        state = self.streams.setdefault(key, StreamState())
        if event.sequence_number < state.expected:
            late = any(start <= event.sequence_number <= end for start, end in state.closed_gaps)
            return [replace(event, sequence_status="LATE_AFTER_GAP" if late else "REPEATED_SEQUENCE")]
        state.held.setdefault(event.sequence_number, []).append(event)
        return self._release(state, now)

    def tick(self, now: float) -> list[Received]:
        released = []
        for state in self.streams.values():
            released.extend(self._release(state, now))
        return released

    def _release(self, state: StreamState, now: float) -> list[Received]:
        released: list[Received] = []
        gap: tuple[int, ...] = ()
        while state.held:
            if state.expected in state.held:
                events = state.held.pop(state.expected)
                released.append(replace(events[0], gap_before=gap))
                released.extend(replace(item, sequence_status="REPEATED_SEQUENCE") for item in events[1:])
                gap = ()
                state.expected += 1
                state.gap_deadline = None
                continue
            if state.gap_deadline is None:
                state.gap_deadline = now + self.gap_seconds
                break
            if now < state.gap_deadline:
                break
            first = min(state.held)
            state.closed_gaps.append((state.expected, first - 1))
            gap = (state.expected, first - 1)
            state.expected = first
            state.gap_deadline = None
        return released
