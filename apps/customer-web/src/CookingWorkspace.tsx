import { useCallback, useEffect, useRef, useState } from "react";
import {
  ApiError, api, jobPath, loadForMass, scoreItems,
  type Assessment, type AssessmentKind, type CookingJob, type CurrentAssessment,
  type JobEvent, type JobSnapshot, type Probe, type ProcessAssessmentView, type ScoreKey, type TransitionEvent,
} from "./cookingApi";
import { AssessmentAggregate } from "./AssessmentAggregate";

type Props = { csrf: string };
type ScoreForm = Record<ScoreKey, number | "">;
type ObservationForm = Record<ScoreKey, string>;
type Comparison = {
  comparison_id: string; baseline_job_id: string; candidate_job_id: string;
  baseline_assessment_version: number; candidate_assessment_version: number;
  baseline_recipe_version: string; candidate_recipe_version: string;
  baseline_actual_portions: number; candidate_actual_portions: number;
  baseline_actual_mass_kg: string; candidate_actual_mass_kg: string;
  baseline_load_percent: string; candidate_load_percent: string;
  baseline_score_percent: string; candidate_score_percent: string;
  baseline_quality_passed: boolean; candidate_quality_passed: boolean;
  score_deltas: Record<ScoreKey, number>; score_total_delta: number;
  improvement_status: "OBSERVED" | "NOT_OBSERVED";
  recipe_improvement_status: "NOT_EVALUATED"; compared_at: string;
};

const jobLabels: Record<CookingJob["status"], string> = {
  PENDING: "대기", READY: "준비 완료", RUNNING: "조리 중", PAUSED: "일시 정지",
  SAFETY_HOLD: "안전 확인으로 진행 보류", COMPLETED: "조리 완료", FAILED: "실패", CANCELED: "취소",
};
const safetyLabels: Record<CookingJob["safety_status"], string> = {
  NOT_MEASURED: "안전 확인 전", PASSED: "안전 통과", FAILED: "안전 실패",
  INCOMPLETE: "안전 확인 미완료", SUSPECTED: "이상 의심",
  UNDER_REVIEW: "확인 중", CLEARED: "의심 해제",
};
const eventLabels: Record<string, string> = {
  PREHEAT_COMPLETED: "합성 예열 완료", PREHEAT_CONDITION_LOST: "예열 조건 상실",
  COOKING_STARTED: "조리 시작", COOKING_PAUSED: "조리 일시 정지",
  COOKING_RESUMED: "조리 재개", COOKING_COMPLETED: "조리 완료",
  COOKING_CANCELED: "조리 취소", SUSPECTED: "이상 의심 신고",
  UNDER_REVIEW: "안전 확인 시작", CLEARED: "이상 의심 해제",
  FAILED: "안전 실패 확정", CORE_CHECK: "합성 심부온도 안전 판정",
  PROCESS_SAMPLE_ASSESSED: "수집 온도 공정판정", PROCESS_FAILURE_DETECTED: "공정 실패 자동판정",
};
const processLabels: Record<CookingJob["process_assessment_status"], string> = {
  INSUFFICIENT_DATA: "판정 자료 대기", NORMAL: "정상", WARNING: "주의", FAILED: "공정 실패",
};
const temperatureLabels: Record<string, string> = {
  NOT_MEASURED: "미측정", NORMAL: "정상 범위", LOW_WARNING: "저온 주의",
  HIGH_WARNING: "고온 주의", LOW_PROCESS_FAILURE: "저온 공정 실패",
  HIGH_PROCESS_FAILURE: "고온 공정 실패",
};
const processReasonLabels: Record<string, string> = {
  LOW_TEMPERATURE_DURATION: "저온 지속·복원시간 기준 초과",
  HIGH_TEMPERATURE_DURATION: "고온 지속시간 기준 초과",
  EARLY_REMOVAL: "두께 기준보다 이른 인양",
  MAX_TIME_EXCEEDED: "두께 기준의 최대 조리시간 초과",
};
const thicknessLabels: Record<string, string> = {
  IN_RANGE: "허용 범위", BELOW_RANGE: "기준보다 얇음 · 주의", ABOVE_RANGE: "프로파일 적용 불가",
};
const timeLabels: Record<string, string> = {
  NOT_MEASURED: "인양 전", NOT_APPLICABLE: "두께 프로파일 적용 불가",
  EARLY_REMOVAL: "조기 인양 · 실패", SHORT_WARNING: "시간 부족 · 주의",
  NORMAL: "정상", DELAY_WARNING: "지연 · 주의",
  EXTENDED_COOKING: "권장시간 초과 · 주의", MAX_TIME_EXCEEDED: "최대시간 초과 · 실패",
};
function eventStatusLabel(domain: string, status: string): string {
  if (domain === "COOKING_JOB_STATUS") return jobLabels[status as CookingJob["status"]] ?? status;
  if (domain === "PROCESS_ASSESSMENT") return processLabels[status as CookingJob["process_assessment_status"]] ?? status;
  return safetyLabels[status as CookingJob["safety_status"]] ?? status;
}
const transitions: Record<CookingJob["status"], { event: TransitionEvent; label: string }[]> = {
  PENDING: [{ event: "PREHEAT_COMPLETED", label: "합성 예열 완료 기록" }],
  READY: [
    { event: "PREHEAT_CONDITION_LOST", label: "예열 조건 상실 기록" },
    { event: "COOKING_STARTED", label: "재료 투입·조리 시작" },
  ],
  RUNNING: [
    { event: "COOKING_PAUSED", label: "일시 정지 기록" },
    { event: "COOKING_COMPLETED", label: "인양·조리 완료" },
    { event: "COOKING_CANCELED", label: "조리 취소" },
  ],
  PAUSED: [
    { event: "COOKING_RESUMED", label: "조리 재개" },
    { event: "COOKING_CANCELED", label: "조리 취소" },
  ],
  SAFETY_HOLD: [
    { event: "COOKING_RESUMED", label: "안전 확인 후 조리 재개" },
    { event: "COOKING_CANCELED", label: "안전 보류 작업 취소" },
  ],
  COMPLETED: [], FAILED: [], CANCELED: [],
};

