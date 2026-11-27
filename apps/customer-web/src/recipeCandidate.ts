import { parseRecipeReview } from "./recipeReview";
import type { RecipeReview } from "./recipeReview";

export type RecipeCandidate = { schema_version: "recipe-candidate-v1"; candidate_id: string;
  status: "CANDIDATE_NOT_APPLIED"; provenance: "SYNTHETIC"; base_recipe_version: string;
  equipment_profile: string; review_version: number; review_request_id: string; execution_id: string;
  job_id: string; job_version: number; proposal_digest: string; proposal: RecipeReview["proposal"]; created_at: string };
const object = (x: unknown): x is Record<string, unknown> => typeof x === "object" && x !== null && !Array.isArray(x);
const integer = (x: unknown): x is number => Number.isSafeInteger(x) && (x as number) >= 1;
const fail = (): never => { throw new Error("후보 버전을 확인하지 못했습니다. 다시 조회해 주세요."); };

export function parseRecipeCandidate(raw: unknown, review: RecipeReview) {
  if (!object(raw) || !object(raw.candidate) || !integer(raw.current_job_version) ||
    !integer(raw.current_review_version) || raw.current_job_version < review.job_version ||
    raw.current_review_version < review.version || review.decision !== "APPROVE") return fail();
  const c = raw.candidate;
  const keys = ["schema_version", "candidate_id", "status", "provenance", "base_recipe_version", "equipment_profile",
    "review_version", "review_request_id", "execution_id", "job_id", "job_version", "proposal_digest", "proposal", "created_at"];
  if (Object.keys(c).sort().join() !== keys.sort().join() || c.schema_version !== "recipe-candidate-v1" ||
    c.candidate_id !== `recipe-candidate-${review.job_id}-r${review.version}` || c.status !== "CANDIDATE_NOT_APPLIED" ||
    c.provenance !== "SYNTHETIC" || typeof c.base_recipe_version !== "string" || !c.base_recipe_version.trim() ||
    c.base_recipe_version !== c.base_recipe_version.trim() || c.base_recipe_version.length > 80 ||
    typeof c.equipment_profile !== "string" || !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$/.test(c.equipment_profile) ||
    c.review_version !== review.version || c.review_request_id !== review.request_id || c.execution_id !== review.execution_id ||
    c.job_id !== review.job_id || c.job_version !== review.job_version || c.proposal_digest !== review.proposal_digest ||
    c.created_at !== review.reviewed_at) return fail();
  const checked = parseRecipeReview({ ...review, proposal: c.proposal }, review.job_id);
  if (JSON.stringify(checked.proposal) !== JSON.stringify(review.proposal)) return fail();
  return { candidate: { schema_version: "recipe-candidate-v1", candidate_id: c.candidate_id,
    status: "CANDIDATE_NOT_APPLIED", provenance: "SYNTHETIC", base_recipe_version: c.base_recipe_version,
    equipment_profile: c.equipment_profile, review_version: review.version, review_request_id: review.request_id,
    execution_id: review.execution_id, job_id: review.job_id, job_version: review.job_version,
    proposal_digest: review.proposal_digest, proposal: checked.proposal, created_at: review.reviewed_at } as RecipeCandidate,
    current_job_version: raw.current_job_version, current_review_version: raw.current_review_version };
}
