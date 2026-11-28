"""Read the current quality revision from the same leased fryer job partition."""
from decimal import Decimal, InvalidOperation

from foodtech_contracts.combi_history_witness import history_integer
from foodtech_contracts.quality_score import summarize_quality_scores
from foodtech_contracts.tonkatsu_observed_inputs import TonkatsuQualitySummary


_POINTER_KEY = "ASSESSMENT#CURRENT"
_POINTER_FIELDS = (
    "pk", "sk", "version", "safety_version", "usable", "blocked_reason",
    "assessment_sk", "quality_passed", "quality_target", "score_percent",
)
_REVISION_FIELDS = (
    "pk", "sk", "version", "safety_version", "job_state_version", "status", "menu",
    "equipment_profile", "scale_version", "formula_version", "scores", "score_total",
    "score_percent", "quality_status", "quality_passed", "quality_target",
    "quality_decision", "planned_portions", "actual_portions", "actual_mass_kg",
    "load_percent", "load_band", "tonkatsu_batch_rule_version",
)


def _scoped_read(table, pk, sk, fields):
    names = {f"#field{index}": field for index, field in enumerate(fields)}
    row = table.get_item(
        Key={"pk": pk, "sk": sk}, ConsistentRead=True,
        ProjectionExpression=", ".join(names), ExpressionAttributeNames=names,
    ).get("Item")
    if row is not None and (type(row) is not dict or row.get("pk") != pk or row.get("sk") != sk):
        raise ValueError("foreign fryer assessment record")
    return row


def _matches_decimal(value, expected):
    if type(value) not in (str, Decimal):
        return False
    try:
        number = Decimal(value)
        return number.is_finite() and number == expected
    except InvalidOperation:
        return False


def observe_tonkatsu_quality(cooking, job, batch):
    """Resolve an immutable quality row whose pointer is fenced by the job META version."""
    pk = job["pk"]
    safety = job.get("safety_status")
    if safety not in {"PASSED", "FAILED"} or batch.recorded_safety_status != safety:
        raise ValueError("recorded fryer safety unavailable")
    pointer = _scoped_read(cooking, pk, _POINTER_KEY, _POINTER_FIELDS)
    if safety == "FAILED":
        if job.get("evaluation_blocked") is not True or (pointer is not None and pointer.get("usable") is not False):
            raise ValueError("inconsistent evaluation block")
        return TonkatsuQualitySummary("BLOCKED_BY_SAFETY")
    if (job.get("status") != "COMPLETED" or job.get("evaluation_status") != "COMPLETED"
        or job.get("evaluation_blocked") is not False or not pointer
        or pointer.get("usable") is not True or pointer.get("blocked_reason") is not None
    ):
        raise ValueError("current fryer evaluation unavailable")

    version = history_integer(pointer.get("version"))
    safety_version = history_integer(job.get("safety_version"))
    sk = f"ASSESSMENT#REV#{version:012d}"
    if pointer.get("assessment_sk") != sk or history_integer(pointer.get("safety_version")) != safety_version:
        raise ValueError("fryer evaluation pointer is stale")
    assessment = _scoped_read(cooking, pk, sk, _REVISION_FIELDS)
    if (
        not assessment or history_integer(assessment.get("version")) != version
        or history_integer(assessment.get("safety_version")) != safety_version
        or history_integer(assessment.get("job_state_version")) != history_integer(job.get("job_state_version"))
        or assessment.get("status") != "COMPLETED" or assessment.get("menu") != "TONKATSU"
        or assessment.get("equipment_profile") != "synthetic-fryer-approved-v2"
        or assessment.get("scale_version") != "tonkatsu-common-v1" or assessment.get("formula_version") != "quality-v1"
        or assessment.get("quality_decision") is not None
    ):
        raise ValueError("fryer evaluation revision does not match its job")
    planned = history_integer(job.get("planned_portions"), 1, 1000)
    portions = history_integer(assessment.get("actual_portions"), 1, planned)
    facts = batch.batch_facts
    if (
        history_integer(assessment.get("planned_portions"), 1, 1000) != planned
        or not _matches_decimal(assessment.get("actual_mass_kg"), batch.load_kg)
        or not _matches_decimal(assessment.get("load_percent"), facts.load_percent)
        or assessment.get("load_band") != facts.batch_mode
        or assessment.get("tonkatsu_batch_rule_version") != facts.batch_rule_version
    ):
        raise ValueError("fryer evaluation load does not match its job")
    scores, total, percent = summarize_quality_scores(assessment.get("scores"))
    minimum = min(score for _, score in scores)
    passed = minimum >= 3 and percent >= 60
    target = passed and minimum >= 4 and percent >= 80
    status = "QUALITY_PASSED" if passed else "QUALITY_FAILED"
    if (
        history_integer(assessment.get("score_total"), 7, 35) != total
        or assessment.get("quality_status") != status
        or any(row.get("quality_passed") is not passed or row.get("quality_target") is not target
               or not _matches_decimal(row.get("score_percent"), percent) for row in (pointer, assessment))
    ):
        raise ValueError("fryer evaluation score does not match its revision")
    return TonkatsuQualitySummary("CURRENT", version=version, scores=scores, score_percent=percent,
                                  quality_status=status, quality_target=target, actual_portions=portions)
