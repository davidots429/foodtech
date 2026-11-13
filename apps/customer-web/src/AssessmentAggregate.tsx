import { useEffect, useState } from "react";
import { api, scoreItems, type ScoreKey } from "./cookingApi";

type AggregateResult = {
  group_id: string; result_id: string; sample_count: number; excluded_count: number;
  excluded: { job_id: string; reason: string }[];
  mean_quality_score_percent: string | null;
  item_score_means: Record<ScoreKey, string> | null;
  quality_pass_rate_percent: string | null;
  quality_target_rate_percent: string | null;
  representativeness_status: "NOT_EVALUATED";
  recipe_improvement_status: "NOT_EVALUATED";
  selection: { recipe_version: string; load_band: "PARTIAL" | "REFERENCE"; period_start: string; period_end: string };
};

const reasonLabels: Record<string, string> = {
  DIMENSION_MISMATCH: "메뉴·장비·레시피 버전 불일치",
  JOB_NOT_COMPLETED: "조리작업 미완료",
  SAFETY_NOT_PASSED: "안전 통과 아님",
  EVALUATION_BLOCKED: "평가 사용 차단",
  NO_CURRENT_ASSESSMENT: "현재 평가 없음",
  CURRENT_ASSESSMENT_BLOCKED: "현재 평가 사용 불가",
  NO_COMPLETED_ASSESSMENT: "완료 평가 기록 없음",
  SCALE_OR_PROFILE_MISMATCH: "척도·프로파일 불일치",
  LOAD_BAND_MISMATCH: "조리량 구간 불일치",
  INVALID_COMPLETION_TIME: "평가 완료시각 오류",
  OUTSIDE_PERIOD: "선택 기간 밖",
  INVALID_SCORE_SET: "평가 점수 형식 오류",
};

function dayStart(date: string, nextDay = false): string {
  const [year, month, day] = date.split("-").map(Number);
  return new Date(year, month - 1, day + Number(nextDay)).toISOString();
}

function localDate(date: Date): string {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
}

