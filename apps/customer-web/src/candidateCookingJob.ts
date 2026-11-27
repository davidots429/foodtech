import { parseRecipeCandidate } from "./recipeCandidate";
import type { RecipeCandidate } from "./recipeCandidate";
import type { RecipeReview } from "./recipeReview";

export type CandidateJobInput = { request_id: string; expected_job_version: number; proposal_digest: string;
  planned_portions: number; thickness_cm: string | null; actual_load_kg: string | null; acknowledged: true };
const object = (x: unknown): x is Record<string, unknown> => typeof x === "object" && x !== null && !Array.isArray(x);
const fail = (): never => { throw new Error("생성된 작업의 연결을 확인하지 못했습니다."); };
const hash = (x: unknown): x is string => typeof x === "string" && /^[0-9a-f]{64}$/.test(x);
export function parseCandidateJobReceipt(raw: unknown, candidate: RecipeCandidate, input: CandidateJobInput) {
  if (!object(raw) || raw.job_id !== input.request_id || raw.recipe_version !== candidate.candidate_id ||
    !hash(raw.request_hash) || typeof raw.created !== "boolean") return fail();
  return { job_id: input.request_id, request_hash: raw.request_hash, created: raw.created };
}
export function parseCandidateJobRecovery(raw: unknown, candidate: RecipeCandidate, review: RecipeReview, input: CandidateJobInput) {
  if (!object(raw) || !object(raw.job)) return fail();
  const job = raw.job, selection = job.recipe_candidate_selection;
  if (job.job_id !== input.request_id || job.recipe_version !== candidate.candidate_id || job.provenance !== "SYNTHETIC" ||
    job.menu !== candidate.proposal.menu || job.equipment_profile !== candidate.equipment_profile ||
    job.planned_portions !== input.planned_portions || job.source_job_id !== null || job.recook_reason !== null || !object(selection) ||
    selection.schema_version !== "recipe-candidate-selection-v1" || selection.request_id !== input.request_id ||
    !hash(selection.request_hash) || selection.source_job_id !== candidate.job_id ||
    selection.source_job_version !== candidate.job_version || selection.review_version !== candidate.review_version) return fail();
  const parsed = parseRecipeCandidate({ candidate: selection.candidate,
    current_job_version: candidate.job_version, current_review_version: candidate.review_version }, review);
  if (JSON.stringify(parsed.candidate) !== JSON.stringify(candidate)) return fail();
  for (const field of ["thickness_cm", "actual_load_kg"] as const) {
    const expected = input[field], value = job[field];
    if (expected === null ? value !== null : typeof value !== "string" || !Number.isFinite(Number(value)) || Number(value) !== Number(expected)) return fail();
  }
  return { job_id: input.request_id, request_hash: selection.request_hash, created: false };
}
