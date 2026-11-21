import { useRef, useState } from "react";
import { api, jobPath } from "./cookingApi";

type Evidence = {
  job_version: number; process_view: "AS_PROCESSED" | "LATEST_RESTATED"; process_stale: boolean;
  recorded: { menu: string | null; recipe_version: string | null; safety_status: string | null; process_status: string | null;
    job_status: string | null; cooking_seconds: number | null; quality: { score_percent: string | null } | null;
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
  source_revision_status: "NOT_TRACKED" | "NOT_CONFIGURED" | "INCONSISTENT" | "MATCHED_AT_READ" | "CHANGED";
  reheat_history_status: "NOT_RECORDED" | "RECORDED" | "INCONSISTENT";
  reheat_history: { segment_id: string; started_at: string; lifted_at: string; duration_seconds: string;
    rechecks: { status: string; recorded_at: string | null }[] }[];
  selected_comparison: { comparison_id: string; baseline_job_id: string; baseline_recipe_version: string; baseline_score_percent: string;
    candidate_score_percent: string; immediately_previous_verified: boolean } | null;
};

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

type RecipeRequest = { execution_id: string; state: string; reason: string };
type LocalNotice = { quote_id: string; notice_version: string; model_id: string; quality_notice: string;
  transmission_location: "LOCAL"; estimated_cost_usd: string; expires_at: string };

export function RecommendationEvidence({ jobId, csrf, comparisonId }: { jobId: string; csrf: string; comparisonId?: string }) {
  const [value, setValue] = useState<Evidence | null>(null);
  const [requested, setRequested] = useState<RecipeRequest | null>(null);
  const [notice, setNotice] = useState<LocalNotice | null>(null);
  const [acknowledged, setAcknowledged] = useState(false);
  const [confirmed, setConfirmed] = useState(false);
  const restatementId = useRef<string | null>(null);
  const [restatementStatus, setRestatementStatus] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function load() {
    if (busy) return;
    setBusy(true); setError(""); setValue(null); setRequested(null); setNotice(null); setConfirmed(false); setAcknowledged(false);
    try {
      const next = await api<Evidence>(jobPath(jobId) + "/recommendation-evidence" +
        (comparisonId ? "?comparison_id=" + encodeURIComponent(comparisonId) : ""));
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
    <button type="button" onClick={() => void load()} disabled={busy}>{busy ? "조회 중" : "조리 근거 조회"}</button>
    {error && <p role="alert">{error}</p>}
    {value && <div aria-live="polite">
      <p>조회한 작업 v{value.job_version} · 레시피 {value.recorded.recipe_version ?? "미기록"}</p>
      {value.selected_comparison && <div><p>선택한 이전 비교 작업: {value.selected_comparison.baseline_job_id} ·
        레시피 {value.selected_comparison.baseline_recipe_version} · 평가 {value.selected_comparison.baseline_score_percent}점</p>
        <p className="small">선택한 비교는 현재 작업보다 이전입니다. 비교 가능한 작업 중 가장 직전인지는 확인하지 않았습니다.</p></div>}
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
      {requested && <><p>Tier 1 모델이 설정되지 않아 요청의 실행이 차단되어 있습니다.</p>
        {!notice && <button type="button" disabled={busy} onClick={() => void preview()}>로컬 모델 전환 고지 보기</button>}</>}
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
