import { useCallback, useEffect, useRef, useState } from "react";
import {
  ApiError, api, jobPath, loadForMass, scoreItems,
  type Assessment, type AssessmentKind, type CookingJob, type CurrentAssessment,
  type JobEvent, type JobSnapshot, type Probe, type ScoreKey, type TransitionEvent,
} from "./cookingApi";

type Props = { csrf: string };
type ScoreForm = Record<ScoreKey, number | "">;
type ObservationForm = Record<ScoreKey, string>;

const jobLabels: Record<CookingJob["status"], string> = {
  PENDING: "대기", READY: "준비 완료", RUNNING: "조리 중", PAUSED: "일시 정지",
  COMPLETED: "조리 완료", FAILED: "공정 실패", CANCELED: "취소",
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
};
function eventStatusLabel(domain: string, status: string): string {
  if (domain === "COOKING_JOB_STATUS") return jobLabels[status as CookingJob["status"]] ?? status;
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
function scoreLabel(value: number): string {
  return ["", "매우 나쁨", "나쁨", "보통", "좋음", "매우 좋음"][value] ?? "";
}

export function CookingWorkspace({ csrf }: Props) {
  const [job, setJob] = useState<CookingJob | null>(null);
  const [current, setCurrent] = useState<CurrentAssessment | null>(null);
  const [assessments, setAssessments] = useState<Assessment[]>([]);
  const [events, setEvents] = useState<JobEvent[]>([]);
  const [lookupId, setLookupId] = useState("");
  const [plannedPortions, setPlannedPortions] = useState(2);
  const [recipeVersion, setRecipeVersion] = useState("synthetic-v1");
  const [loadingJob, setLoadingJob] = useState(false);
  const [busy, setBusy] = useState(false);
  const activeRequest = useRef(false);
  const loadedJobId = useRef<string | null>(null);
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

  const refresh = useCallback(async (id: string) => {
    setLoadingJob(true);
    try {
      const path = jobPath(id);
      const [snapshot, history, audit] = await Promise.all([
        api<JobSnapshot>(path), api<Assessment[]>(path + "/assessments"), api<JobEvent[]>(path + "/events"),
      ]);
      setJob(snapshot.job);
      setCurrent(snapshot.current_assessment);
      setAssessments(history);
      setEvents(audit);
      setLookupId(id);
      if (loadedJobId.current !== id) {
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
        loadedJobId.current = id;
      }
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
    await action(
      () => api<CookingJob>("/api/customer/cooking-jobs", csrf, {
        planned_portions: plannedPortions, recipe_version: recipeVersion.trim(), equipment_profile: "synthetic-fryer",
      }),
      "합성 조리작업을 생성했습니다.", (created) => created.job_id,
    );
  }

  async function recordTransition(event: TransitionEvent) {
    if (!job) return;
    if (event === "COOKING_CANCELED" && !window.confirm("이 조리작업을 취소하고 종료할까요?")) return;
    await action(() => api<CookingJob>(jobPath(job.job_id) + "/transitions", csrf, {
      event, expected_version: job.version,
    }), "작업 상태를 기록했습니다.");
  }

  async function recordSafety(event: "SUSPECTED" | "UNDER_REVIEW" | "CLEARED" | "FAILED") {
    if (!job || !safetyReason.trim()) { setError("안전 조치의 근거를 입력해 주세요."); return; }
    await action(() => api<CookingJob>(jobPath(job.job_id) + "/safety/events", csrf, {
      event, expected_version: job.version, reason: safetyReason.trim(), manual_stop_confirmed: manualStopConfirmed,
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
              <div><span className="datum-label">버전</span><strong>{job.version}</strong></div>
            </div>
            <p className="small">레시피 {job.recipe_version} · 계획 {job.planned_portions}인분 · 마지막 변경 {timeLabel(job.updated_at)}</p>
            <p className="small">작업 ID: <code>{job.job_id}</code> · 이 주소를 저장하면 다시 불러올 수 있습니다.</p>
            {job.source_job_id && <p className="small">원 작업 ID: <code>{job.source_job_id}</code> · 재조리 사유: {job.recook_reason}{" "}
              <button type="button" className="text-button" onClick={() => void loadJob(job.source_job_id!)} disabled={busy || loadingJob}>원 작업 열기</button></p>}
            <div className="actions">
              {availableTransitions.map(({ event, label }) => <button type="button" key={event} onClick={() => recordTransition(event)}
                disabled={busy || loadingJob} className={event === "COOKING_CANCELED" ? "danger" : ""}>{label}</button>)}
            </div>
            {availableTransitions.length === 0 && <p className="muted">현재 기록할 수 있는 조리 상태 전이가 없습니다.</p>}
          </section>

          <section className="subcard" aria-labelledby="safety-title">
            <h3 id="safety-title">3. 안전 확인</h3>
            {job.safety_status === "SUSPECTED" || job.safety_status === "UNDER_REVIEW" || job.safety_status === "FAILED"
              ? <p className="warning">안전 이상 상태입니다. 평가와 제공 판단을 진행하지 마세요. 장비 중단은 운영자가 현장에서 직접 확인합니다.</p>
              : null}
            <div className="field-row">
              <label className="grow">안전 이상·조치 근거
                <input value={safetyReason} maxLength={500} onChange={(event) => setSafetyReason(event.target.value)}
                  placeholder="발견 내용 또는 조사 결과" />
              </label>
            </div>
            <label className="check-line"><input type="checkbox" checked={manualStopConfirmed}
              onChange={(event) => setManualStopConfirmed(event.target.checked)} /> 운영자가 장비에서 수동 중단했음을 확인</label>
            <div className="actions">
              {["NOT_MEASURED", "PASSED", "INCOMPLETE", "CLEARED"].includes(job.safety_status) &&
                <button type="button" className="danger" disabled={busy || !safetyReason.trim()}
                  onClick={() => recordSafety("SUSPECTED")}>이상 의심 신고</button>}
              {job.safety_status === "SUSPECTED" &&
                <button type="button" disabled={busy || !safetyReason.trim()} onClick={() => recordSafety("UNDER_REVIEW")}>확인 중으로 전환</button>}
              {["SUSPECTED", "UNDER_REVIEW"].includes(job.safety_status) && <>
                <button type="button" disabled={busy || !safetyReason.trim()} onClick={() => recordSafety("CLEARED")}>이상 의심 해제</button>
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
            <p className="preview">{previewScore ? `입력 점수 미리보기 ${previewScore}점 / 100점` : "7개 점수를 모두 선택하면 종합점수 미리보기가 표시됩니다."}</p>
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

          <section className="subcard" aria-labelledby="recook-title">
            <h3 id="recook-title">5. 재조리 연결</h3>
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
              {item.reason && <small>{item.reason}</small>}
            </li>)}
          </ol>}
        </section>
      </aside>
    </div>
  </section>;
}
