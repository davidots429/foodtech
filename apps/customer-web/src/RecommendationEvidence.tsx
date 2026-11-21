import { useRef, useState } from "react";
import { api, jobPath } from "./cookingApi";

type FeedTiming = { status: "RECORDED"; vegetable_start_offset_us: number; sauce_start_offset_us: number;
  start_separation_us: number; simultaneous: boolean } | { status: "NOT_RECORDED" | "INCONSISTENT" };

type Setting = { status: "NOT_RECORDED" | "PARTIAL" | "CHANGED" | "STABLE"; value: string | null };

type JeyukCollection = { status: "NOT_CONFIGURED" | "NOT_RECORDED" | "REVISION_UNVERIFIED" | "CHANGED_DURING_READ" | "NOT_MATCHED" | "MATCHED_AT_READ";
  raw_count: number | null; settings?: { heating_setpoint_celsius: Setting; target_rpm: Setting };
  process_observations?: { reason_codes: string[]; basis: "SYNTHETIC_SAMPLES_ONLY" } };

type Evidence = {
  job_version: number; process_view: "AS_PROCESSED" | "LATEST_RESTATED"; process_stale: boolean;
  recorded: { menu: string | null; recipe_version: string | null; safety_status: string | null; process_status: string | null;
    job_status: string | null; cooking_seconds: number | null; jeyuk_feed_timing: FeedTiming | null; quality: { score_percent: string | null } | null;
    temperature_sample_summary: { sample_count: number; minimum_sample_celsius: string;
      maximum_sample_celsius: string; below_160_between_qualifying_samples_us: number;
      above_180_between_qualifying_samples_us: number; longest_above_180_sample_span_us: number;
      last_sampled_recovery: { span_us: number; onset_bracketed_by_samples: boolean } | null } | null;
    preplacement_temperature_observation: { status: "OBSERVED" | "NOT_CAPTURED" | "AMBIGUOUS";
      sample_celsius: string | null; age_before_placement_us: number | null; quality_status: string | null } | null };
  unverified_categories: string[];
  completeness: "INSUFFICIENT" | "NOT_ASSESSED";
  completeness_reasons: string[];
  collection_snapshot_matches_receipt: boolean;
  jeyuk_collection: JeyukCollection | null;
  jeyuk_timeline: "MATCHED" | "NOT_MATCHED" | "NOT_ASSESSED" | null;
  selected_prior_job: { job_id: string; job_version: number; status: string; safety_status: string;
    started_at: string; ended_at: string | null; order_status: "ENDED_BEFORE" | "END_UNVERIFIED";
    immediately_previous_verified: false; collection: JeyukCollection; timeline: "MATCHED" | "NOT_MATCHED" | "NOT_ASSESSED" } | null;
  source_revision_status: "NOT_TRACKED" | "NOT_CONFIGURED" | "INCONSISTENT" | "MATCHED_AT_READ" | "CHANGED";
  reheat_history_status: "NOT_RECORDED" | "RECORDED" | "INCONSISTENT";
  reheat_history: { segment_id: string; started_at: string; lifted_at: string; duration_seconds: string;
    rechecks: { status: string; recorded_at: string | null }[] }[];
  selected_comparison: { comparison_id: string; baseline_job_id: string; baseline_recipe_version: string; baseline_score_percent: string;
    candidate_score_percent: string; baseline_jeyuk_feed_timing: FeedTiming | null; immediately_previous_verified: boolean;
    baseline_jeyuk_collection?: JeyukCollection | null; baseline_jeyuk_timeline?: "MATCHED" | "NOT_MATCHED" | "NOT_ASSESSED" } | null;
};

const processObservationLabels: Record<string, string> = {
  AGITATOR_JAM_WARNING: "교반기 걸림 의심", AGITATOR_FAILURE: "교반기 장애",
  OVER_RECOMMENDED_LOAD: "권장 적재량 초과", RATED_LOAD_EXCEEDED: "정격 적재량 초과",
};

function settingLabel(setting: Setting, unit: string): string {
  if (setting.status === "STABLE") return `${setting.value} ${unit}`;
  return ({ NOT_RECORDED: "미기록", PARTIAL: "일부 기록", CHANGED: "조리 중 변경됨" })[setting.status] ?? "확인 필요";
}

