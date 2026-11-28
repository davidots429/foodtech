import { parseCombiCandidate } from "./combiCandidate";
import type { CombiCandidate } from "./combiCandidate";
import type { CombiReview } from "./combiReview";
export type FreshCombiMeal = { equipment_size: CombiCandidate["equipment_size"]; rack_count: number; pan_count: number;
  dry_rice_kg: string; drained_rice_kg: string; added_water_kg: string; pan_tare_kg: string; rack_tare_kg: string;
  rice_recipe_version?: string; mackerel_recipe_version?: string };
export type CandidateMealInput = { request_id: string; meal_id: string; menu: CombiCandidate["menu"];
  expected_meal_version: number; proposal_digest: string; confirmed_pan_positions: { rack: number; shelf: number }[];
  new_meal: FreshCombiMeal; acknowledged: true };
const object = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);
const fail = (): never => { throw new Error("새 배치의 생성·출처를 확인하지 못했습니다. 같은 요청으로 확인·재시도해 주세요."); };
const same = (a: unknown, b: unknown): boolean => {
  if (object(a) && object(b)) return Object.keys(a).sort().join() === Object.keys(b).sort().join() && Object.keys(a).every(k => same(a[k], b[k]));
  if (Array.isArray(a) && Array.isArray(b)) return a.length === b.length && a.every((v, i) => same(v, b[i]));
  return a === b;
};
export function parseCandidateMealReceipt(raw: unknown, candidate: CombiCandidate, review: CombiReview, input: CandidateMealInput) {
  const fields = ["schema_version", "request_id", "meal_id", "request_hash", "created", "meal"];
  if (!object(raw) || Object.keys(raw).sort().join() !== fields.sort().join() ||
    raw.schema_version !== "combi-candidate-meal-receipt-v1" || raw.request_id !== input.request_id || raw.meal_id !== input.request_id ||
    typeof raw.created !== "boolean" || typeof raw.request_hash !== "string" || !/^[a-f0-9]{64}$/.test(raw.request_hash) ||
    !object(raw.meal) || !object(raw.meal.meal)) return fail();
  if (input.meal_id !== candidate.meal_id || input.menu !== candidate.menu || input.expected_meal_version !== candidate.job_version ||
    input.proposal_digest !== candidate.proposal_digest || input.acknowledged !== true) return fail();
  const snapshot = raw.meal, meta = raw.meal.meal, selection = meta.recipe_candidate_selection;
  const ids = [meta.meal_batch_id, meta.rice_cooking_job_id, meta.mode_transition_id, meta.mackerel_cooking_job_id];
  if (new Set(ids).size !== 4 || ids.some(id => typeof id !== "string" || !/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/.test(id)) ||
    ids.includes(candidate.meal_id) || ids.includes(candidate.job_id)) return fail();
  const otherMenu = candidate.menu === "RICE" ? "mackerel_recipe_version" : "rice_recipe_version";
  if (meta[otherMenu] !== input.new_meal[otherMenu]) return fail();
  if (meta.meal_batch_id !== input.request_id || meta.provenance !== "SYNTHETIC" || !Number.isSafeInteger(meta.version) ||
    (meta.version as number) < 1 || meta.equipment_size !== input.new_meal.equipment_size || !object(selection) ||
    selection.schema_version !== "combi-candidate-selection-v1" || selection.request_id !== input.request_id ||
    selection.request_hash !== raw.request_hash || selection.source_meal_id !== candidate.meal_id ||
    selection.source_meal_version !== candidate.job_version || selection.source_job_id !== candidate.job_id ||
    selection.menu !== candidate.menu || selection.review_version !== candidate.review_version || !same(selection.review, review)) return fail();
  const scope = { executionId: candidate.execution_id, mealId: candidate.meal_id, jobId: candidate.job_id, menu: candidate.menu };
  const checked = parseCombiCandidate(selection.candidate, scope, review);
  if (!same(checked, candidate) || meta[candidate.menu.toLowerCase() + "_recipe_version"] !== candidate.candidate_id ||
    meta.meal_batch_id === candidate.meal_id) return fail();
  for (const menu of ["RICE", "MACKEREL"] as const) {
    const step = snapshot[menu.toLowerCase()];
    if (!object(step) || step.job_id !== meta[menu.toLowerCase() + "_cooking_job_id"] || step.meal_batch_id !== input.request_id || step.menu !== menu) return fail();
  }
  if (!object(snapshot.transition) || snapshot.transition.transition_id !== meta.mode_transition_id ||
    snapshot.transition.meal_batch_id !== input.request_id) return fail();
  const step = snapshot[candidate.menu.toLowerCase()] as Record<string, unknown>, data = candidate.proposal.data;
  if (step.program_celsius !== data.temperature_c || step.climate_setpoint_percent !== data.climate_setpoint_percent ||
    step.fan_level !== data.fan_level || step.target_program_seconds !== data.duration_seconds ||
    !same(step.planned_pan_positions, data.pan_positions)) return fail();
  const rice = snapshot.rice as Record<string, unknown>;
  for (const name of ["dry_rice_kg", "drained_rice_kg", "added_water_kg", "pan_tare_kg", "rack_tare_kg"] as const) {
    if (typeof rice[name] !== "string" || !Number.isFinite(Number(rice[name])) || Number(rice[name]) !== Number(input.new_meal[name])) return fail();
  }
  if (rice.rack_count !== input.new_meal.rack_count || rice.pan_count !== input.new_meal.pan_count) return fail();
  if (candidate.menu === "RICE" && candidate.proposal.menu === "RICE" &&
    (step.target_effective_water_ratio !== candidate.proposal.data.effective_water_ratio ||
     step.target_soaking_seconds !== candidate.proposal.data.soak_seconds || step.target_draining_seconds !== candidate.proposal.data.drain_seconds)) return fail();
  if (candidate.menu === "MACKEREL" && candidate.proposal.menu === "MACKEREL" &&
    snapshot.transition.target_duration_seconds !== candidate.proposal.data.transition_duration_seconds) return fail();
  return { meal_id: input.request_id, request_hash: raw.request_hash, created: raw.created };
}
