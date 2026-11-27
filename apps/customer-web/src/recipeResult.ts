export type RecipeState = "PENDING" | "RESERVED" | "SUCCEEDED" | "FAILED";
export type RecipeStatus = { execution_id: string; job_id: string; job_version: number; state: RecipeState;
  result_available: boolean; execution_available: false; job_changed: boolean; manual_invocation_enabled: boolean };
export type RecipeProposal = { status: "NEEDS_REVIEW"; schema_version: string; guard_version: string;
  menu: "TONKATSU" | "JEYUK"; data: Record<string, string | number> };
export type RecipeResult = RecipeStatus & { projection_status: "NOT_READY" | "AVAILABLE" | "FAILED" | "UNAVAILABLE";
  proposal: RecipeProposal | null; error_code: string | null };

const states = new Set(["PENDING", "RESERVED", "SUCCEEDED", "FAILED"]);
const tonkatsu = ["temperature_c", "duration_seconds"];
const jeyuk = [...tonkatsu, "rpm", "load_kg", "vegetable_interval_seconds", "sauce_interval_seconds"];
const record = (value: unknown): value is Record<string, unknown> => typeof value === "object" && value !== null && !Array.isArray(value);
const fail = (): never => { throw new Error("권고 결과의 형식을 확인하지 못했습니다. 다시 조회해 주세요."); };

export function parseRecipeStatus(raw: unknown, executionId: string, jobId: string): RecipeStatus {
  if (!record(raw) || raw.execution_id !== executionId || raw.job_id !== jobId ||
    !Number.isSafeInteger(raw.job_version) || (raw.job_version as number) < 1 ||
    typeof raw.state !== "string" || !states.has(raw.state) || typeof raw.result_available !== "boolean" ||
    raw.execution_available !== false || typeof raw.job_changed !== "boolean" ||
    raw.result_available !== ["SUCCEEDED", "FAILED"].includes(raw.state) ||
    (raw.manual_invocation_enabled !== undefined && typeof raw.manual_invocation_enabled !== "boolean")) return fail();
  return { execution_id: executionId, job_id: jobId, job_version: raw.job_version as number,
    state: raw.state as RecipeState, result_available: raw.result_available, execution_available: false,
    job_changed: raw.job_changed, manual_invocation_enabled: raw.manual_invocation_enabled === true };
}

export function parseRecipeResult(raw: unknown, executionId: string, jobId: string): RecipeResult {
  const base = parseRecipeStatus(raw, executionId, jobId);
  if (!record(raw)) return fail();
  const status = raw.projection_status;
  if (status === "AVAILABLE") {
    const p = raw.proposal;
    if (base.state !== "SUCCEEDED" || raw.error_code !== null || !record(p) ||
      Object.keys(p).sort().join() !== ["status", "schema_version", "guard_version", "menu", "data"].sort().join() ||
      p.status !== "NEEDS_REVIEW" || p.schema_version !== "synthetic-recommendation-output-v1" ||
      p.guard_version !== "synthetic-recommendation-guard-v1" || !["TONKATSU", "JEYUK"].includes(p.menu as string) || !record(p.data)) return fail();
    const fields = p.menu === "JEYUK" ? jeyuk : tonkatsu;
    if (Object.keys(p.data).sort().join() !== [...fields].sort().join()) return fail();
    const data: Record<string, string | number> = {};
    for (const field of fields) {
      const value = p.data[field];
      if (["duration_seconds", "vegetable_interval_seconds", "sauce_interval_seconds"].includes(field)) {
        if (!Number.isSafeInteger(value) || (value as number) < 0 || (value as number) >= 2 ** 31) return fail();
        data[field] = value as number;
      } else {
        if (typeof value !== "string" || value.length > 64 || !/^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:E[+-]?[0-9]+)?$/.test(value) ||
          !Number.isFinite(Number(value)) || Number(value) < 0) return fail();
        data[field] = value;
      }
    }
    return { ...base, projection_status: status, error_code: null,
      proposal: { status: "NEEDS_REVIEW", schema_version: p.schema_version, guard_version: p.guard_version,
        menu: p.menu as "TONKATSU" | "JEYUK", data } };
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

export function canInvokeRecipe(status: RecipeStatus | null, executionId: string, jobId: string, menu: string): boolean {
  return status !== null && status.execution_id === executionId && status.job_id === jobId && menu === "JEYUK" &&
    status.manual_invocation_enabled && status.state === "PENDING" && !status.job_changed;
}