function FeedTimingView({ timing, label }: { timing: FeedTiming | null; label: string }) {
  if (!timing) return null;
  if (timing.status !== "RECORDED") return <p>{label} 투입 시각: {timing.status === "INCONSISTENT" ? "기록 불일치" : "기록 부족"}</p>;
  return <p>{label} 투입 시각: 고기 투입 완료 후 채소 {timing.vegetable_start_offset_us / 1_000_000}초,
    소스 {timing.sauce_start_offset_us / 1_000_000}초 · 시작 차이 {timing.start_separation_us / 1_000_000}초
    ({timing.simultaneous ? "논리적 동시 투입" : "동시 투입 아님"})</p>;
}

const completenessLabels: Record<string, string> = {
  MENU_NOT_ASSESSED: "이 메뉴의 입력 완전성은 아직 판정하지 않았습니다.",
  SAFETY_NOT_MEASURED_OR_INCOMPLETE: "안전 측정이 없거나 완료되지 않았습니다.",
  CORE_SERIES_MISSING_INVALID_OR_INCONSISTENT: "제품별 심부온도 표본이 누락·무효이거나 안전 상태와 일치하지 않습니다.",
  THICKNESS_PROFILE_MISSING: "두께 프로파일이 없습니다.", COOKING_DURATION_MISSING: "조리시간이 없습니다.",
  BATCH_INPUT_MISSING: "배치 입력이 없습니다.", INITIAL_TEMPERATURE_MISSING: "재료 초기 온도 또는 관찰 시각이 없습니다.",
  JEYUK_FEEDS_MISSING: "고기·채소·소스 투입 기록이 부족합니다.",
  JEYUK_INITIAL_TEMPERATURE_MISSING: "재료 초기 온도와 관찰 시각이 기록되지 않았습니다.",
  JEYUK_INITIAL_TEMPERATURE_PARTIAL: "일부 재료의 초기 온도만 기록되어 있습니다.",
  JEYUK_FEED_MASS_UNVERIFIED: "투입량 합계가 확인되지 않았습니다.",
  JEYUK_FEED_SAFETY_UNVERIFIED: "투입 시각과 안전 표본의 일치가 확인되지 않았습니다.",
  JEYUK_INITIAL_TEMPERATURE_SUITABILITY_NOT_ASSESSED: "재료 초기 온도의 적합성은 아직 판정하지 않았습니다.",
  SOURCE_COMPLETENESS_NOT_ASSESSED: "전체 원천 데이터의 완전성은 아직 판정하지 않았습니다.",
  SEVERE_OBSERVATION_GAP: "조리 중 사용 가능한 표본이 30초 이상 연속으로 없어 권고할 수 없습니다.",
  GAP_SEVERITY_NOT_ASSESSED: "감지된 결손의 심각도는 아직 판정하지 않았습니다.",
};

const labels: Record<string, string> = {
  SUPPORTED_SYNTHETIC_JOB: "지원되는 합성 작업", COMPLETED_COOKING_TIME: "완료된 조리시간",
  SAFETY_ASSESSMENT: "안전 판정", CURRENT_QUALITY_ASSESSMENT: "현재 유효한 품질평가",
  OBSERVATION_COVERAGE: "표본 관찰 범위", CURRENT_PROCESS_DERIVATION: "현재 작업에 맞는 공정 재계산",
  SOURCE_COMPLETENESS: "원천 데이터의 전체 완전성", DETECTED_PROCESS_GAP: "감지된 공정 데이터 결손",
  REHEAT_HISTORY_NOT_ASSESSED: "추가 가열 전체 이력의 추천 적합성",
  INITIAL_CONTEXT_STALE: "변경된 배치에 맞는 재료·기름 정보",
  INITIAL_CONTEXT_DECLARED_UNVERIFIED: "입력된 재료·기름 정보의 검증",
  STORAGE_OUTSIDE_SCENARIO: "승인된 냉장 생제품 시나리오와 다른 보관 상태",
  THICKNESS_PROFILE: "두께 프로파일", INGREDIENT_INITIAL_STATE: "재료 초기 상태",
  PREPLACEMENT_TEMPERATURE_UNAVAILABLE: "투입 전 온도 표본",
  PREPLACEMENT_RECENCY_NOT_ASSESSED: "투입 전 표본의 최신성 기준",
  PREPLACEMENT_TEMPERATURE_FLAGGED: "투입 전 표본의 품질 경고 검토",
  FRYER_AND_BATCH_BASELINE: "튀김기·배치 기준 정보", COMPARABLE_PREVIOUS_JOB: "비교 가능한 이전 조리",
  TARGET_SETTINGS_AND_INTERVALS: "목표 설정과 투입 간격", PREVIOUS_JAM_OR_OVERLOAD: "이전 걸림·과부하 여부",
};

