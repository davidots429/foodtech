export type JobStatus = "PENDING" | "READY" | "RUNNING" | "PAUSED" | "SAFETY_HOLD" | "COMPLETED" | "FAILED" | "CANCELED";
export type SafetyStatus = "NOT_MEASURED" | "PASSED" | "FAILED" | "INCOMPLETE" | "SUSPECTED" | "UNDER_REVIEW" | "CLEARED";
export type TransitionEvent = "PREHEAT_COMPLETED" | "PREHEAT_CONDITION_LOST" | "COOKING_STARTED" |
  "COOKING_PAUSED" | "COOKING_RESUMED" | "COOKING_COMPLETED" | "COOKING_CANCELED";
export type SafetyEvent = "SUSPECTED" | "UNDER_REVIEW" | "CLEARED" | "FAILED";
export type AssessmentKind = "INITIAL" | "REVISION" | "REASSESSMENT";

export const scoreItems = [
  { key: "ingredient_condition", label: "재료 상태", help: "투입 직전의 신선도와 상태" },
  { key: "preparation", label: "전처리·손질 적정성", help: "두께와 손질의 적정성" },
  { key: "taste", label: "맛", help: "완성 음식의 맛" },
  { key: "texture", label: "식감", help: "육질과 튀김옷의 식감" },
  { key: "appearance", label: "외관", help: "색과 형태의 균일성" },
  { key: "aroma", label: "향·이취", help: "향과 이상 냄새 여부" },
  { key: "temperature_suitability", label: "온도 적합성", help: "제공 시점의 온도 적합성" },
] as const;
export type ScoreKey = typeof scoreItems[number]["key"];

export type CookingJob = {
  job_id: string;
  menu: "TONKATSU";
  provenance: "SYNTHETIC";
  equipment_profile: string;
  recipe_version: string;
  planned_portions: number;
  thickness_cm?: string;
  thickness_profile?: "THIN" | "STANDARD" | "THICK" | null;
  status: JobStatus;
  version: number;
  safety_status: SafetyStatus;
  safety_version: number;
  evaluation_blocked: boolean;
  evaluation_status: "NOT_COMPLETED" | "INTERRUPTED" | "COMPLETED";
  safety_hold_requested_at?: string | null;
  manual_stop_confirmed_at?: string | null;
  manual_stopped_at?: string | null;
  source_job_id: string | null;
  recook_reason: string | null;
  source_binding: { source_id: string; device_id: string; batch_id: string } | null;
  process_assessment_status: "INSUFFICIENT_DATA" | "NORMAL" | "WARNING" | "FAILED";
  process_temperature_status: string;
  process_thickness_status?: string;
  process_time_status?: string;
  actual_placed_at?: string | null;
  actual_lifted_at?: string | null;
  actual_cooking_seconds?: number | null;
  updated_at: string;
};

export type CurrentAssessment = {
  version: number;
  usable: boolean;
  blocked_reason: string | null;
  quality_passed: boolean;
  quality_target: boolean;
  score_percent: string;
  assessment_sk: string;
};

export type Assessment = {
  version: number;
  previous_version: number | null;
  kind: AssessmentKind;
  reason: string | null;
  status: "COMPLETED";
  scores: Record<ScoreKey, number>;
  observations: Record<ScoreKey, string>;
  score_percent: string;
  quality_passed: boolean;
  quality_target: boolean;
  overall_satisfaction: number | null;
  actual_portions: number;
  actual_mass_kg: string;
  load_percent: string;
  load_band: "PARTIAL" | "REFERENCE";
  completed_at: string;
};

export type JobEvent = {
  sk: string;
  domain: string;
  trigger: string;
  from_status: string;
  to_status: string;
  reason?: string | null;
  occurred_at: string;
  manual_stop_confirmed_at?: string | null;
  manual_stopped_at?: string | null;
  partial_scores?: Partial<Record<ScoreKey, number>>;
  partial_observations?: Partial<Record<ScoreKey, string>>;
};

export type JobSnapshot = { job: CookingJob; current_assessment: CurrentAssessment | null };
export type CoreSample = { at_second: number; celsius: string };
export type Probe = { item_id: string; calibration_status: "OK" | "EXPIRED"; samples: CoreSample[] };

export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message); }
}

function errorMessage(status: number, detail: unknown): string {
  if (status === 401) return "로그인이 만료되었습니다. 다시 로그인해 주세요.";
  if (status === 409) return "다른 변경이 먼저 반영되었습니다. 최신 작업을 다시 불러와 확인해 주세요.";
  if (status === 422) return "입력값을 확인해 주세요. 필수 항목과 범위가 맞아야 합니다.";
  if (typeof detail === "string" && detail.length < 200) return detail;
  return `요청을 완료하지 못했습니다. (${status})`;
}

export async function api<T>(path: string, csrf?: string, body?: unknown): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, {
      method: body === undefined ? "GET" : "POST",
      credentials: "same-origin",
      headers: body === undefined ? undefined : { "Content-Type": "application/json", "X-CSRF-Token": csrf ?? "" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new ApiError(0, "서버에 연결할 수 없습니다. 잠시 후 다시 시도해 주세요.");
  }
  if (!response.ok) {
    const payload: unknown = await response.json().catch(() => null);
    const detail = typeof payload === "object" && payload !== null && "detail" in payload ? payload.detail : null;
    throw new ApiError(response.status, errorMessage(response.status, detail));
  }
  return response.json() as Promise<T>;
}

export const jobPath = (id: string) => `/api/customer/cooking-jobs/${encodeURIComponent(id)}`;

export function loadForMass(mass: number): { percent: string; band: "PARTIAL" | "REFERENCE" } | null {
  if (!Number.isFinite(mass) || mass <= 0 || mass > 0.8 || Math.abs(mass * 100 - Math.round(mass * 100)) > 1e-9) return null;
  const percent = Math.round(mass / 0.8 * 10000) / 100;
  return { percent: percent.toFixed(2), band: percent <= 50 ? "PARTIAL" : "REFERENCE" };
}