export function AssessmentAggregate({ csrf, currentJobId, recipeVersion, defaultLoadBand }: {
  csrf: string; currentJobId: string; recipeVersion: string; defaultLoadBand?: "PARTIAL" | "REFERENCE";
}) {
  const today = new Date();
  const [idsText, setIdsText] = useState(currentJobId);
  const [recipe, setRecipe] = useState(recipeVersion);
  const [loadBand, setLoadBand] = useState<"PARTIAL" | "REFERENCE">(defaultLoadBand ?? "PARTIAL");
  const [startDate, setStartDate] = useState(localDate(new Date(today.getFullYear(), today.getMonth(), 1)));
  const [endDate, setEndDate] = useState(localDate(today));
  const [result, setResult] = useState<AggregateResult | null>(null);
  const [groupId, setGroupId] = useState("");
  const [resultId, setResultId] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setIdsText(currentJobId); setRecipe(recipeVersion); setLoadBand(defaultLoadBand ?? "PARTIAL");
    setResult(null); setGroupId(""); setResultId("");
  }, [currentJobId]);

  async function calculate() {
    const ids = idsText.split(/[\s,]+/).map(value => value.trim()).filter(Boolean);
    if (ids.length < 1 || ids.length > 20 || new Set(ids).size !== ids.length ||
        ids.some(value => !/^[0-9a-f-]{36}$/i.test(value)) || !startDate || !endDate || startDate > endDate) {
      setMessage("서로 다른 작업 UUID를 1~20개 입력하고 기간을 확인해 주세요."); return;
    }
    setBusy(true); setMessage("");
    try {
      const next = await api<AggregateResult>("/api/customer/assessment-aggregates", csrf, {
        job_ids: ids, period_start: dayStart(startDate), period_end: dayStart(endDate, true),
        recipe_version: recipe.trim(), load_band: loadBand,
      });
      setResult(next); setGroupId(next.group_id); setResultId(next.result_id);
      setMessage("선택 표본의 기술 통계를 새 파생 결과로 기록했습니다.");
    } catch (error) { setMessage(error instanceof Error ? error.message : "집계를 계산하지 못했습니다."); }
    finally { setBusy(false); }
  }

  async function load() {
    if (!/^[0-9a-f]{64}$/.test(groupId) || !/^[0-9a-f]{64}$/.test(resultId)) {
      setMessage("그룹 ID와 결과 ID를 각각 64자리 해시로 입력해 주세요."); return;
    }
    setBusy(true); setMessage("");
    try {
      setResult(await api<AggregateResult>(`/api/customer/assessment-aggregates/${groupId}/results/${resultId}`));
      setMessage("저장된 집계 결과를 불러왔습니다.");
    } catch (error) { setMessage(error instanceof Error ? error.message : "집계 결과를 읽지 못했습니다."); }
    finally { setBusy(false); }
  }

  return <section className="subcard" aria-labelledby="aggregate-title">
    <h3 id="aggregate-title">6. 선택 표본 품질 집계</h3>
    <p className="small">고객이 지정한 작업만 계산하는 기술 통계입니다. 대표 표본 여부와 레시피 개선 확정은 판정하지 않습니다. 안전상 중단되거나 사용 불가한 평가는 평균에서 제외하고 사유를 남깁니다.</p>
    <label>작업 ID 목록 (쉼표 또는 줄바꿈, 최대 20개)
      <textarea value={idsText} onChange={event => setIdsText(event.target.value)} rows={3} /></label>
    <div className="field-row">
      <label>레시피 버전 <input value={recipe} maxLength={80} onChange={event => setRecipe(event.target.value)} /></label>
      <label>조리량 구간 <select value={loadBand} onChange={event => setLoadBand(event.target.value as "PARTIAL" | "REFERENCE")}>
        <option value="PARTIAL">부분 적재</option><option value="REFERENCE">기준 적재</option></select></label>
      <label>시작일 <input type="date" value={startDate} onChange={event => setStartDate(event.target.value)} /></label>
      <label>종료일 (포함) <input type="date" value={endDate} onChange={event => setEndDate(event.target.value)} /></label>
    </div>
    <button type="button" onClick={calculate} disabled={busy || !recipe.trim()}>선택 표본 집계 기록</button>
    <div className="field-row">
      <label>저장된 그룹 ID <input value={groupId} onChange={event => setGroupId(event.target.value)} /></label>
      <label>저장된 결과 ID <input value={resultId} onChange={event => setResultId(event.target.value)} /></label>
      <button type="button" className="secondary" onClick={load} disabled={busy}>과거 결과 불러오기</button>
    </div>
    {message && <p role="status">{message}</p>}
    {result && <div className="card">
      <p>사용 표본 {result.sample_count}건 · 제외 {result.excluded_count}건 · 평균 종합 조리품질 점수 {result.mean_quality_score_percent ?? "미산출"}</p>
      <p>품질 합격률 {result.quality_pass_rate_percent === null ? "미산출" : `${result.quality_pass_rate_percent}%`} · 목표 달성률 {result.quality_target_rate_percent === null ? "미산출" : `${result.quality_target_rate_percent}%`}</p>
      {result.item_score_means && <ul>{scoreItems.map(({ key, label }) => <li key={key}>{label} 평균 {result.item_score_means![key]} / 5점</li>)}</ul>}
      {result.excluded.length > 0 && <details><summary>제외 작업과 사유</summary><ul>{result.excluded.map(item =>
        <li key={item.job_id}>{item.job_id}: {reasonLabels[item.reason] ?? item.reason}</li>)}</ul></details>}
      <p className="small">대표성 미판정 · 레시피 개선 확정 미판정 · 그룹 <code>{result.group_id}</code> · 결과 <code>{result.result_id}</code></p>
    </div>}
  </section>;
}