type PriorCandidate = { status: "NOT_BOUND" | "CURRENT_NOT_STARTED" | "HISTORY_INCOMPLETE" | "NO_CANDIDATE" | "AMBIGUOUS" | "CANDIDATE" | "UNAVAILABLE";
  candidate: { job_id: string; status: string; safety_status: string; started_at: string } | null;
  immediately_previous_verified: false };

type RecipeRequest = { execution_id: string; state: string; reason: string };
const recipeBlockedMessages: Record<string, string> = {
  TIER1_MODEL_NOT_CONFIGURED: "Tier 1 모델이 설정되지 않아 실행할 수 없습니다.",
  MODEL_NOT_ACTIVE: "Tier 1 모델의 승인 또는 활성 상태를 확인해야 합니다.",
  MODEL_CONFIGURATION_INVALID: "Tier 1 모델 설정을 확인해야 합니다.",
  EXTERNAL_TRANSFER_CONSENT_REQUIRED: "외부 전송 동의를 확인해야 합니다.",
  EXTERNAL_TRANSFER_BLOCKED: "현재 데이터의 외부 전송은 허용되지 않습니다.",
  CREDENTIAL_NOT_USABLE: "외부 공급자 자격증명을 확인해야 합니다.",
  LIMIT_ACTION_REQUIRED: "AI 사용 한도를 확인해야 합니다.",
  RECOMMENDATION_EVIDENCE_UNVERIFIED: "모델이 준비되어 있지만 조리 근거 검증이 끝나지 않아 실행할 수 없습니다.",
};
const downgradeEligible = new Set(["TIER1_MODEL_NOT_CONFIGURED", "MODEL_NOT_ACTIVE", "MODEL_CONFIGURATION_INVALID",
  "EXTERNAL_TRANSFER_CONSENT_REQUIRED", "EXTERNAL_TRANSFER_BLOCKED", "CREDENTIAL_NOT_USABLE", "LIMIT_ACTION_REQUIRED"]);
type LocalNotice = { quote_id: string; notice_version: string; model_id: string; quality_notice: string;
  transmission_location: "LOCAL"; estimated_cost_usd: string; expires_at: string };

