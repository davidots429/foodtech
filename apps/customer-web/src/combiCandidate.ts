import { parseCombiReview } from "./combiReview";
import type { CombiReview } from "./combiReview";
import type { CombiProposal, CombiScope } from "./combiRecipeResult";
export type CombiCandidate = { schema_version: "combi-recipe-candidate-v1"; candidate_id: string;
  status: "CANDIDATE_NOT_APPLIED"; provenance: "SYNTHETIC"; base_recipe_version: string | null;
  equipment_size: "MEDIUM_FIXED_RACK_10" | "LARGE_ROLL_IN_RACK_20"; meal_id: string; menu: "RICE" | "MACKEREL";
  job_id: string; job_version: number; execution_id: string; review_version: number; review_request_id: string;
  proposal_digest: string; proposal: CombiProposal; reviewed_at: string; recorded_at: string };
const object = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);
const integer = (v: unknown): v is number => Number.isSafeInteger(v) && (v as number) >= 1;
const fail = (): never => { throw new Error("콤비 후보 버전을 확인하지 못했습니다. 같은 검토 버전의 후보를 다시 조회해 주세요."); };
export function parseCombiCandidate(raw: unknown, scope: CombiScope, review: CombiReview): CombiCandidate {
  const checked = parseCombiReview(review, scope);
  const fields = ["schema_version", "candidate_id", "status", "provenance", "base_recipe_version", "equipment_size",
    "meal_id", "menu", "job_id", "job_version", "execution_id", "review_version", "review_request_id",
    "proposal_digest", "proposal", "reviewed_at", "recorded_at"];
  if (!object(raw) || Object.keys(raw).sort().join() !== fields.sort().join() || checked.decision !== "APPROVE" ||
    raw.schema_version !== "combi-recipe-candidate-v1" || raw.status !== "CANDIDATE_NOT_APPLIED" || raw.provenance !== "SYNTHETIC" ||
    raw.candidate_id !== `combi-${checked.meal_id}-${checked.menu.toLowerCase()}-r${checked.version}` ||
    raw.meal_id !== checked.meal_id || raw.menu !== checked.menu || raw.job_id !== checked.job_id || raw.job_version !== checked.job_version ||
    raw.execution_id !== checked.execution_id || raw.review_version !== checked.version || raw.review_request_id !== checked.request_id ||
    raw.proposal_digest !== checked.proposal_digest || raw.reviewed_at !== checked.reviewed_at ||
    !["MEDIUM_FIXED_RACK_10", "LARGE_ROLL_IN_RACK_20"].includes(raw.equipment_size as string) ||
    (raw.base_recipe_version !== null && (typeof raw.base_recipe_version !== "string" || !raw.base_recipe_version.trim() ||
      raw.base_recipe_version !== raw.base_recipe_version.trim() || raw.base_recipe_version.length > 80)) ||
    typeof raw.recorded_at !== "string" || !/(?:Z|\+00:00)$/.test(raw.recorded_at) || !Number.isFinite(Date.parse(raw.recorded_at)) ||
    Date.parse(raw.recorded_at) < Date.parse(checked.reviewed_at)) return fail();
  const proposal = parseCombiReview({ ...checked, proposal: raw.proposal }, scope).proposal;
  if (JSON.stringify(proposal) !== JSON.stringify(checked.proposal)) return fail();
  return { schema_version: "combi-recipe-candidate-v1", candidate_id: raw.candidate_id as string, status: "CANDIDATE_NOT_APPLIED",
    provenance: "SYNTHETIC", base_recipe_version: raw.base_recipe_version as string | null,
    equipment_size: raw.equipment_size as CombiCandidate["equipment_size"], meal_id: checked.meal_id, menu: checked.menu,
    job_id: checked.job_id, job_version: checked.job_version, execution_id: checked.execution_id, review_version: checked.version,
    review_request_id: checked.request_id, proposal_digest: checked.proposal_digest, proposal,
    reviewed_at: checked.reviewed_at, recorded_at: raw.recorded_at };
}
export function parseCombiCandidateReceipt(raw: unknown, scope: CombiScope, review: CombiReview) {
  if (!object(raw) || Object.keys(raw).sort().join() !== ["adoption_available", "candidate", "current_meal_version", "current_review_version"].join() ||
    typeof raw.adoption_available !== "boolean" || !integer(raw.current_meal_version) || !integer(raw.current_review_version) ||
    raw.current_review_version < review.version || (raw.adoption_available &&
      (raw.current_meal_version !== review.job_version || raw.current_review_version !== review.version))) return fail();
  return { candidate: parseCombiCandidate(raw.candidate, scope, review), current_meal_version: raw.current_meal_version,
    current_review_version: raw.current_review_version, adoption_available: raw.adoption_available as boolean };
}