function emptyScores(): ScoreForm {
  return Object.fromEntries(scoreItems.map(({ key }) => [key, ""])) as ScoreForm;
}
function emptyObservations(): ObservationForm {
  return Object.fromEntries(scoreItems.map(({ key }) => [key, ""])) as ObservationForm;
}
function timeLabel(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("ko-KR");
}
function localDateTime(value: Date): string {
  return new Date(value.getTime() - value.getTimezoneOffset() * 60_000).toISOString().slice(0, 16);
}
function scoreLabel(value: number): string {
  return ["", "매우 나쁨", "나쁨", "보통", "좋음", "매우 좋음"][value] ?? "";
}

export function CookingWorkspace({ csrf }: Props) {
  const [job, setJob] = useState<CookingJob | null>(null);
  const [current, setCurrent] = useState<CurrentAssessment | null>(null);
  const [processView, setProcessView] = useState<ProcessAssessmentView | null>(null);
  const [assessments, setAssessments] = useState<Assessment[]>([]);
  const [events, setEvents] = useState<JobEvent[]>([]);
  const [lookupId, setLookupId] = useState("");
  const [plannedPortions, setPlannedPortions] = useState(2);
  const [recipeVersion, setRecipeVersion] = useState("synthetic-v1");
  const [thicknessCm, setThicknessCm] = useState("1.20");
  const [placedAt, setPlacedAt] = useState(() => localDateTime(new Date()));
  const [liftedAt, setLiftedAt] = useState(() => localDateTime(new Date()));
  const [batchId, setBatchId] = useState("batch-a-001");
  const [loadingJob, setLoadingJob] = useState(false);
  const [busy, setBusy] = useState(false);
  const activeRequest = useRef(false);
  const loadedJobId = useRef<string | null>(null);
  const loadedJobVersion = useRef<number | null>(null);
  const [error, setError] = useState("");
  const [status, setStatus] = useState("");
  const [safetyReason, setSafetyReason] = useState("");
  const [manualStopConfirmed, setManualStopConfirmed] = useState(false);
  const [probeTemps, setProbeTemps] = useState<string[]>([]);
  const [probeExpired, setProbeExpired] = useState<boolean[]>([]);
  const [simulateGap, setSimulateGap] = useState(false);
  const [scores, setScores] = useState<ScoreForm>(emptyScores);
  const [observations, setObservations] = useState<ObservationForm>(emptyObservations);
  const [actualPortions, setActualPortions] = useState(2);
  const [massKg, setMassKg] = useState("");
  const [overallSatisfaction, setOverallSatisfaction] = useState("");
  const [assessmentKind, setAssessmentKind] = useState<"REVISION" | "REASSESSMENT">("REASSESSMENT");
  const [assessmentReason, setAssessmentReason] = useState("");
  const [recookReason, setRecookReason] = useState("");
  const [transitionReason, setTransitionReason] = useState("");
  const [baselineJobId, setBaselineJobId] = useState("");
  const [comparisonId, setComparisonId] = useState("");
  const [comparison, setComparison] = useState<Comparison | null>(null);
  const [comparisonFreshness, setComparisonFreshness] = useState<"CURRENT" | "STALE" | null>(null);
  const [comparisonChangedJobs, setComparisonChangedJobs] = useState<string[]>([]);
  const [manualStoppedAt, setManualStoppedAt] = useState(() => localDateTime(new Date()));

  const refresh = useCallback(async (id: string) => {
    setLoadingJob(true);
    try {
      const path = jobPath(id);
      const [snapshot, history, audit] = await Promise.all([
        api<JobSnapshot>(path), api<Assessment[]>(path + "/assessments"), api<JobEvent[]>(path + "/events"),
      ]);
      setJob(snapshot.job);
      setCurrent(snapshot.current_assessment);
      setProcessView(snapshot.process_assessment_view);
      setAssessments(history);
      setEvents(audit);
      setLookupId(id);
      if (loadedJobId.current === id && loadedJobVersion.current !== snapshot.job.version) {
        setComparisonFreshness(null);
        setComparisonChangedJobs([]);
      }
      if (loadedJobId.current !== id) {
        setBaselineJobId(snapshot.job.source_job_id ?? "");
        setComparisonId("");
        setComparison(null);
        setComparisonFreshness(null);
        setComparisonChangedJobs([]);
        setProbeTemps(Array(snapshot.job.planned_portions).fill("75.0"));
        setProbeExpired(Array(snapshot.job.planned_portions).fill(false));
        setSimulateGap(false);
        setSafetyReason("");
        setManualStopConfirmed(false);
        setScores(emptyScores());
        setObservations(emptyObservations());
        setActualPortions(snapshot.job.planned_portions);
        setMassKg("");
        setOverallSatisfaction("");
        setAssessmentReason("");
        setRecookReason("");
        setTransitionReason("");
        setManualStoppedAt(localDateTime(new Date()));
        loadedJobId.current = id;
      }
      loadedJobVersion.current = snapshot.job.version;
      window.history.replaceState(null, "", `#job=${encodeURIComponent(id)}`);
    } finally {
      setLoadingJob(false);
    }
  }, []);

  useEffect(() => {
    const match = /^#job=([0-9a-f-]{36})$/i.exec(window.location.hash);
    if (!match) return;
    void refresh(match[1]).catch((cause: unknown) => {
      setError(cause instanceof Error ? cause.message : "작업을 불러오지 못했습니다.");
    });
  }, [refresh]);

  async function action<T>(operation: () => Promise<T>, message: string, nextId?: (value: T) => string) {
    if (activeRequest.current) return;
    activeRequest.current = true;
    setBusy(true);
    setError("");
    setStatus("");
    try {
      const value = await operation();
      const id = nextId ? nextId(value) : job?.job_id;
      if (id) await refresh(id);
      setStatus(message);
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 409 && job) {
        try { await refresh(job.job_id); } catch { /* Keep the original conflict message. */ }
      }
      setError(cause instanceof Error ? cause.message : "요청을 완료하지 못했습니다.");
    } finally {
      activeRequest.current = false;
      setBusy(false);
    }
  }

  async function loadJob(requestedId = lookupId) {
    const id = requestedId.trim();
    if (!/^[0-9a-f-]{36}$/i.test(id)) { setError("작업 ID 형식을 확인해 주세요."); return; }
    setError("");
    setStatus("");
    try { await refresh(id); setStatus("작업을 불러왔습니다."); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "작업을 불러오지 못했습니다."); }
  }

  async function createJob() {
    if (!Number.isInteger(plannedPortions) || plannedPortions < 1 || plannedPortions > 4 || !recipeVersion.trim()) {
      setError("계획 인분은 1~4, 레시피 버전은 필수입니다."); return;
    }
    if (!thicknessCm || !Number.isFinite(Number(thicknessCm)) || Number(thicknessCm) <= 0 || Number(thicknessCm) > 2) {
      setError("합성 제품의 최대 두께는 0 초과~2.00cm로 입력해 주세요. 2.00cm 초과는 프로파일에서 제외됩니다."); return;
    }
    await action(
      () => api<CookingJob>("/api/customer/cooking-jobs", csrf, {
        planned_portions: plannedPortions, recipe_version: recipeVersion.trim(),
        thickness_cm: thicknessCm, equipment_profile: "synthetic-fryer",
      }),
      "합성 조리작업을 생성했습니다.", (created) => created.job_id,
    );
  }

  async function recordTransition(event: TransitionEvent) {
    if (!job) return;
    if (event === "COOKING_CANCELED" && !window.confirm("이 조리작업을 취소하고 종료할까요?")) return;
    if (job.status === "SAFETY_HOLD" && !transitionReason.trim()) {
      setError("안전 보류 작업의 재개·취소 사유를 입력해 주세요."); return;
    }
    const body: { event: TransitionEvent; expected_version: number; reason?: string; actual_placed_at?: string; actual_lifted_at?: string } =
      { event, expected_version: job.version };
    if (job.status === "SAFETY_HOLD") body.reason = transitionReason.trim();
    if (event === "COOKING_STARTED" || event === "COOKING_COMPLETED") {
      const entered = new Date(event === "COOKING_STARTED" ? placedAt : liftedAt);
      if (Number.isNaN(entered.getTime()) || entered.getTime() > Date.now()) {
        setError("투입·인양 시각은 현재 이전의 유효한 시각이어야 합니다."); return;
      }
      if (event === "COOKING_STARTED") body.actual_placed_at = entered.toISOString();
      else {
        if (job.actual_placed_at && entered.getTime() < new Date(job.actual_placed_at).getTime()) {
          setError("인양 시각은 기록된 투입 시각 이후여야 합니다."); return;
        }
        body.actual_lifted_at = entered.toISOString();
      }
    }
    await action(() => api<CookingJob>(jobPath(job.job_id) + "/transitions", csrf, body), "작업 상태와 공정판정을 기록했습니다.");
  }

  async function bindSource() {
    if (!job || !batchId.trim()) { setError("합성 수집 배치 ID를 입력해 주세요."); return; }
    await action(() => api<CookingJob>(jobPath(job.job_id) + "/source-binding", csrf, {
      expected_version: job.version, source_id: "synthetic-rest",
      device_id: "mockprofile-100-a-001", batch_id: batchId.trim(),
    }), "이 조리작업과 합성 수집 배치를 연결했습니다.");
  }

  async function recordSafety(event: "SUSPECTED" | "UNDER_REVIEW" | "CLEARED" | "FAILED") {
    if (!job || !safetyReason.trim()) { setError("안전 조치의 근거를 입력해 주세요."); return; }
    const confirmStop = ["RUNNING", "PAUSED", "SAFETY_HOLD"].includes(job.status) && manualStopConfirmed;
    let stoppedAt: string | undefined;
    if (confirmStop && (event === "SUSPECTED" || !job.manual_stop_confirmed_at)) {
      const entered = new Date(manualStoppedAt);
      if (Number.isNaN(entered.getTime()) || entered.getTime() > Date.now() ||
          (job.actual_placed_at && entered.getTime() < new Date(job.actual_placed_at).getTime())) {
        setError("수동 중단 시각은 재료 투입 이후이면서 현재 이전이어야 합니다."); return;
      }
      stoppedAt = entered.toISOString();
    }
    const partialScores = Object.fromEntries(scoreItems.filter(({ key }) => scores[key] !== "")
      .map(({ key }) => [key, scores[key]]));
    const partialObservations = Object.fromEntries(scoreItems.filter(({ key }) => observations[key].trim())
      .map(({ key }) => [key, observations[key].trim()]));
    await action(() => api<CookingJob>(jobPath(job.job_id) + "/safety/events", csrf, {
      event, expected_version: job.version, reason: safetyReason.trim(), manual_stop_confirmed: confirmStop,
      ...(stoppedAt ? { manual_stopped_at: stoppedAt } : {}),
      ...(event === "SUSPECTED" ? { partial_scores: partialScores, partial_observations: partialObservations } : {}),
    }), "안전 상태와 근거를 기록했습니다.");
  }

  async function checkCore() {
    if (!job) return;
    const temps = probeTemps.map((value) => Number(value));
    if (temps.length !== job.planned_portions || temps.some((value) => !Number.isFinite(value) || value < -273.15 || value > 300)) {
      setError("각 제품의 합성 온도를 -273.15~300°C 범위로 입력해 주세요."); return;
    }
    const probes: Probe[] = temps.map((value, index) => ({
      item_id: `piece-${index + 1}`,
      calibration_status: probeExpired[index] ? "EXPIRED" : "OK",
      samples: Array.from({ length: 61 }, (_, second) => ({ at_second: second, celsius: value.toString() }))
        .filter((sample) => !simulateGap || index !== 0 || sample.at_second !== 30),
    }));
    await action(() => api<CookingJob>(jobPath(job.job_id) + "/safety/core-check", csrf, {
      expected_version: job.version, probes,
    }), "합성 심부온도 표본의 안전 판정을 기록했습니다.");
  }

  async function submitAssessment() {
    if (!job) return;
    const values = Object.values(scores);
    const mass = Number(massKg);
    const load = loadForMass(mass);
    const currentVersion = current?.version ?? 0;
    if (values.some((value) => value === "") || scoreItems.some(({ key }) => !observations[key].trim())) {
      setError("7개 항목의 점수와 관찰 근거를 모두 입력해 주세요."); return;
    }
    if (!load) { setError("실제 투입량은 0.01~0.80 kg이며 소수 둘째 자리까지 입력해 주세요."); return; }
    if (!Number.isInteger(actualPortions) || actualPortions < 1 || actualPortions > 4) {
      setError("실제 인분은 1~4입니다."); return;
    }
    if (currentVersion > 0 && !assessmentReason.trim()) {
      setError("수정·재평가 사유를 입력해 주세요."); return;
    }
    const kind: AssessmentKind = currentVersion === 0 ? "INITIAL" : assessmentKind;
    await action(() => api<Assessment>(jobPath(job.job_id) + "/assessments", csrf, {
      expected_version: job.version, expected_current_version: currentVersion, kind,
      reason: currentVersion === 0 ? null : assessmentReason.trim(), scores, observations,
      overall_satisfaction: overallSatisfaction ? Number(overallSatisfaction) : null,
      scale_version: "tonkatsu-common-v1", planned_portions: job.planned_portions,
      actual_portions: actualPortions, actual_mass_kg: massKg, load_percent: load.percent,
      load_band: load.band,
    }), "완료 평가를 새 버전으로 기록했습니다.");
  }

  async function createRecook() {
    if (!job || !recookReason.trim()) { setError("재조리 사유를 입력해 주세요."); return; }
    await action(() => api<CookingJob>(jobPath(job.job_id) + "/recook", csrf, {
      expected_version: job.version, reason: recookReason.trim(),
    }), "원 작업과 연결된 별도 재조리 작업을 만들었습니다.", (created) => created.job_id);
  }

  async function checkComparison(candidateId: string, id: string): Promise<boolean> {
    try {
      const latest = await api<{ status: "CURRENT" | "STALE"; changed_job_ids: string[] }>(
        jobPath(candidateId) + "/comparisons/" + id + "/status");
      setComparisonFreshness(latest.status);
      setComparisonChangedJobs(latest.changed_job_ids);
      return true;
    } catch {
      setComparisonFreshness(null);
      setComparisonChangedJobs([]);
      return false;
    }
  }

  async function compareJobs() {
    if (!job || !/^[0-9a-f-]{36}$/i.test(baselineJobId.trim())) {
      setError("비교할 이전 작업의 UUID를 입력해 주세요."); return;
    }
    await action(() => api<Comparison>(jobPath(job.job_id) + "/comparisons", csrf,
      { baseline_job_id: baselineJobId.trim() }).then(async (result) => {
        setComparison(result); setComparisonId(result.comparison_id);
        setComparisonFreshness(null); setComparisonChangedJobs([]);
        await checkComparison(job.job_id, result.comparison_id);
        return result;
      }), "현재 유효한 두 평가를 비교하고 결과 버전을 보존했습니다.");
  }

  async function loadComparison() {
    if (!job || !/^[0-9a-f]{64}$/.test(comparisonId.trim())) {
      setError("비교 기록 ID는 64자리 해시여야 합니다."); return;
    }
    setError("");
    try {
      const saved = await api<Comparison>(jobPath(job.job_id) + "/comparisons/" + comparisonId.trim());
      setComparison(saved);
      setComparisonFreshness(null); setComparisonChangedJobs([]);
      const checked = await checkComparison(job.job_id, saved.comparison_id);
      setStatus("저장된 비교 기록을 불러왔습니다." + (checked ? "" : " 최신성 확인은 다시 시도해 주세요."));
    } catch (cause) { setError(cause instanceof Error ? cause.message : "비교 기록을 읽지 못했습니다."); }
  }

  async function refreshComparison() {
    if (!job || !comparison) return;
    const previousId = comparison.comparison_id;
    await action(() => api<Comparison>(jobPath(job.job_id) + "/comparisons/" + previousId + "/refresh", csrf, {})
      .then(async result => {
        setComparison(result); setComparisonId(result.comparison_id);
        setComparisonFreshness(null); setComparisonChangedJobs([]);
        await checkComparison(job.job_id, result.comparison_id);
        return result;
      }), "현재 평가로 다시 비교했습니다. 이전 결과는 보존됩니다.");
  }

  const scoreValues = Object.values(scores).filter((value): value is number => value !== "");
  const previewScore = scoreValues.length === 7 ? (scoreValues.reduce((sum, value) => sum + value, 0) / 35 * 100).toFixed(2) : null;
  const load = loadForMass(Number(massKg));
  const canAssess = !!job && job.status === "COMPLETED" && job.safety_status === "PASSED" && !job.evaluation_blocked;
  const safetyLocked = !!job && ["SUSPECTED", "UNDER_REVIEW", "FAILED"].includes(job.safety_status);
  const availableTransitions = job ? transitions[job.status].filter(({ event }) => !safetyLocked || event === "COOKING_CANCELED") : [];

  return <section className="workspace" aria-labelledby="cooking-title">
    <div className="section-heading">
      <div><p className="eyebrow">조리작업 · 합성 시연</p><h2 id="cooking-title">돈까스 작업·안전·평가</h2></div>
      <span className="tag">장비 제어 없음</span>
    </div>
    <p className="muted">이 화면은 실제 장비 없이 합성 값으로 업무 흐름을 확인합니다. 운영자의 기록과 판정은 장비에 자동 전송되지 않습니다.</p>
    {error && <p role="alert" className="error panel-alert">{error}{error.includes("로그인이") && <> <a href="/api/auth/customer/login">다시 로그인</a></>}</p>}
    {status && <p role="status" className="success panel-alert">{status}</p>}

    <div className="workspace-grid">
      <div className="workflow-main">
        <section className="subcard" aria-labelledby="job-create-title">
          <h3 id="job-create-title">1. 작업 생성·불러오기</h3>
          <div className="field-row">
            <label>계획 인분 <input type="number" min="1" max="4" step="1" value={plannedPortions}
              onChange={(event) => setPlannedPortions(Number(event.target.value))} /></label>
            <label>레시피 버전 <input maxLength={80} value={recipeVersion}
              onChange={(event) => setRecipeVersion(event.target.value)} /></label>
            <label>제품 최대 두께(cm) <input type="number" min="0.01" max="2.00" step="0.01" value={thicknessCm}
              onChange={(event) => setThicknessCm(event.target.value)} /></label>
          </div>
          <button type="button" onClick={createJob} disabled={busy || loadingJob}>합성 조리작업 만들기</button>
          <div className="lookup-row"><label>기존 작업 ID
            <input value={lookupId} onChange={(event) => setLookupId(event.target.value)} placeholder="작업 UUID" />
          </label><button type="button" className="secondary" onClick={() => void loadJob()} disabled={busy || loadingJob}>불러오기</button></div>
        </section>

        {loadingJob && <p role="status">작업을 불러오는 중입니다.</p>}
        {job && <>
          <section className="subcard" aria-labelledby="job-state-title">
            <h3 id="job-state-title">2. 조리 상태</h3>
            <div className="status-grid">
              <div><span className="datum-label">작업 상태</span><strong>{jobLabels[job.status]}</strong></div>
              <div><span className="datum-label">안전 상태</span><strong>{safetyLabels[job.safety_status]}</strong></div>
              <div><span className="datum-label">자동 공정판정</span><strong>{processLabels[processView?.status ?? job.process_assessment_status] ?? "수집값 대기"}</strong></div>
            </div>
            {processView?.view === "LATEST_RESTATED" && <p className="small">늦은 수집값을 반영한 파생 판정입니다. 최초 처리 판정: {processLabels[job.process_assessment_status]}. 원 기록은 유지됩니다.
              {processView.job_version_changed && " 파생 이후 작업 기록이 변경되어 재검토가 필요합니다."}</p>}
            <p className="small">레시피 {job.recipe_version} · 계획 {job.planned_portions}인분 · 마지막 변경 {timeLabel(job.updated_at)}</p>
            <p className="small">두께 {job.thickness_cm ?? "미기록"}cm · {job.thickness_profile ?? "프로파일 없음"}
              {" "}({thicknessLabels[job.process_thickness_status ?? ""] ?? "미판정"})</p>
            <p className="small">실제 조리시간 {job.actual_cooking_seconds == null ? "인양 전" : `${job.actual_cooking_seconds}초`}
              {" "}· 시간 판정 {timeLabels[job.process_time_status ?? "NOT_MEASURED"] ?? "미판정"}</p>
            <p className="small">작업 ID: <code>{job.job_id}</code> · 이 주소를 저장하면 다시 불러올 수 있습니다.</p>
            {job.source_binding ? <p className="small">연결된 합성 수집 배치: <code>{job.source_binding.batch_id}</code> · 온도 판정 {temperatureLabels[job.process_temperature_status] ?? job.process_temperature_status}</p>
              : <div className="binding-box"><p className="small">Mock의 <code>synthetic-rest / mockprofile-100-a-001</code> 배치를 조리 시작 전에 연결해야 수집 온도값으로 자동 공정판정이 실행됩니다.</p>
                <div className="field-row"><label className="grow">합성 배치 ID
                  <input maxLength={80} value={batchId} onChange={(event) => setBatchId(event.target.value)} />
                </label><button type="button" onClick={bindSource} disabled={busy || loadingJob || !["PENDING", "READY"].includes(job.status)}>수집 배치 연결</button></div>
              </div>}
            {job.source_job_id && <p className="small">원 작업 ID: <code>{job.source_job_id}</code> · 재조리 사유: {job.recook_reason}{" "}
              <button type="button" className="text-button" onClick={() => void loadJob(job.source_job_id!)} disabled={busy || loadingJob}>원 작업 열기</button></p>}
            {job.status === "READY" && <div className="field-row"><label>합성 재료 투입 시각
              <input type="datetime-local" value={placedAt} onChange={(event) => setPlacedAt(event.target.value)} />
            </label><button type="button" className="secondary" onClick={() => setPlacedAt(localDateTime(new Date()))}>현재 시각 입력</button></div>}
            {job.status === "RUNNING" && <div className="field-row"><label>합성 바스켓 인양 시각
              <input type="datetime-local" value={liftedAt} onChange={(event) => setLiftedAt(event.target.value)} />
            </label><button type="button" className="secondary" onClick={() => setLiftedAt(localDateTime(new Date()))}>현재 시각 입력</button></div>}
            {(job.status === "READY" || job.status === "RUNNING") &&
              <p className="small">시연용 합성 시각입니다. 실제 조리시간은 인양 시각에서 투입 시각을 뺀 값이며, 두께별 승인 경계로 판정합니다.</p>}
            <div className="actions">
              {availableTransitions.map(({ event, label }) => <button type="button" key={event} onClick={() => recordTransition(event)}
                disabled={busy || loadingJob} className={event === "COOKING_CANCELED" ? "danger" : ""}>{label}</button>)}
              <button type="button" className="secondary" onClick={() => void loadJob(job.job_id)} disabled={busy || loadingJob}>최신 상태 확인</button>
            </div>
            {job.status === "SAFETY_HOLD" && <label>안전 보류 작업의 재개·취소 사유
              <input value={transitionReason} maxLength={500} onChange={(event) => setTransitionReason(event.target.value)}
                placeholder="현장 확인 결과와 운영자 결정" />
            </label>}
            {availableTransitions.length === 0 && <p className="muted">현재 기록할 수 있는 조리 상태 전이가 없습니다.</p>}
          </section>

          <section className="subcard" aria-labelledby="safety-title">
            <h3 id="safety-title">3. 안전 확인</h3>
            {job.safety_status === "SUSPECTED" || job.safety_status === "UNDER_REVIEW" || job.safety_status === "FAILED"
              ? <p className="warning">안전 이상 상태입니다. 평가와 제공 판단을 진행하지 마세요. 장비는 자동 정지되지 않습니다. 운영자가 현장에서 직접 중단하고 확인해야 합니다.</p>
              : null}
            {job.status === "SAFETY_HOLD" && <p className="warning">조리 진행 기록이 보류되었습니다. 현장 수동 중단: {job.manual_stopped_at ? timeLabel(job.manual_stopped_at) : "미확인"}. 의심 해제 후 운영자가 재개 또는 취소를 선택합니다.</p>}
            <div className="field-row">
              <label className="grow">안전 이상·조치 근거
                <input value={safetyReason} maxLength={500} onChange={(event) => setSafetyReason(event.target.value)}
                  placeholder="발견 내용 또는 조사 결과" />
              </label>
            </div>
            <label className="check-line"><input type="checkbox" checked={manualStopConfirmed}
              disabled={!(["RUNNING", "PAUSED", "SAFETY_HOLD"].includes(job.status))}
              onChange={(event) => { setManualStopConfirmed(event.target.checked); if (event.target.checked) setManualStoppedAt(localDateTime(new Date())); }} /> 운영자가 장비에서 수동 중단했음을 확인</label>
            {manualStopConfirmed && ["RUNNING", "PAUSED", "SAFETY_HOLD"].includes(job.status) &&
              (!job.manual_stop_confirmed_at || job.safety_status === "CLEARED") &&
              <label>현장 수동 중단 시각 <input type="datetime-local" value={manualStoppedAt}
                onChange={(event) => setManualStoppedAt(event.target.value)} /></label>}
            <div className="actions">
              {["NOT_MEASURED", "PASSED", "INCOMPLETE", "CLEARED"].includes(job.safety_status) &&
                <button type="button" className="danger" disabled={busy || !safetyReason.trim()}
                  onClick={() => recordSafety("SUSPECTED")}>이상 의심 신고</button>}
              {job.safety_status === "SUSPECTED" &&
                <button type="button" disabled={busy || !safetyReason.trim()} onClick={() => recordSafety("UNDER_REVIEW")}>확인 중으로 전환</button>}
              {["SUSPECTED", "UNDER_REVIEW"].includes(job.safety_status) && <>
                <button type="button" disabled={busy || !safetyReason.trim() ||
                  (job.status === "SAFETY_HOLD" && !job.manual_stop_confirmed_at && !manualStopConfirmed)}
                  onClick={() => recordSafety("CLEARED")}>이상 의심 해제</button>
                <button type="button" className="danger" disabled={busy || !safetyReason.trim()} onClick={() => recordSafety("FAILED")}>안전 실패 확정</button>
              </>}
            </div>
            <div className="divider" />
            <h4>합성 심부온도 표본</h4>
            <p className="small">제품마다 입력 온도로 0~60초의 1Hz 합성 표본 61개를 생성합니다. 실제 측정값으로 표시하지 않습니다. 전 제품의 유효 표본이 75°C 이상이어야 통과합니다.</p>
            <div className="probe-grid">
              {Array.from({ length: job.planned_portions }, (_, index) => <div className="probe-card" key={index}>
                <label>제품 {index + 1} 온도(°C)
                  <input type="number" step="0.1" value={probeTemps[index] ?? "75.0"}
                    onChange={(event) => setProbeTemps((old) => old.map((value, at) => at === index ? event.target.value : value))} />
                </label>
                <label className="check-line"><input type="checkbox" checked={probeExpired[index] ?? false}
                  onChange={(event) => setProbeExpired((old) => old.map((value, at) => at === index ? event.target.checked : value))} /> 탐침 교정 만료 재현</label>
              </div>)}
            </div>
            <label className="check-line"><input type="checkbox" checked={simulateGap}
              onChange={(event) => setSimulateGap(event.target.checked)} /> 첫 제품의 30초 표본 누락 재현</label>
            <button type="button" onClick={checkCore} disabled={busy || job.status !== "COMPLETED" || safetyLocked}>
              합성 표본으로 안전 판정 기록</button>
            {job.status !== "COMPLETED" && <p className="small">조리 완료 후 제품별 안전 확인을 기록할 수 있습니다.</p>}
          </section>

          <section className="subcard" aria-labelledby="assessment-title">
            <h3 id="assessment-title">4. 품질평가</h3>
            <p className="small">공통 척도 v1 · 7항목 동일 가중 · 항목별 1~5점. 초기 척도에는 ‘해당 없음’이 없습니다.</p>
            {job.evaluation_status === "INTERRUPTED" && <p className="warning">안전 이상으로 평가가 중단되었습니다. 종합점수는 미산출이며 신고 당시 입력한 점수·관찰값은 안전 이력에 보존됩니다.</p>}
            {!canAssess && <p className="warning">작업 완료와 안전 통과가 확인되기 전에는 평가를 완료할 수 없습니다. 과거 품질 합격은 현재 제공 허가가 아닙니다.</p>}
            {current && <p className={current.usable ? "small" : "warning"}>
              현재 평가 포인터 v{current.version}: {current.usable ? "사용 가능" : `사용 차단 (${current.blocked_reason ?? "안전 확인 필요"})`}
            </p>}
            <div className="score-grid">
              {scoreItems.map(({ key, label, help }) => <div className="score-row" key={key}>
                <div><strong>{label}</strong><small>{help}</small></div>
                <label>점수 <select value={scores[key]} onChange={(event) => setScores((old) => ({ ...old, [key]: event.target.value ? Number(event.target.value) : "" }))}>
                  <option value="">선택</option>{[1, 2, 3, 4, 5].map((value) => <option key={value} value={value}>{value} · {scoreLabel(value)}</option>)}
                </select></label>
                <label>관찰 근거 <input maxLength={500} value={observations[key]}
                  onChange={(event) => setObservations((old) => ({ ...old, [key]: event.target.value }))} placeholder="관찰한 사실" /></label>
              </div>)}
            </div>
            <p className="preview">{job.evaluation_status === "INTERRUPTED" && job.safety_status !== "CLEARED" && job.safety_status !== "PASSED"
              ? "안전 확인 중 · 종합점수 미산출"
              : previewScore ? `입력 점수 미리보기 ${previewScore}점 / 100점` : "7개 점수를 모두 선택하면 종합점수 미리보기가 표시됩니다."}</p>
            <div className="field-row">
              <label>실제 인분 <input type="number" min="1" max="4" step="1" value={actualPortions}
                onChange={(event) => setActualPortions(Number(event.target.value))} /></label>
              <label>실제 투입량(kg) <input type="number" min="0.01" max="0.8" step="0.01" value={massKg}
                onChange={(event) => setMassKg(event.target.value)} placeholder="0.40" /></label>
              <label>전반적 만족도 <select value={overallSatisfaction} onChange={(event) => setOverallSatisfaction(event.target.value)}>
                <option value="">선택 안 함</option>{[1, 2, 3, 4, 5].map((value) => <option key={value} value={value}>{value}</option>)}
              </select></label>
            </div>
            {load && <p className="small">합성 기준 배치 0.8 kg 대비 적재율 {load.percent}% · {load.band === "PARTIAL" ? "부분 적재" : "기준 적재"}</p>}
            {current && <div className="field-row">
              <label>평가 유형 <select value={assessmentKind} onChange={(event) => setAssessmentKind(event.target.value as "REVISION" | "REASSESSMENT")}>
                <option value="REASSESSMENT">재평가</option><option value="REVISION">수정</option>
              </select></label>
              <label className="grow">수정·재평가 사유 <input maxLength={500} value={assessmentReason}
                onChange={(event) => setAssessmentReason(event.target.value)} /></label>
            </div>}
            <button type="button" onClick={submitAssessment} disabled={busy || !canAssess}>완료 평가 기록</button>
          </section>

          <section className="subcard" aria-labelledby="comparison-title">
            <h3 id="comparison-title">5. 조리 평가 비교</h3>
            <p className="small">같은 메뉴·척도·장비의 두 작업에서 현재 유효한 완료 평가만 비교합니다. 항목 합계가 2점 이상 오르면 ‘개선 관찰’로 기록하지만 레시피 버전 전체의 개선 확정은 아닙니다.</p>
            <div className="field-row"><label className="grow">이전 작업 ID
              <input value={baselineJobId} onChange={(event) => setBaselineJobId(event.target.value)} placeholder="비교할 작업 UUID" />
            </label><button type="button" onClick={compareJobs} disabled={busy || loadingJob || !current?.usable}>현재 평가 비교</button></div>
            <div className="field-row"><label className="grow">저장된 비교 기록 ID
              <input value={comparisonId} onChange={(event) => setComparisonId(event.target.value)} placeholder="64자리 비교 해시" />
            </label><button type="button" className="secondary" onClick={loadComparison} disabled={busy || loadingJob}>기록 불러오기</button></div>
            {comparison && <div className="actions">
              <button type="button" className="secondary" onClick={() => void checkComparison(comparison.candidate_job_id, comparison.comparison_id)
                .then(checked => setStatus(checked ? "조회 시점의 비교 입력을 확인했습니다." : "최신성을 확인하지 못했습니다."))}
                disabled={busy || loadingJob}>결과 최신성 확인</button>
              <button type="button" onClick={refreshComparison} disabled={busy || loadingJob}>현재 평가로 다시 비교</button>
            </div>}
            {comparison && <div className="card">
              <p className={comparisonFreshness === "STALE" ? "warning" : "small"}>결과 최신성: {
                comparisonFreshness === "STALE" ? "입력 변경으로 과거 결과" :
                comparisonFreshness === "CURRENT" ? "조회 시점의 현재 입력과 일치" : "확인 전"}</p>
              {comparisonChangedJobs.length > 0 && <p className="small">변경된 작업: {comparisonChangedJobs.join(", ")}</p>}
              <p><strong>{comparison.improvement_status === "OBSERVED" ? "개선 관찰" : "개선 관찰 기준 미달"}</strong> · 항목 합계 변화 {comparison.score_total_delta >= 0 ? "+" : ""}{comparison.score_total_delta}점</p>
              <p>종합 조리품질 점수 {comparison.baseline_score_percent} → {comparison.candidate_score_percent} / 100점</p>
              <p>품질 합격: 이전 {comparison.baseline_quality_passed ? "합격" : "불합격"} · 이후 {comparison.candidate_quality_passed ? "합격" : "불합격"}</p>
              <p className="small">이전 {comparison.baseline_recipe_version} 평가 v{comparison.baseline_assessment_version} · 이후 {comparison.candidate_recipe_version} 평가 v{comparison.candidate_assessment_version} · {timeLabel(comparison.compared_at)}</p>
              <p className="small">조리량: 이전 {comparison.baseline_actual_portions}인분 · {comparison.baseline_actual_mass_kg}kg · 적재율 {comparison.baseline_load_percent}%, 이후 {comparison.candidate_actual_portions}인분 · {comparison.candidate_actual_mass_kg}kg · 적재율 {comparison.candidate_load_percent}%</p>
              <ul>{scoreItems.map(({ key, label }) => <li key={key}>{label}: {comparison.score_deltas[key] >= 0 ? "+" : ""}{comparison.score_deltas[key]}점</li>)}</ul>
              <p className="small">레시피 전체 개선 확정: 미평가 · 저장된 파생 기록 <code>{comparison.comparison_id}</code></p>
            </div>}
          </section>

          <AssessmentAggregate csrf={csrf} currentJobId={job.job_id} recipeVersion={job.recipe_version}
            defaultLoadBand={assessments.find(item => item.version === current?.version)?.load_band} />

          <section className="subcard" aria-labelledby="recook-title">
            <h3 id="recook-title">7. 재조리 연결</h3>
            <p className="small">재조리는 기존 결과를 덮어쓰지 않고 새 작업으로 생성합니다.</p>
            <div className="field-row"><label className="grow">재조리 사유
              <input value={recookReason} maxLength={500} onChange={(event) => setRecookReason(event.target.value)} />
            </label></div>
            <button type="button" onClick={createRecook} disabled={busy || !["COMPLETED", "FAILED", "CANCELED"].includes(job.status) || !recookReason.trim()}>
              연결된 새 작업 만들기</button>
          </section>
        </>}
      </div>

      <aside className="workflow-aside" aria-label="현재 평가와 이력">
        <section className="subcard"><h3>현재 평가</h3>
          {!job ? <p className="muted">작업을 만들거나 불러오면 상태가 표시됩니다.</p> : !current ?
            <p className="muted">완료된 평가가 없습니다.</p> : <>
              <p className="score-figure">{current.score_percent}<small> / 100점 · v{current.version}</small></p>
              {current.usable && job.safety_status === "PASSED" ? <>
                <p className="success">현재 평가 기록 유효</p>
                <p>{current.quality_target ? "품질 목표 달성" : current.quality_passed ? "품질 합격" : "품질 불합격"}</p>
              </> : <p className="warning">현재 사용 차단 · 과거 합격을 제공 허가로 사용하지 마세요.</p>}
            </>}
        </section>
        <section className="subcard"><h3>평가 이력</h3>
          {assessments.length === 0 ? <p className="muted">평가 이력이 없습니다.</p> : <ol className="history-list">
            {[...assessments].reverse().map((item) => <li key={item.version}>
              <strong>v{item.version} · {item.score_percent}점</strong>
              <small>{item.kind === "INITIAL" ? "최초" : item.kind === "REVISION" ? "수정" : "재평가"} · {timeLabel(item.completed_at)}</small>
              <small>{current?.version === item.version && current.usable ? "현재 평가 기록" : "과거 기록 또는 사용 차단"}</small>
              {item.reason && <small>사유: {item.reason}</small>}
            </li>)}
          </ol>}
        </section>
        <section className="subcard"><h3>작업·안전 이력</h3>
          {events.length === 0 ? <p className="muted">상태 이력이 없습니다.</p> : <ol className="history-list">
            {[...events].reverse().map((item) => <li key={item.sk}>
              <strong>{eventStatusLabel(item.domain, item.from_status)} → {eventStatusLabel(item.domain, item.to_status)}</strong>
              <small>{eventLabels[item.trigger] ?? item.trigger} · {timeLabel(item.occurred_at)}</small>
              {item.reason && <small>{processReasonLabels[item.reason] ?? item.reason}</small>}
              {item.manual_stop_confirmed_at && <small>운영자 수동 중단 확인: {timeLabel(item.manual_stop_confirmed_at)}</small>}
              {item.manual_stopped_at && <small>현장 수동 중단 시각: {timeLabel(item.manual_stopped_at)}</small>}
              {scoreItems.map(({ key, label }) => item.partial_scores?.[key] !== undefined || item.partial_observations?.[key]
                ? <small key={key}>중단 전 {label}: {item.partial_scores?.[key] ?? "점수 미입력"} · {item.partial_observations?.[key] ?? "관찰 미입력"}</small>
                : null)}
            </li>)}
          </ol>}
        </section>
      </aside>
    </div>
  </section>;
}
