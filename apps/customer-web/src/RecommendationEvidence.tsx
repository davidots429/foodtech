import { useState } from "react";
import { api, jobPath } from "./cookingApi";

type Evidence = {
  job_version: number; process_view: "AS_PROCESSED" | "LATEST_RESTATED"; process_stale: boolean;
  recorded: { recipe_version: string | null; safety_status: string | null; process_status: string | null;
    cooking_seconds: number | null; quality: { score_percent: string | null } | null };
  unverified_categories: string[];
  completeness: "INSUFFICIENT" | "NOT_ASSESSED";
  completeness_reasons: string[];
};

const completenessLabels: Record<string, string> = {
  MENU_NOT_ASSESSED: "이 메뉴의 입력 완전성은 아직 판정하지 않았습니다.",
  SAFETY_NOT_MEASURED_OR_INCOMPLETE: "안전 측정이 없거나 완료되지 않았습니다.",
  CORE_SERIES_MISSING_INVALID_OR_INCONSISTENT: "제품별 심부온도 표본이 누락·무효이거나 안전 상태와 일치하지 않습니다.",
  THICKNESS_PROFILE_MISSING: "두께 프로파일이 없습니다.", COOKING_DURATION_MISSING: "조리시간이 없습니다.",
  BATCH_INPUT_MISSING: "배치 입력이 없습니다.", INITIAL_TEMPERATURE_MISSING: "재료 초기 온도 또는 관찰 시각이 없습니다.",
  SOURCE_COMPLETENESS_NOT_ASSESSED: "전체 원천 데이터의 완전성은 아직 판정하지 않았습니다.",
  GAP_SEVERITY_NOT_ASSESSED: "감지된 결손의 심각도는 아직 판정하지 않았습니다.",
};

const labels: Record<string, string> = {
  SUPPORTED_SYNTHETIC_JOB: "지원되는 합성 작업", COMPLETED_COOKING_TIME: "완료된 조리시간",
  SAFETY_ASSESSMENT: "안전 판정", CURRENT_QUALITY_ASSESSMENT: "현재 유효한 품질평가",
  OBSERVATION_COVERAGE: "표본 관찰 범위", CURRENT_PROCESS_DERIVATION: "현재 작업에 맞는 공정 재계산",
  SOURCE_COMPLETENESS: "원천 데이터의 전체 완전성", DETECTED_PROCESS_GAP: "감지된 공정 데이터 결손",
  INITIAL_CONTEXT_STALE: "변경된 배치에 맞는 재료·기름 정보",
  INITIAL_CONTEXT_DECLARED_UNVERIFIED: "입력된 재료·기름 정보의 검증",
  STORAGE_OUTSIDE_SCENARIO: "승인된 냉장 생제품 시나리오와 다른 보관 상태",
  THICKNESS_PROFILE: "두께 프로파일", INGREDIENT_INITIAL_STATE: "재료 초기 상태",
  FRYER_AND_BATCH_BASELINE: "튀김기·배치 기준 정보", COMPARABLE_PREVIOUS_JOB: "비교 가능한 이전 조리",
  TARGET_SETTINGS_AND_INTERVALS: "목표 설정과 투입 간격", PREVIOUS_JAM_OR_OVERLOAD: "이전 걸림·과부하 여부",
};

export function RecommendationEvidence({ jobId }: { jobId: string }) {
  const [value, setValue] = useState<Evidence | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function load() {
    if (busy) return;
    setBusy(true); setError(""); setValue(null);
    try { setValue(await api<Evidence>(jobPath(jobId) + "/recommendation-evidence")); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "조리 근거를 조회하지 못했습니다."); }
    finally { setBusy(false); }
  }
  return <section className="binding-box" aria-labelledby="recommendation-evidence-title">
    <h4 id="recommendation-evidence-title">AI 권고에 사용할 조리 기록 확인</h4>
    <p className="small">기록된 정보와 필수 근거의 일부를 확인합니다. 전체 데이터 완전성과 AI 권고는 아직 판정하지 않습니다.</p>
    <button type="button" onClick={() => void load()} disabled={busy}>{busy ? "조회 중" : "조리 근거 조회"}</button>
    {error && <p role="alert">{error}</p>}
    {value && <div aria-live="polite">
      <p>조회한 작업 v{value.job_version} · 레시피 {value.recorded.recipe_version ?? "미기록"}</p>
      <p>공정 {value.recorded.process_status ?? "미기록"} · 안전 {value.recorded.safety_status ?? "미기록"}
        {" "}· 품질평가 {value.recorded.quality?.score_percent == null ? "현재 유효 평가 없음" : `${value.recorded.quality.score_percent}점`}</p>
      {value.process_stale && <p className="warning">공정 재계산 이후 작업이 변경되어 다시 확인해야 합니다.</p>}
      <p>입력 완전성: {value.completeness === "INSUFFICIENT" ? "필수 근거 부족" : "전체 미평가"}</p>
      <ul>{value.completeness_reasons.map(reason => <li key={reason}>{completenessLabels[reason] ?? reason}</li>)}</ul>
      {value.unverified_categories.length > 0 && <><p>아직 확인하지 못한 범주</p>
        <ul>{value.unverified_categories.map(category => <li key={category}>{labels[category] ?? category}</li>)}</ul></>}
      <p className="small">이 화면에서 AI를 호출하거나 장비 설정을 변경하지 않습니다.</p>
    </div>}
  </section>;
}
