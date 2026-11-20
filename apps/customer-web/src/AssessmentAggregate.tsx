import { useCallback, useEffect, useRef, useState } from "react";
import { api, scoreItems, type ScoreKey } from "./cookingApi";

type Menu = "TONKATSU" | "JEYUK";
type LoadBand = "PARTIAL" | "REFERENCE" | "OVER_RECOMMENDED";
const loadBandLabels: Record<LoadBand, string> = {
  PARTIAL: "부분 적재", REFERENCE: "기준 적재", OVER_RECOMMENDED: "권장량 초과",
};

type AggregateResult = {
  group_id: string; result_id: string; sample_count: number; excluded_count: number;
  excluded: { job_id: string; reason: string }[];
  mean_quality_score_percent: string | null;
  item_score_means: Record<ScoreKey, string> | null;
  quality_pass_rate_percent: string | null;
  quality_target_rate_percent: string | null;
  representativeness_status: "NOT_EVALUATED";
  recipe_improvement_status: "NOT_EVALUATED";
  selection: { menu: Menu; recipe_version: string; load_band: LoadBand; period_start: string; period_end: string };
};
type AggregateGroup = {
  group_id: string; result_id: string | null; updated_at: string | null;
  menu?: Menu; recipe_version: string; load_band: LoadBand;
  period_start: string; period_end: string; job_count: number;
};
type AggregateGroupPage = { items: AggregateGroup[]; next_cursor: string | null };
type AggregateVersion = {
  result_id: string; created_at: string; sample_count: number; excluded_count: number;
  mean_quality_score_percent: string | null;
};
type AggregateVersionPage = {
  items: AggregateVersion[]; next_cursor: string | null; current_result_id: string | null;
};

