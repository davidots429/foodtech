import { parseCombiResult } from "./combiRecipeResult";
import type { CombiProposal, CombiScope } from "./combiRecipeResult";
export type CombiReview = { schema_version: "combi-recipe-review-v1"; version: number; request_id: string;
  execution_id: string; meal_id: string; menu: "RICE" | "MACKEREL"; job_id: string; job_version: number;
  proposal_digest: string; proposal: CombiProposal; decision: "APPROVE" | "REJECT"; reason: string; reviewed_at: string };
export type CombiReviewContext = { review_version: number; latest_review: CombiReview | null; can_review: boolean;
  proposal_digest: string; proposal: CombiProposal };
const object = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);
const integer = (v: unknown, min = 1): v is number => Number.isSafeInteger(v) && (v as number) >= min;
const uuid = (v: unknown): v is string => typeof v === "string" && /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/.test(v);
const digest = (v: unknown): v is string => typeof v === "string" && /^[a-f0-9]{64}$/.test(v);
const fail = (): never => { throw new Error("콤비 검토 기록을 확인하지 못했습니다. 이력을 다시 조회해 주세요."); };
function proposal(value: unknown, scope: CombiScope): CombiProposal {
  return parseCombiResult({ execution_id: scope.executionId, meal_id: scope.mealId, job_id: scope.jobId, menu: scope.menu,
    job_version: 1, state: "SUCCEEDED", result_available: true, execution_available: false,
    manual_invocation_enabled: false, job_changed: false, projection_status: "AVAILABLE", error_code: null, proposal: value }, scope).proposal!;
}
export function parseCombiReview(value: unknown, scope: CombiScope): CombiReview {
  const fields = ["schema_version", "version", "request_id", "execution_id", "meal_id", "menu", "job_id", "job_version",
    "proposal_digest", "proposal", "decision", "reason", "reviewed_at"];
  if (!object(value) || Object.keys(value).sort().join() !== fields.sort().join() ||
    value.schema_version !== "combi-recipe-review-v1" || value.meal_id !== scope.mealId || value.menu !== scope.menu ||
    !uuid(value.request_id) || !uuid(value.execution_id) || !uuid(value.job_id) || !integer(value.version) ||
    !integer(value.job_version) || !digest(value.proposal_digest) || !["APPROVE", "REJECT"].includes(value.decision as string) ||
    typeof value.reason !== "string" || !value.reason.trim() || value.reason !== value.reason.trim() || value.reason.length > 1000 ||
    typeof value.reviewed_at !== "string" || !/(?:Z|\+00:00)$/.test(value.reviewed_at) || !Number.isFinite(Date.parse(value.reviewed_at))) return fail();
  return { schema_version: "combi-recipe-review-v1", version: value.version, request_id: value.request_id,
    execution_id: value.execution_id, meal_id: scope.mealId, menu: scope.menu, job_id: value.job_id, job_version: value.job_version,
    proposal_digest: value.proposal_digest, proposal: proposal(value.proposal, { ...scope, executionId: value.execution_id, jobId: value.job_id }),
    decision: value.decision as "APPROVE" | "REJECT", reason: value.reason, reviewed_at: value.reviewed_at };
}
export function parseCombiReviewContext(value: unknown, scope: CombiScope, version: number, displayed: CombiProposal): CombiReviewContext {
  if (!object(value) || value.execution_id !== scope.executionId || value.job_id !== scope.jobId ||
    value.meal_id !== scope.mealId || value.menu !== scope.menu || value.job_version !== version ||
    !digest(value.proposal_digest) || !integer(value.review_version, 0) || typeof value.can_review !== "boolean") return fail();
  const checked = proposal(value.proposal, scope);
  if (JSON.stringify(checked) !== JSON.stringify(displayed)) return fail();
  const latest = value.latest_review === null ? null : parseCombiReview(value.latest_review, scope);
  if ((value.review_version === 0) !== (latest === null) || (latest !== null && latest.version !== value.review_version)) return fail();
  return { review_version: value.review_version, latest_review: latest, can_review: value.can_review,
    proposal_digest: value.proposal_digest, proposal: checked };
}
export function parseCombiReviewPage(value: unknown, scope: CombiScope, cursor: number | null) {
  if (!object(value) || !Array.isArray(value.items) || value.items.length > 50 ||
    (value.next_cursor !== null && !integer(value.next_cursor))) return fail();
  const items = value.items.map(item => parseCombiReview(item, scope));
  let previous = cursor ?? 0;
  for (const item of items) { if (item.version <= previous) return fail(); previous = item.version; }
  if (value.next_cursor !== null && (!items.length || value.next_cursor !== previous)) return fail();
  return { items, next_cursor: value.next_cursor as number | null };
}

export function parseCombiReviewReceipt(value: unknown, scope: CombiScope, version: number,
    context: CombiReviewContext, request: { request_id: string; decision: "APPROVE" | "REJECT"; reason: string }): CombiReview {
  if (!object(value) || Object.keys(value).sort().join() !== ["adoption_available", "review"].join() || value.adoption_available !== false) return fail();
  const saved = parseCombiReview(value.review, scope);
  if (saved.execution_id !== scope.executionId || saved.job_id !== scope.jobId || saved.job_version !== version ||
    saved.request_id !== request.request_id || saved.version !== context.review_version + 1 ||
    saved.proposal_digest !== context.proposal_digest || saved.reason !== request.reason || saved.decision !== request.decision ||
    JSON.stringify(saved.proposal) !== JSON.stringify(context.proposal)) return fail();
  return saved;
}
