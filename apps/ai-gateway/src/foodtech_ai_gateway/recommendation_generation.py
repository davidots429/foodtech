"""Internal structured generation; callers must authorize and account for each call.

There is deliberately no public endpoint here. Evidence, baselines and context
must come from trusted, tenant-scoped records, not from a model or browser.
"""
import json
from collections.abc import Awaitable, Callable

from foodtech_contracts.combi_recommendation import CombiBaseline, fields as combi_fields
from foodtech_contracts.recommendation_guard import Evidence
from foodtech_contracts.recommendation_output import JeyukBaseline, TonkatsuBaseline, validate

MAX_ATTEMPTS = 3
_SYSTEM = ("합성 조리 권고 초안을 JSON으로만 작성하세요. 누락된 근거를 추정하지 마세요. "
           "제안은 운영자 검토 대상이며 안전 승인이나 장비 제어가 아닙니다.")
_FIELDS = {
    TonkatsuBaseline: {"temperature_c": "number", "duration_seconds": "integer"},
    JeyukBaseline: {"temperature_c": "number", "rpm": "number", "duration_seconds": "integer",
                    "load_kg": "number", "vegetable_interval_seconds": "integer",
                    "sauce_interval_seconds": "integer"},
}


async def generate_candidate(
    invoke: Callable[[list[dict], dict], Awaitable[str]], *, evidence: Evidence,
    baseline: TonkatsuBaseline | JeyukBaseline | CombiBaseline, context: dict,
) -> dict:
    """Validate evidence and each response; retry malformed responses at most twice.

    The invoking service must enforce feature/tier consent and reserve usage for
    every provider request. A rejected or indeterminate candidate is never retried.
    """
    fields = combi_fields(baseline.menu) if type(baseline) is CombiBaseline else _FIELDS.get(type(baseline))
    if fields is None or not isinstance(evidence, Evidence) or not isinstance(context, dict):
        raise ValueError("trusted recommendation inputs required")
    # Probe the same guard with syntactically valid placeholders. Only its
    # indeterminate result is used; rejection of placeholders is irrelevant.
    probe = {name: ([{'rack': 1, 'shelf': 1}] if name == 'pan_positions' else 1) for name in fields}
    check = validate(json.dumps(probe), evidence, baseline)
    if check["status"] in {"INDETERMINATE", "FAILED"}:
        return {**check, "attempts": 0}
    encoded = json.dumps(context, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > 8192:
        raise ValueError("recommendation context too large")
    if type(baseline) is CombiBaseline:
        fields['pan_positions']['minItems'] = baseline.pan_count
        fields['pan_positions']['maxItems'] = baseline.pan_count
        fields['pan_positions']['items']['properties']['rack']['maximum'] = baseline.rack_count
        if baseline.menu == 'MACKEREL' and baseline.humidity_available is False:
            fields['transition_duration_seconds']['minimum'] = 180
    schema = {"type": "object", "properties": {name: ({"type": kind} if isinstance(kind, str) else kind) for name, kind in fields.items()},
              "required": list(fields), "additionalProperties": False}
    messages = [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": encoded}]
    for attempt in range(1, MAX_ATTEMPTS + 1):
        result = validate(await invoke(messages, schema), evidence, baseline)
        if result.get("error_code") != "STRUCTURED_OUTPUT_INVALID":
            return {**result, "attempts": attempt}
    return {**result, "attempts": MAX_ATTEMPTS}