const reasonLabels: Record<string, string> = {
  DIMENSION_MISMATCH: "메뉴·장비·레시피 버전 불일치",
  JOB_NOT_COMPLETED: "조리작업 미완료",
  SAFETY_NOT_PASSED: "안전 통과 아님",
  JEYUK_FEED_SAFETY_UNVERIFIED: "재료 투입과 안전 표본 일치 미확인",
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

export function AssessmentAggregate({ csrf, currentJobId, menu, equipmentProfile, recipeVersion, defaultLoadBand, availableJobs }: {
  csrf: string; currentJobId: string; menu: Menu; equipmentProfile: string; recipeVersion: string; defaultLoadBand?: LoadBand;
  availableJobs: { job_id: string; menu?: Menu; created_at: string; recipe_version: string; planned_portions: number }[];
}) {
  const today = new Date();
  const [idsText, setIdsText] = useState(currentJobId);
  const [recipe, setRecipe] = useState(recipeVersion);
  const [loadBand, setLoadBand] = useState<LoadBand>(defaultLoadBand ?? "PARTIAL");
  const [startDate, setStartDate] = useState(localDate(new Date(today.getFullYear(), today.getMonth(), 1)));
  const [endDate, setEndDate] = useState(localDate(today));
  const [result, setResult] = useState<AggregateResult | null>(null);
  const [freshness, setFreshness] = useState<"CURRENT" | "STALE" | null>(null);
  const [changedJobIds, setChangedJobIds] = useState<string[]>([]);
  const [groupId, setGroupId] = useState("");
  const [resultId, setResultId] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [groups, setGroups] = useState<AggregateGroup[]>([]);
  const [nextGroupCursor, setNextGroupCursor] = useState<string | null>(null);
  const [loadingGroups, setLoadingGroups] = useState(false);
  const groupListRequest = useRef(0);
  const [versions, setVersions] = useState<AggregateVersion[]>([]);
  const [versionsGroupId, setVersionsGroupId] = useState("");
  const [nextVersionCursor, setNextVersionCursor] = useState<string | null>(null);
  const [currentVersionId, setCurrentVersionId] = useState<string | null>(null);
  const [loadingVersions, setLoadingVersions] = useState(false);
  const versionListRequest = useRef(0);
  const selectedIds = idsText.split(/[\s,]+/).map(value => value.trim()).filter(Boolean);

  const loadGroups = useCallback(async (cursor?: string) => {
    const requestNumber = ++groupListRequest.current;
    setLoadingGroups(true);
    try {
      const page = await api<AggregateGroupPage>(
        `/api/customer/assessment-aggregates/jobs/${currentJobId}/groups?limit=20` +
        (cursor ? "&cursor=" + encodeURIComponent(cursor) : ""));
      if (groupListRequest.current !== requestNumber) return;
      setGroups(previous => cursor ? [...previous, ...page.items] : page.items);
      setNextGroupCursor(page.next_cursor);
    } catch (error) {
      if (groupListRequest.current === requestNumber)
        setMessage(error instanceof Error ? error.message : "관련 집계 그룹을 불러오지 못했습니다.");
    } finally {
      if (groupListRequest.current === requestNumber) setLoadingGroups(false);
    }
  }, [currentJobId]);

  async function loadVersions(selectedGroupId: string, cursor?: string) {
    if (!/^[0-9a-f]{64}$/.test(selectedGroupId)) {
      setMessage("그룹 ID를 64자리 해시로 입력해 주세요."); return;
    }
    const requestNumber = ++versionListRequest.current;
    setLoadingVersions(true);
    try {
      const page = await api<AggregateVersionPage>(
        `/api/customer/assessment-aggregates/${selectedGroupId}/results?limit=20` +
        (cursor ? "&cursor=" + encodeURIComponent(cursor) : ""));
      if (versionListRequest.current !== requestNumber) return;
      setVersions(previous => cursor && versionsGroupId === selectedGroupId ? [...previous, ...page.items] : page.items);
      setVersionsGroupId(selectedGroupId);
      setNextVersionCursor(page.next_cursor);
      setCurrentVersionId(page.current_result_id);
    } catch (error) {
      if (versionListRequest.current === requestNumber)
        setMessage(error instanceof Error ? error.message : "집계 결과 버전을 불러오지 못했습니다.");
    } finally {
      if (versionListRequest.current === requestNumber) setLoadingVersions(false);
    }
  }

  function toggleJob(id: string) {
    const selected = new Set(selectedIds);
    if (selected.has(id)) selected.delete(id);
    else if (selected.size >= 20) { setMessage("집계 표본은 최대 20개까지 선택할 수 있습니다."); return; }
    else selected.add(id);
    setIdsText([...selected].join("\n"));
    setMessage("");
  }

  useEffect(() => {
    groupListRequest.current += 1;
    versionListRequest.current += 1;
    setGroups([]); setNextGroupCursor(null); setLoadingGroups(false);
    setVersions([]); setVersionsGroupId(""); setNextVersionCursor(null);
    setCurrentVersionId(null); setLoadingVersions(false);
    setIdsText(currentJobId); setRecipe(recipeVersion); setLoadBand(defaultLoadBand ?? "PARTIAL");
    setResult(null); setGroupId(""); setResultId("");
    setFreshness(null); setChangedJobIds([]);
    void loadGroups();
  }, [currentJobId, loadGroups]);

  async function checkFreshness(group: string, resultKey: string) {
    try {
      const status = await api<{ status: "CURRENT" | "STALE"; changed_job_ids: string[] }>(
        `/api/customer/assessment-aggregates/${group}/results/${resultKey}/status`);
      setFreshness(status.status); setChangedJobIds(status.changed_job_ids);
      return true;
    } catch {
      setFreshness(null); setChangedJobIds([]);
      return false;
    }
  }

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
        menu, equipment_profile: equipmentProfile, recipe_version: recipe.trim(), load_band: loadBand,
      });
      setResult(next); setGroupId(next.group_id); setResultId(next.result_id);
      setFreshness(null); setChangedJobIds([]);
      const checked = await checkFreshness(next.group_id, next.result_id);
      setMessage("선택 표본의 기술 통계를 기록했습니다." + (checked ? "" : " 최신성 확인은 다시 시도해 주세요."));
      await loadGroups();
      await loadVersions(next.group_id);
    } catch (error) { setMessage(error instanceof Error ? error.message : "집계를 계산하지 못했습니다."); }
    finally { setBusy(false); }
  }

  async function loadVersion(selectedGroupId: string, selectedResultId: string) {
    if (!/^[0-9a-f]{64}$/.test(selectedGroupId) || !/^[0-9a-f]{64}$/.test(selectedResultId)) {
      setMessage("그룹 ID와 결과 ID를 각각 64자리 해시로 입력해 주세요."); return;
    }
    setBusy(true); setMessage("");
    try {
      const saved = await api<AggregateResult>(
        `/api/customer/assessment-aggregates/${selectedGroupId}/results/${selectedResultId}`);
      setResult(saved); setGroupId(saved.group_id); setResultId(saved.result_id);
      setFreshness(null); setChangedJobIds([]);
      const checked = await checkFreshness(saved.group_id, saved.result_id);
      setMessage("저장된 집계 결과를 불러왔습니다." + (checked ? "" : " 최신성 확인은 다시 시도해 주세요."));
      if (versionsGroupId !== selectedGroupId) await loadVersions(selectedGroupId);
    } catch (error) { setMessage(error instanceof Error ? error.message : "집계 결과를 읽지 못했습니다."); }
    finally { setBusy(false); }
  }

  async function loadCurrentFor(selectedGroupId: string) {
    if (!/^[0-9a-f]{64}$/.test(selectedGroupId)) {
      setMessage("그룹 ID를 64자리 해시로 입력해 주세요."); return;
    }
    setBusy(true); setMessage("");
    try {
      const saved = await api<AggregateResult>(`/api/customer/assessment-aggregates/${selectedGroupId}/current`);
      setResult(saved); setGroupId(saved.group_id); setResultId(saved.result_id);
      setFreshness(null); setChangedJobIds([]);
      const checked = await checkFreshness(saved.group_id, saved.result_id);
      setMessage("최신 집계 결과를 불러왔습니다." + (checked ? "" : " 최신성 확인은 다시 시도해 주세요."));
      await loadVersions(saved.group_id);
    } catch (error) { setMessage(error instanceof Error ? error.message : "최신 집계 결과를 읽지 못했습니다."); }
    finally { setBusy(false); }
  }

  async function refresh() {
    if (!result || busy) return;
    setBusy(true); setMessage("");
    try {
      const next = await api<AggregateResult>(
        `/api/customer/assessment-aggregates/${result.group_id}/results/${result.result_id}/refresh`, csrf, {});
      setResult(next); setGroupId(next.group_id); setResultId(next.result_id);
      setFreshness(null); setChangedJobIds([]);
      const checked = await checkFreshness(next.group_id, next.result_id);
      setMessage((next.result_id === result.result_id ? "입력 버전이 같아 기존 결과를 유지했습니다." :
        "현재 평가를 반영한 새 결과 버전을 기록했습니다. 이전 결과는 보존됩니다.") +
        (checked ? "" : " 최신성 확인은 다시 시도해 주세요."));
      await loadGroups();
      await loadVersions(next.group_id);
    } catch (error) { setMessage(error instanceof Error ? error.message : "재계산을 완료하지 못했습니다."); }
    finally { setBusy(false); }
  }

  return <section className="subcard" aria-labelledby="aggregate-title">
    <h3 id="aggregate-title">6. 선택 표본 품질 집계</h3>
    <p className="small">고객이 지정한 작업만 계산하는 기술 통계입니다. 대표 표본 여부와 레시피 개선 확정은 판정하지 않습니다. 안전상 중단되거나 사용 불가한 평가는 평균에서 제외하고 사유를 남깁니다.</p>
    <label>작업 ID 목록 (쉼표 또는 줄바꿈, 최대 20개)
      <textarea value={idsText} onChange={event => setIdsText(event.target.value)} rows={3} /></label>
    {availableJobs.some(item => (item.menu ?? "TONKATSU") === menu) && <fieldset className="job-picker"><legend>내 작업 목록에서 표본 선택</legend>
      {availableJobs.filter(item => (item.menu ?? "TONKATSU") === menu).map(item => <label className="check-line" key={item.job_id}>
        <input type="checkbox" checked={selectedIds.includes(item.job_id)}
          onChange={() => toggleJob(item.job_id)} disabled={busy} />
        {new Date(item.created_at).toLocaleString("ko-KR")} · {item.recipe_version} · {item.planned_portions}인분 · {item.job_id.slice(0, 8)}
      </label>)}
      <p className="small">더 오래된 작업은 화면 상단에서 ‘이전 작업 더 보기’를 누르세요. 목록에 없는 이전 형식의 작업은 ID로 한 번 열거나 위에 직접 입력할 수 있습니다.</p>
    </fieldset>}
    <div className="field-row">
      <label>레시피 버전 <input value={recipe} maxLength={80} onChange={event => setRecipe(event.target.value)} /></label>
      <label>조리량 구간 <select value={loadBand} onChange={event => setLoadBand(event.target.value as LoadBand)}>
        <option value="PARTIAL">부분 적재</option><option value="REFERENCE">기준 적재</option>
        {menu === "JEYUK" && <option value="OVER_RECOMMENDED">권장량 초과</option>}</select></label>
      <label>시작일 <input type="date" value={startDate} onChange={event => setStartDate(event.target.value)} /></label>
      <label>종료일 (포함) <input type="date" value={endDate} onChange={event => setEndDate(event.target.value)} /></label>
    </div>
    <button type="button" onClick={calculate} disabled={busy || !recipe.trim()}>선택 표본 집계 기록</button>
    <div className="field-row">
      <label>저장된 그룹 ID <input value={groupId} onChange={event => setGroupId(event.target.value)} /></label>
      <label>저장된 결과 ID <input value={resultId} onChange={event => setResultId(event.target.value)} /></label>
      <button type="button" className="secondary" onClick={() => void loadCurrentFor(groupId)} disabled={busy}>최신 결과 불러오기</button>
      <button type="button" className="secondary" onClick={() => void loadVersion(groupId, resultId)}
        disabled={busy}>과거 결과 불러오기</button>
    </div>
    <div className="actions"><strong>현재 작업이 포함된 집계 그룹</strong>
      <button type="button" className="secondary" onClick={() => void loadGroups()}
        disabled={loadingGroups}>목록 새로고침</button></div>
    {groups.length === 0 && !loadingGroups && <p className="small">이 작업이 포함된 저장 집계 그룹이 없습니다.</p>}
    {groups.length > 0 && <ul>{groups.map(item => <li key={item.group_id}>
      <button type="button" className="text-button" onClick={() => void loadCurrentFor(item.group_id)}
        disabled={busy || !item.result_id}>
        {item.recipe_version} · {loadBandLabels[item.load_band]} · {item.job_count}개 작업
        {" · "}{item.period_start.slice(0, 10)}부터 {item.period_end.slice(0, 10)} 전까지 · {item.group_id.slice(0, 8)}
      </button>{!item.result_id && <span className="small"> 현재 결과 포인터 없음</span>}
    </li>)}</ul>}
    {nextGroupCursor && <button type="button" className="secondary"
      onClick={() => void loadGroups(nextGroupCursor)} disabled={loadingGroups}>집계 그룹 더 보기</button>}
    <div className="actions"><strong>선택한 그룹의 결과 버전</strong>
      <button type="button" className="secondary" onClick={() => void loadVersions(groupId)}
        disabled={loadingVersions || busy}>버전 목록 불러오기</button></div>
    {versionsGroupId === groupId && versions.length > 0 && <ul>{versions.map(item => <li key={item.result_id}>
      <button type="button" className="text-button" onClick={() => void loadVersion(groupId, item.result_id)}
        disabled={busy}>
        {new Date(item.created_at).toLocaleString("ko-KR")} · {item.sample_count}건 사용 · {item.excluded_count}건 제외
        {" · "}평균 {item.mean_quality_score_percent ?? "미산출"} · {item.result_id.slice(0, 8)}
      </button> {item.result_id === currentVersionId ? "현재 포인터" : "과거 결과"}
    </li>)}</ul>}
    {versionsGroupId === groupId && nextVersionCursor && <button type="button" className="secondary"
      onClick={() => void loadVersions(groupId, nextVersionCursor)} disabled={loadingVersions}>결과 버전 더 보기</button>}
    {result && <div className="actions">
      <button type="button" className="secondary" onClick={() => void checkFreshness(result.group_id, result.result_id)
        .then(checked => setMessage(checked ? "조회 시점의 입력 버전을 확인했습니다." : "최신성을 확인하지 못했습니다."))} disabled={busy}>결과 최신성 확인</button>
      <button type="button" onClick={refresh} disabled={busy}>현재 평가로 다시 계산</button>
    </div>}
    {message && <p role="status">{message}</p>}
    {result && <div className="card">
      <p className={freshness === "STALE" ? "warning" : "small"}>결과 최신성: {freshness === "STALE" ? "원본 변경으로 과거 결과" : freshness === "CURRENT" ? "조회 시점의 현재 입력과 일치" : "확인 전"}</p>
      {changedJobIds.length > 0 && <p className="small">변경된 작업: {changedJobIds.join(", ")}</p>}
      <p>사용 표본 {result.sample_count}건 · 제외 {result.excluded_count}건 · 평균 종합 조리품질 점수 {result.mean_quality_score_percent ?? "미산출"}</p>
      <p>품질 합격률 {result.quality_pass_rate_percent === null ? "미산출" : `${result.quality_pass_rate_percent}%`} · 목표 달성률 {result.quality_target_rate_percent === null ? "미산출" : `${result.quality_target_rate_percent}%`}</p>
      {result.item_score_means && <ul>{scoreItems.map(({ key, label }) => <li key={key}>{label} 평균 {result.item_score_means![key]} / 5점</li>)}</ul>}
      {result.excluded.length > 0 && <details><summary>제외 작업과 사유</summary><ul>{result.excluded.map(item =>
        <li key={item.job_id}>{item.job_id}: {reasonLabels[item.reason] ?? item.reason}</li>)}</ul></details>}
      <p className="small">대표성 미판정 · 레시피 개선 확정 미판정 · 그룹 <code>{result.group_id}</code> · 결과 <code>{result.result_id}</code></p>
    </div>}
  </section>;
}
