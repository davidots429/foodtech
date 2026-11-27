import { parseRecipeStatus } from "./recipeResult";
import type { RecipeStatus } from "./recipeResult";

export type CombiMenu = "RICE" | "MACKEREL";
export type CombiScope = { executionId: string; mealId: string; jobId: string; menu: CombiMenu };
export type CombiStatus = RecipeStatus & { meal_id: string; menu: CombiMenu; manual_invocation_enabled: false };
export type CombiProposal = { status: "NEEDS_REVIEW"; schema_version: string; guard_version: string; menu: CombiMenu;
  data: { temperature_c: string; duration_seconds: number; climate_setpoint_percent: string; fan_level: number;
    pan_positions: { rack: number; shelf: number }[]; effective_water_ratio?: string;
    soak_seconds?: number; drain_seconds?: number; transition_duration_seconds?: number } };
export type CombiResult = CombiStatus & { projection_status: "NOT_READY" | "AVAILABLE" | "FAILED" | "UNAVAILABLE";
  proposal: CombiProposal | null; error_code: string | null };

const record = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);
const fail = (): never => { throw new Error("콤비 권고 기록의 형식을 확인하지 못했습니다. 다시 조회해 주세요."); };
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const sameKeys = (value: Record<string, unknown>, keys: string[]) => Object.keys(value).sort().join() === [...keys].sort().join();
const integer = (value: unknown): value is number => Number.isSafeInteger(value) && (value as number) >= 0 && (value as number) < 2 ** 31;
const decimal = (value: unknown): value is string => typeof value === "string" && value.length <= 64 &&
  /^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:E[+-]?[0-9]+)?$/.test(value) && Number.isFinite(Number(value)) && Number(value) >= 0;

function requireScope(scope: CombiScope) {
  if (![scope.executionId, scope.mealId, scope.jobId].every(id => typeof id === "string" && uuid.test(id)) ||
    !["RICE", "MACKEREL"].includes(scope.menu)) return fail();
}

export function combiRecordPath(scope: CombiScope, view: "status" | "result"): string {
  requireScope(scope);
  if (view !== "status" && view !== "result") return fail();
  return `/api/customer/ai/combi/recipe-executions/${encodeURIComponent(scope.executionId)}/${view}` +
    `?meal_id=${encodeURIComponent(scope.mealId)}&menu=${scope.menu}`;
}

export function parseCombiStatus(raw: unknown, scope: CombiScope): CombiStatus {
  requireScope(scope);
  if (!record(raw) || raw.meal_id !== scope.mealId || raw.menu !== scope.menu || raw.manual_invocation_enabled !== false) return fail();
  return { ...parseRecipeStatus(raw, scope.executionId, scope.jobId), meal_id: scope.mealId,
    menu: scope.menu, manual_invocation_enabled: false };
}

export function parseCombiResult(raw: unknown, scope: CombiScope): CombiResult {
  const base = parseCombiStatus(raw, scope);
  if (!record(raw)) return fail();
  const status = raw.projection_status;
  if (status === "AVAILABLE") {
    const p = raw.proposal;
    if (base.state !== "SUCCEEDED" || raw.error_code !== null || !record(p) ||
      !sameKeys(p, ["status", "schema_version", "guard_version", "menu", "data"]) || p.status !== "NEEDS_REVIEW" ||
      p.schema_version !== "synthetic-combi-recommendation-output-v1" ||
      p.guard_version !== "synthetic-combi-recommendation-guard-v1" || p.menu !== scope.menu || !record(p.data)) return fail();
    const d = p.data;
    const fields = ["temperature_c", "duration_seconds", "climate_setpoint_percent", "fan_level", "pan_positions",
      ...(scope.menu === "RICE" ? ["effective_water_ratio", "soak_seconds", "drain_seconds"] : ["transition_duration_seconds"])];
    if (!sameKeys(d, fields) || !decimal(d.temperature_c) || !decimal(d.climate_setpoint_percent) ||
      !integer(d.duration_seconds) || !integer(d.fan_level) || !Array.isArray(d.pan_positions) ||
      d.pan_positions.length < 1 || d.pan_positions.length > 20) return fail();
    const positions = d.pan_positions.map((p: unknown) => {
      if (!record(p) || !sameKeys(p, ["rack", "shelf"]) || !Number.isSafeInteger(p.rack) || !Number.isSafeInteger(p.shelf) ||
        (p.rack as number) < 1 || (p.rack as number) > 2 || (p.shelf as number) < 1 || (p.shelf as number) > 10) return fail();
      return { rack: p.rack as number, shelf: p.shelf as number };
    });
    if (new Set(positions.map(p => `${p.rack}:${p.shelf}`)).size !== positions.length) return fail();
    const data: CombiProposal["data"] = { temperature_c: d.temperature_c, duration_seconds: d.duration_seconds,
      climate_setpoint_percent: d.climate_setpoint_percent, fan_level: d.fan_level, pan_positions: positions };
    if (scope.menu === "RICE") {
      if (!decimal(d.effective_water_ratio) || !integer(d.soak_seconds) || !integer(d.drain_seconds)) return fail();
      data.effective_water_ratio = d.effective_water_ratio; data.soak_seconds = d.soak_seconds; data.drain_seconds = d.drain_seconds;
    } else {
      if (!integer(d.transition_duration_seconds)) return fail();
      data.transition_duration_seconds = d.transition_duration_seconds;
    }
    return { ...base, projection_status: "AVAILABLE", error_code: null,
      proposal: { status: "NEEDS_REVIEW", schema_version: p.schema_version, guard_version: p.guard_version, menu: scope.menu, data } };
  }
  if (raw.proposal !== null) return fail();
  if (status === "NOT_READY" && ["PENDING", "RESERVED"].includes(base.state) && raw.error_code === null)
    return { ...base, projection_status: status, proposal: null, error_code: null };
  if (status === "FAILED" && base.state === "FAILED" && raw.error_code === "RECIPE_EXECUTION_FAILED")
    return { ...base, projection_status: status, proposal: null, error_code: raw.error_code };
  if (status === "UNAVAILABLE" && base.state === "SUCCEEDED" && raw.error_code === "RECIPE_RESULT_UNAVAILABLE")
    return { ...base, projection_status: status, proposal: null, error_code: raw.error_code };
  return fail();
}

export async function readCombiRecord(scope: CombiScope, read: (path: string) => Promise<unknown>,
    isCurrent: () => boolean = () => true): Promise<{ status: CombiStatus; result: CombiResult | null } | null> {
  if (!isCurrent()) return null;
  const observed = parseCombiStatus(await read(combiRecordPath(scope, "status")), scope);
  if (!isCurrent()) return null;
  const result = observed.result_available ? parseCombiResult(await read(combiRecordPath(scope, "result")), scope) : null;
  if (!isCurrent()) return null;
  return { status: result ?? observed, result };
}