export function RecommendationEvidence({ jobId, csrf, comparisonId, menu }: { jobId: string; csrf: string; comparisonId?: string; menu: string }) {
  const [value, setValue] = useState<Evidence | null>(null);
  const [priorJobId, setPriorJobId] = useState("");
  const [priorCandidate, setPriorCandidate] = useState<PriorCandidate | null>(null);
  const [requested, setRequested] = useState<RecipeRequest | null>(null);
  const [notice, setNotice] = useState<LocalNotice | null>(null);
  const [acknowledged, setAcknowledged] = useState(false);
  const [confirmed, setConfirmed] = useState(false);
  const restatementId = useRef<string | null>(null);
  const [restatementStatus, setRestatementStatus] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function findPriorCandidate() {
    if (busy) return;
    setBusy(true); setError(""); setPriorCandidate(null);
    try { setPriorCandidate(await api<PriorCandidate>(jobPath(jobId) + "/prior-job-candidate")); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "이전 작업 후보를 조회하지 못했습니다."); }
    finally { setBusy(false); }
  }
  async function load() {
    if (busy) return;
    setBusy(true); setError(""); setValue(null); setRequested(null); setNotice(null); setConfirmed(false); setAcknowledged(false);
    try {
      const params = new URLSearchParams();
      if (comparisonId) params.set("comparison_id", comparisonId);
      if (menu === "JEYUK" && priorJobId.trim()) params.set("prior_job_id", priorJobId.trim());
      const next = await api<Evidence>(jobPath(jobId) + "/recommendation-evidence" +
        (params.size ? "?" + params.toString() : ""));
      if (!value || value.job_version !== next.job_version) { restatementId.current = null; setRestatementStatus(""); }
      setValue(next);
    }
    catch (cause) { setError(cause instanceof Error ? cause.message : "조리 근거를 조회하지 못했습니다."); }
    finally { setBusy(false); }
  }
  async function requestRecipe() {
    if (!value || busy) return;
    setBusy(true); setError("");
    try { setRequested(await api<RecipeRequest>("/api/customer/ai/recipe-requests", csrf,
      { job_id: jobId, job_version: value.job_version, comparison_id: value.selected_comparison?.comparison_id ?? null })); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "권고 요청을 기록하지 못했습니다."); }
    finally { setBusy(false); }
  }
  async function requestRestatement() {
    if (!value || busy) return;
    setBusy(true); setError("");
    const requestId = restatementId.current ?? crypto.randomUUID();
    restatementId.current = requestId;
    try {
      const result = await api<{ status: string }>(jobPath(jobId) + "/process-restatements", csrf,
        { request_id: requestId, expected_version: value.job_version });
      setRestatementStatus(result.status);
      if (["COMPLETED", "STALE"].includes(result.status)) restatementId.current = null;
    } catch (cause) { setError(cause instanceof Error ? cause.message : "재계산 요청을 전달하지 못했습니다."); }
    finally { setBusy(false); }
  }
  async function checkRestatement() {
    if (!restatementId.current || busy) return;
    setBusy(true); setError("");
    try {
      const result = await api<{ status: string }>(jobPath(jobId) + "/process-restatements/" + restatementId.current);
      setRestatementStatus(result.status);
      if (["COMPLETED", "STALE"].includes(result.status)) restatementId.current = null;
    } catch (cause) { setError(cause instanceof Error ? cause.message : "재계산 상태를 조회하지 못했습니다."); }
    finally { setBusy(false); }
  }
  async function preview() {
    if (!value || !requested || busy) return;
    setBusy(true); setError("");
    try { setNotice(await api<LocalNotice>(`/api/customer/ai/recipe-requests/${requested.execution_id}/local-preview`, csrf,
      { job_id: jobId, job_version: value.job_version, comparison_id: value.selected_comparison?.comparison_id ?? null })); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "전환 고지를 조회하지 못했습니다."); }
    finally { setBusy(false); }
  }
  async function confirm() {
    if (!value || !notice || !acknowledged || busy) return;
    setBusy(true); setError("");
    try { await api(`/api/customer/ai/recipe-quotes/${notice.quote_id}/confirm`, csrf,
      { job_id: jobId, job_version: value.job_version, comparison_id: value.selected_comparison?.comparison_id ?? null, notice_version: notice.notice_version,
        quality_warning_acknowledged: true }); setConfirmed(true); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "전환 동의를 기록하지 못했습니다."); }
    finally { setBusy(false); }
  }
  return <section className="binding-box" aria-labelledby="recommendation-evidence-title">
    <h4 id="recommendation-evidence-title">AI 권고에 사용할 조리 기록 확인</h4>
    <p className="small">기록된 정보와 필수 근거의 일부를 확인합니다. 전체 데이터 완전성과 AI 권고는 아직 판정하지 않습니다.</p>
    <p className="small">아래에서 비교 기록을 선택하고 최신성을 확인하면 다음 근거 조회에 포함됩니다.</p>
    {menu === "JEYUK" && <div>
      <button type="button" className="secondary" onClick={() => void findPriorCandidate()} disabled={busy}>이전 작업 후보 찾기</button>
      {priorCandidate && <div className="small" role="status">
        <p>{({ NOT_BOUND: "수집 배치를 연결해야 합니다.", CURRENT_NOT_STARTED: "현재 조리의 시작 시각이 필요합니다.",
          HISTORY_INCOMPLETE: "확인할 수 없는 작업이 있거나 목록이 너무 길어 후보를 정할 수 없습니다.",
          NO_CANDIDATE: "조회한 목록에서 이전 작업 후보가 없습니다.", AMBIGUOUS: "같은 시각의 후보가 여러 개입니다.",
          CANDIDATE: "조회한 목록에서 가장 최근에 시작한 후보입니다.", UNAVAILABLE: "가장 최근 후보가 진행 중이거나 이력 조회 조건에 맞지 않습니다." })[priorCandidate.status]}</p>
        {priorCandidate.status === "CANDIDATE" && priorCandidate.candidate && <button type="button" onClick={() => {
          setPriorJobId(priorCandidate.candidate!.job_id); setValue(null);
        }}>이 후보 선택</button>}
        <p>목록에 없는 과거 작업이나 조회 직후의 변경은 확인할 수 없어 가장 직전이라고 확정하지 않습니다.</p>
      </div>}
    </div>}
    {menu === "JEYUK" && <label>확인할 이전 작업 ID (선택)
      <input value={priorJobId} disabled={busy} onChange={event => { setPriorJobId(event.target.value); setValue(null); }}
        placeholder="이전 조리작업의 ID" />
    </label>}
    <button type="button" onClick={() => void load()} disabled={busy}>{busy ? "조회 중" : "조리 근거 조회"}</button>
    {error && <p role="alert">{error}</p>}
    {value && <div aria-live="polite">
      <p>조회한 작업 v{value.job_version} · 레시피 {value.recorded.recipe_version ?? "미기록"}</p>
      {value.selected_comparison && <div><p>선택한 이전 비교 작업: {value.selected_comparison.baseline_job_id} ·
        레시피 {value.selected_comparison.baseline_recipe_version} · 평가 {value.selected_comparison.baseline_score_percent}점</p>
        <p className="small">선택한 비교는 현재 작업보다 이전입니다. 비교 가능한 작업 중 가장 직전인지는 확인하지 않았습니다.</p></div>}
      {value.selected_prior_job && <div>
        <p>지정한 이전 작업 · 상태 {({ COMPLETED: "완료", FAILED: "실패", CANCELED: "취소" } as Record<string, string>)[value.selected_prior_job.status] ?? "확인 필요"}
          {" "}· 안전 {({ PASSED: "통과", FAILED: "실패", INCOMPLETE: "미완료", NOT_MEASURED: "미측정", SUSPECTED: "이상 의심", UNDER_REVIEW: "검토 중" } as Record<string, string>)[value.selected_prior_job.safety_status] ?? "확인 필요"}</p>
        <p>작업 순서: {value.selected_prior_job.order_status === "ENDED_BEFORE" ? "현재 작업 시작 전 종료 기록" : "먼저 시작했으나 종료 시각 미확인"}
          {" "}· 원천 조리 구간 {({ MATCHED: "일치", NOT_MATCHED: "불일치", NOT_ASSESSED: "미확인" })[value.selected_prior_job.timeline]}</p>
        {value.selected_prior_job.collection.settings && <p>기록된 설정: 열원 {settingLabel(value.selected_prior_job.collection.settings.heating_setpoint_celsius, "°C")}
          {" "}· 목표 회전속도 {settingLabel(value.selected_prior_job.collection.settings.target_rpm, "rpm")}</p>}
        {value.selected_prior_job.collection.process_observations && <p>합성 표본의 걸림·적재 이상 신호: {value.selected_prior_job.collection.process_observations.reason_codes.length ?
          value.selected_prior_job.collection.process_observations.reason_codes.map(code => processObservationLabels[code] ?? "추가 확인 필요").join(", ") : "표본에서 감지되지 않음"}</p>}
        <p className="small">가장 직전 작업 여부와 표본 밖 이상은 확인하지 않았으며 이 지정은 AI 요청에 반영되지 않습니다.</p>
      </div>}
      <FeedTimingView label="현재 작업" timing={value.recorded.jeyuk_feed_timing} />
      {value.jeyuk_collection && <div>
        <p>합성 수집 원천: {({ NOT_CONFIGURED: "저장소 미연결", NOT_RECORDED: "수집 완료 기록 없음",
          REVISION_UNVERIFIED: "원천 버전 미확인", CHANGED_DURING_READ: "조회 중 원천 변경",
          NOT_MATCHED: "완료 기록과 불일치", MATCHED_AT_READ: "조회한 원천과 완료 기록 일치" })[value.jeyuk_collection.status]}</p>
        {value.jeyuk_collection.settings && <p>기록된 열원 목표 온도: {settingLabel(value.jeyuk_collection.settings.heating_setpoint_celsius, "°C")}
          {" "}· 목표 회전속도: {settingLabel(value.jeyuk_collection.settings.target_rpm, "rpm")}</p>}
        <p className="small">수집된 설정 기록이며 적합성이나 AI 실행 승인을 뜻하지 않습니다.</p>
      </div>}
      {value.selected_comparison && <FeedTimingView label="선택한 이전 작업"
        timing={value.selected_comparison.baseline_jeyuk_feed_timing} />}
      {value.selected_comparison?.baseline_jeyuk_collection && <div>
        <p>선택한 이전 작업의 수집 기록: {value.selected_comparison.baseline_jeyuk_collection.status === "MATCHED_AT_READ" ? "원천 대조됨" : "원천 확인 필요"}
          {" "}· 조리 구간 {({ MATCHED: "일치", NOT_MATCHED: "불일치", NOT_ASSESSED: "미확인" })[value.selected_comparison.baseline_jeyuk_timeline ?? "NOT_ASSESSED"]}</p>
        {value.selected_comparison.baseline_jeyuk_collection.settings && <p>이전 수집 설정: 열원 {settingLabel(value.selected_comparison.baseline_jeyuk_collection.settings.heating_setpoint_celsius, "°C")}
          {" "}· 목표 회전속도 {settingLabel(value.selected_comparison.baseline_jeyuk_collection.settings.target_rpm, "rpm")}</p>}
        {value.selected_comparison.baseline_jeyuk_collection.process_observations && <p>이전 합성 표본의 걸림·적재 이상 신호: {value.selected_comparison.baseline_jeyuk_collection.process_observations.reason_codes.length ?
          value.selected_comparison.baseline_jeyuk_collection.process_observations.reason_codes.map(code => processObservationLabels[code] ?? "추가 확인 필요").join(", ") : "표본에서 감지되지 않음"}</p>}
        <p className="small">선택한 이전 작업의 합성 표본만 분석합니다. 가장 직전 작업 여부와 표본 밖 이상은 확인하지 않습니다.</p>
      </div>}
      {value.jeyuk_timeline && <p>현재 작업과 수집 시각: {({ MATCHED: "일치", NOT_MATCHED: "불일치", NOT_ASSESSED: "미확인" })[value.jeyuk_timeline]}</p>}
      <p>공정 {value.recorded.process_status ?? "미기록"} · 안전 {value.recorded.safety_status ?? "미기록"}
        {" "}· 품질평가 {value.recorded.quality?.score_percent == null ? "현재 유효 평가 없음" : `${value.recorded.quality.score_percent}점`}</p>
      {value.reheat_history_status === "INCONSISTENT" && <p className="warning">추가 가열 이력의 연결을 확인할 수 없습니다.</p>}
      {value.reheat_history.length > 0 && <div><p>기록된 추가 가열 구간</p><ul>
        {value.reheat_history.map(segment => <li key={segment.segment_id}>{segment.started_at} ~ {segment.lifted_at}
          {" "}({segment.duration_seconds}초) · 재측정 {segment.rechecks.length ?
            segment.rechecks.map(check => check.status).join(", ") : "없음"}</li>)}
      </ul><p className="small">구간 이력의 추천 적합성은 아직 판정하지 않았습니다.</p></div>}
      {value.process_stale && <p className="warning">공정 재계산 이후 작업이 변경되어 다시 확인해야 합니다.</p>}
      {value.recorded.preplacement_temperature_observation?.status === "OBSERVED" &&
        typeof value.recorded.preplacement_temperature_observation.age_before_placement_us === "number" && <p>
        재계산 당시 투입 전 마지막으로 기록된 표본: {value.recorded.preplacement_temperature_observation.sample_celsius}°C ·
        투입 {value.recorded.preplacement_temperature_observation.age_before_placement_us / 1_000_000}초 전 ·
        품질 {value.recorded.preplacement_temperature_observation.quality_status}</p>}
      {value.recorded.preplacement_temperature_observation?.status === "AMBIGUOUS" &&
        <p className="warning">같은 시각의 투입 전 표본이 여러 개여서 온도를 선택하지 않았습니다.</p>}
      {value.recorded.temperature_sample_summary && <div>
        <p>조리 중 관찰 표본 {value.recorded.temperature_sample_summary.sample_count}개 ·
          표본 최저 {value.recorded.temperature_sample_summary.minimum_sample_celsius}°C ·
          표본 최고 {value.recorded.temperature_sample_summary.maximum_sample_celsius}°C</p>
        <p>160°C 미만 연속 표본 사이 합계 {value.recorded.temperature_sample_summary.below_160_between_qualifying_samples_us / 1_000_000}초 ·
          180°C 초과 연속 표본 사이 합계 {value.recorded.temperature_sample_summary.above_180_between_qualifying_samples_us / 1_000_000}초</p>
        {value.recorded.temperature_sample_summary.last_sampled_recovery && <p>마지막 저온 표본부터 정상 범위 표본까지
          {" "}{value.recorded.temperature_sample_summary.last_sampled_recovery.span_us / 1_000_000}초 ·
          {value.recorded.temperature_sample_summary.last_sampled_recovery.onset_bracketed_by_samples ?
            " 저온 진입 전 표본 있음" : " 저온 진입 전 표본 없음"}</p>}
        <p className="small">표본 사이의 실제 온도와 투입 직전 온도는 확인하지 못했습니다.</p>
      </div>}
      {value.collection_snapshot_matches_receipt && <p>저장된 수집 원본 스냅샷이 수집 종료 기록과 일치합니다.
        전체 원천 완전성과 이후 추가 데이터는 별도로 확인해야 합니다.</p>}
      {value.source_revision_status === "MATCHED_AT_READ" && <p>조회 시점의 수집 버전이 공정 재계산 버전과 일치합니다.
        이후 변경과 구버전 수집기의 쓰기는 별도로 확인해야 합니다.</p>}
      {value.source_revision_status === "CHANGED" && <p className="warning">공정 재계산 후 수집 원본이 변경되었습니다.
        새 재계산을 확인해 주세요.</p>}
      {value.source_revision_status === "INCONSISTENT" && <p className="warning">원천 연결과 재계산 대상이 일치하지 않습니다.</p>}
      {value.source_revision_status === "NOT_CONFIGURED" && <p>원천 변경 확인을 위한 저장소가 설정되지 않았습니다.</p>}
      {value.source_revision_status === "NOT_TRACKED" && <p>원천 변경을 확인할 수 있는 버전 기록이 없습니다.</p>}
      <p>입력 완전성: {value.completeness === "INSUFFICIENT" ? "필수 근거 부족" : "전체 미평가"}</p>
      <ul>{value.completeness_reasons.map(reason => <li key={reason}>{completenessLabels[reason] ?? reason}</li>)}</ul>
      {value.unverified_categories.length > 0 && <><p>아직 확인하지 못한 범주</p>
        <ul>{value.unverified_categories.map(category => <li key={category}>{labels[category] ?? category}</li>)}</ul></>}
      {value.recorded.menu === "TONKATSU" && value.recorded.job_status && ["COMPLETED", "FAILED"].includes(value.recorded.job_status) && <div>
        <button type="button" disabled={busy} onClick={() => void requestRestatement()}>공정 근거 재계산 요청</button>
        {restatementId.current && <button type="button" disabled={busy} onClick={() => void checkRestatement()}>재계산 상태 확인</button>}
        {restatementStatus && <p>재계산 요청: {restatementStatus === "COMPLETED" ? "완료 · 조리 근거를 다시 조회해 주세요" :
          restatementStatus === "STALE" ? "작업이 변경되어 취소됨 · 조리 근거를 다시 조회해 주세요" : "처리 대기"}</p>}
      </div>}
      {!requested && <button type="button" onClick={() => void requestRecipe()}
        disabled={busy || value.completeness === "INSUFFICIENT"}>AI 권고 요청 기록</button>}
      {requested && <><p>{recipeBlockedMessages[requested.reason] ?? "요청의 실행 조건을 확인해야 합니다."}</p>
        {!notice && downgradeEligible.has(requested.reason) && <button type="button" disabled={busy} onClick={() => void preview()}>로컬 모델 전환 고지 보기</button>}</>}
      {notice && !confirmed && <div>
        <p>{notice.quality_notice}</p>
        <p>모델: {notice.model_id} · 처리 위치: 로컬 서버 · 예상 API 호출 비용: 00원</p>
        <p>시연 기능이며 실제 과금은 없습니다. 로컬 서버 자원은 별도로 사용합니다.</p>
        <p>고지 만료: {new Date(notice.expires_at).toLocaleString()}</p>
        <label><input type="checkbox" checked={acknowledged} onChange={event => setAcknowledged(event.target.checked)} />
          품질 저하 가능성과 모델·처리 위치·예상 비용을 확인했습니다.</label>
        <button type="button" disabled={busy || !acknowledged} onClick={() => void confirm()}>전환에 동의</button>
      </div>}
      {confirmed && <p role="status">전환 동의가 기록되었습니다. 원천 데이터 완전성과 실행 전 근거 확인이 끝나야 권고를 생성할 수 있습니다.</p>}
      <p className="small">이 화면에서 AI를 호출하거나 장비 설정을 변경하지 않습니다.</p>
    </div>}
  </section>;
}
