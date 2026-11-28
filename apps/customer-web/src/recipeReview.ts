import { parseRecipeResult } from "./recipeResult";
import type { RecipeProposal } from "./recipeResult";

export const fryerSourceContract = "tonkatsu-guarded-source-v1" as const;

export type RecipeReview = { schema_version: "recipe-review-v1"; version: number; request_id: string;
  execution_id: string; job_id: string; job_version: number; proposal_digest: string; proposal: RecipeProposal;
  decision: "APPROVE" | "REJECT"; reason: string; reviewed_at: string; source_contract?: typeof fryerSourceContract };
export type ReviewContext = { execution_id: string; job_id: string; job_version: number; proposal_digest: string;
  proposal: RecipeProposal; review_version: number; can_review: boolean; latest_review: RecipeReview | null };
const object = (x: unknown): x is Record<string, unknown> => typeof x === "object" && x !== null && !Array.isArray(x);
const integer = (x: unknown, minimum = 1): x is number => Number.isSafeInteger(x) && (x as number) >= minimum;
const uuid = (x: unknown): x is string => typeof x === "string" && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(x);
const digest = (x: unknown): x is string => typeof x === "string" && /^[0-9a-f]{64}$/.test(x);
const fail = (): never => { throw new Error("검토 기록의 형식을 확인하지 못했습니다. 다시 조회해 주세요."); };

function proposal(raw: unknown, executionId: string, jobId: string): RecipeProposal {
  return parseRecipeResult({ execution_id: executionId, job_id: jobId, job_version: 1,
    state: "SUCCEEDED", result_available: true, execution_available: false, job_changed: false,
    projection_status: "AVAILABLE", error_code: null, proposal: raw }, executionId, jobId).proposal!;
}

export function parseRecipeReview(raw: unknown, jobId: string): RecipeReview {
  if (!object(raw) || Object.keys(raw).sort().join() !== ["schema_version", "version", "request_id", "execution_id",
     "job_id", "job_version", "proposal_digest", "proposal", "decision", "reason", "reviewed_at",
    ...(raw.source_contract === undefined ? [] : ["source_contract"])].sort().join() ||
    (raw.source_contract !== undefined && raw.source_contract !== fryerSourceContract) ||
    raw.schema_version !== "recipe-review-v1" || raw.job_id !== jobId || !uuid(raw.request_id) || !uuid(raw.execution_id) ||
    !integer(raw.version) || !integer(raw.job_version) || !digest(raw.proposal_digest) ||
    (raw.decision !== "APPROVE" && raw.decision !== "REJECT") || typeof raw.reason !== "string" ||
    !raw.reason.trim() || raw.reason !== raw.reason.trim() || raw.reason.length > 1000 ||
    typeof raw.reviewed_at !== "string" || !/(?:Z|\+00:00)$/.test(raw.reviewed_at) || !Number.isFinite(Date.parse(raw.reviewed_at))) return fail();
  const checked = proposal(raw.proposal, raw.execution_id, jobId);
  if (raw.source_contract !== undefined && checked.menu !== "TONKATSU") return fail();
  return { schema_version: "recipe-review-v1", version: raw.version, request_id: raw.request_id,
    execution_id: raw.execution_id, job_id: jobId, job_version: raw.job_version, proposal_digest: raw.proposal_digest,
    proposal: checked, decision: raw.decision,
    reason: raw.reason, reviewed_at: raw.reviewed_at,
    ...(raw.source_contract === fryerSourceContract ? { source_contract: fryerSourceContract } : {}) };
}

export function parseReviewContext(raw: unknown, executionId: string, jobId: string,
  jobVersion: number, displayed: RecipeProposal): ReviewContext {
  if (!object(raw) || raw.execution_id !== executionId || raw.job_id !== jobId || raw.job_version !== jobVersion ||
    !digest(raw.proposal_digest) || !integer(raw.review_version, 0) || typeof raw.can_review !== "boolean") return fail();
  const value = proposal(raw.proposal, executionId, jobId);
  if (JSON.stringify(value) !== JSON.stringify(displayed)) return fail();
  const latest = raw.latest_review === null ? null : parseRecipeReview(raw.latest_review, jobId);
  if ((raw.review_version === 0) !== (latest === null) || (latest !== null && latest.version !== raw.review_version)) return fail();
  return { execution_id: executionId, job_id: jobId, job_version: jobVersion, proposal_digest: raw.proposal_digest,
    proposal: value, review_version: raw.review_version, can_review: raw.can_review, latest_review: latest };
}

export function parseReviewPage(raw: unknown, jobId: string, cursor: number | null) {
  if (!object(raw) || raw.job_id !== jobId || !integer(raw.current_job_version) || !Array.isArray(raw.items) ||
    raw.items.length > 50 || (raw.next_cursor !== null && !integer(raw.next_cursor))) return fail();
  const items = raw.items.map(value => parseRecipeReview(value, jobId));
  let previous = cursor ?? 0;
  for (const item of items) { if (item.version <= previous) return fail(); previous = item.version; }
  if (raw.next_cursor !== null && (!items.length || raw.next_cursor !== previous)) return fail();
  return { items, next_cursor: raw.next_cursor as number | null, current_job_version: raw.current_job_version };
}
