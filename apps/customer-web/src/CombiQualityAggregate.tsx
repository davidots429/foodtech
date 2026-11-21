import { useRef, useState } from "react";
import { api, scoreItems, type ScoreKey } from "./cookingApi";

type Menu = "RICE" | "MACKEREL";
type LoadBand = "PARTIAL" | "REFERENCE" | "OVER_RECOMMENDED";
type Result = {
  group_id: string; result_id: string; created_at: string; snapshot_status: "HISTORICAL";
  sample_count: number; excluded_count: number; mean_quality_score_percent: string | null;
  quality_pass_rate_percent: string | null; quality_target_rate_percent: string | null;
  item_score_means: Record<ScoreKey, string> | null;
  selection: { menu: Menu; equipment_size: string; recipe_version: string; load_band: LoadBand;
    period_start: string; period_end: string };
  references: { meal_id: string; included: boolean; exclusion_reason: string | null }[];
};
type Freshness = { status: "CHANGED" | "UNCHANGED_AS_OBSERVED"; checked_at: string;
  changed: { meal_id: string; reason: "UNAVAILABLE" | "VERSION_CHANGED" }[] };
type VersionPage = { items: { result_id: string; created_at: string; sample_count: number;
  excluded_count: number; mean_quality_score_percent: string | null }[]; next_cursor: string | null };
type GroupPage = { items: { group_id: string; selection: Result["selection"] }[]; next_cursor: string | null };
type Comparison = { baseline: { sample_count: number; freshness: Freshness; selection: Result["selection"] };
  candidate: { sample_count: number; freshness: Freshness; selection: Result["selection"] };
  deltas: { mean_quality_score_percent: string; quality_pass_rate_percent: string; quality_target_rate_percent: string };
  item_score_deltas: Record<ScoreKey, string> };

const reasons: Record<string, string> = {
  NO_CURRENT_ASSESSMENT: "현재 평가 없음", CURRENT_ASSESSMENT_BLOCKED: "안전 또는 평가 사용 불가",
  MISSING_RECIPE_VERSION: "레시피 버전 미기록", MISSING_ACTUAL_MASS: "실제 중량 미기록",
  DIMENSION_MISMATCH: "장비 또는 레시피 불일치", LOAD_BAND_MISMATCH: "조리량 구간 불일치",
  OUTSIDE_PERIOD: "선택 기간 밖",
};

function localDate(date: Date): string {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
}

function dayBoundary(date: string, following: boolean): string {
  const [year, month, day] = date.split("-").map(Number);
  return new Date(year, month - 1, day + Number(following)).toISOString();
}

export function CombiQualityAggregate({ csrf, mealId, size, riceRecipe, mackerelRecipe }: {
  csrf: string; mealId: string; size: string; riceRecipe?: string; mackerelRecipe?: string;
}) {
  const today = new Date();
  const [ids, setIds] = useState(mealId);
  const [menu, setMenu] = useState<Menu>("RICE");
  const [recipe, setRecipe] = useState(riceRecipe ?? "");
  const [band, setBand] = useState<LoadBand>("PARTIAL");
  const [start, setStart] = useState(localDate(new Date(today.getFullYear(), today.getMonth(), 1)));
  const [end, setEnd] = useState(localDate(today));
  const [result, setResult] = useState<Result | null>(null);
  const [groupId, setGroupId] = useState("");
  const [resultId, setResultId] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [freshness, setFreshness] = useState<Freshness | null>(null);
  const [baselineGroup, setBaselineGroup] = useState("");
  const [baselineResult, setBaselineResult] = useState("");
  const [comparison, setComparison] = useState<Comparison | null>(null);
  const [versions, setVersions] = useState<VersionPage["items"]>([]);
  const [versionsGroup, setVersionsGroup] = useState("");
  const [versionsCursor, setVersionsCursor] = useState<string | null>(null);
  const [groups, setGroups] = useState<GroupPage["items"]>([]);
  const [groupsLoaded, setGroupsLoaded] = useState(false);
  const [groupsCursor, setGroupsCursor] = useState<string | null>(null);
  const [refreshMessage, setRefreshMessage] = useState("");
  const active = useRef(false);

  async function calculate() {
    if (active.current) return;
    active.current = true;
    setBusy(true); setError(""); setResult(null); setFreshness(null); setComparison(null);
    try {
      const mealIds = ids.split(/[\s,]+/).filter(Boolean);
      const value = await api<Result>("/api/customer/combi-quality-aggregates", csrf, {
        meal_ids: mealIds, menu, equipment_size: size, recipe_version: recipe.trim(), load_band: band,
        period_start: dayBoundary(start, false), period_end: dayBoundary(end, true),
      });
      setResult(value); setGroupId(value.group_id); setResultId(value.result_id);
      setVersions([]); setVersionsGroup(""); setVersionsCursor(null);
    } catch (cause) { setError(cause instanceof Error ? cause.message : "집계하지 못했습니다."); }
    finally { active.current = false; setBusy(false); }
  }

  async function open() {
    if (active.current) return;
    active.current = true;
    setBusy(true); setError(""); setResult(null); setFreshness(null); setComparison(null);
    try {
      setResult(await api<Result>(`/api/customer/combi-quality-aggregates/${encodeURIComponent(groupId.trim())}/${encodeURIComponent(resultId.trim())}`));
    } catch (cause) { setError(cause instanceof Error ? cause.message : "집계를 불러오지 못했습니다."); }
    finally { active.current = false; setBusy(false); }
  }

  async function checkFreshness() {
    if (active.current || !result) return;
    active.current = true; setBusy(true); setError(""); setFreshness(null);
    try {
      setFreshness(await api<Freshness>(`/api/customer/combi-quality-aggregates/${encodeURIComponent(result.group_id)}/${encodeURIComponent(result.result_id)}/freshness`));
    } catch (cause) { setError(cause instanceof Error ? cause.message : "배치 변경 여부를 확인하지 못했습니다."); }
    finally { active.current = false; setBusy(false); }
  }

  async function recalculate() {
    if (active.current || !result) return;
    active.current = true; setBusy(true); setError(""); setFreshness(null); setComparison(null);
    try {
      const value = await api<Result>(`/api/customer/combi-quality-aggregates/${encodeURIComponent(result.group_id)}/${encodeURIComponent(result.result_id)}/recalculate`, csrf, {});
      setResult(value); setGroupId(value.group_id); setResultId(value.result_id);
    } catch (cause) { setError(cause instanceof Error ? cause.message : "다시 집계하지 못했습니다."); }
    finally { active.current = false; setBusy(false); }
  }

  async function compare() {
    if (active.current || !result) return;
    active.current = true; setBusy(true); setError(""); setComparison(null);
    try {
      setComparison(await api<Comparison>('/api/customer/combi-quality-aggregates/compare', csrf, {
        baseline_group_id: baselineGroup.trim(), baseline_result_id: baselineResult.trim(),
        candidate_group_id: result.group_id, candidate_result_id: result.result_id,
      }));
    } catch (cause) { setError(cause instanceof Error ? cause.message : "집계 결과를 비교하지 못했습니다."); }
    finally { active.current = false; setBusy(false); }
  }

  async function listVersions(cursor?: string) {
    if (active.current || !groupId.trim()) return;
    active.current = true; setBusy(true); setError("");
    const group = groupId.trim();
    try {
      const page = await api<VersionPage>(`/api/customer/combi-quality-aggregates/groups/${encodeURIComponent(group)}/results?limit=20` +
        (cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""));
      setVersions(previous => cursor && versionsGroup === group ? [...previous, ...page.items] : page.items);
      setVersionsGroup(group); setVersionsCursor(page.next_cursor);
    } catch (cause) { setError(cause instanceof Error ? cause.message : "집계 목록을 불러오지 못했습니다."); }
    finally { active.current = false; setBusy(false); }
  }

  async function listGroups(cursor?: string) {
    if (active.current) return;
    active.current = true; setBusy(true); setError("");
    try {
      const page = await api<GroupPage>(`/api/customer/combi-quality-aggregates/meals/${encodeURIComponent(mealId)}/groups?limit=20` +
        (cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""));
      setGroups(previous => cursor ? [...previous, ...page.items] : page.items);
      setGroupsCursor(page.next_cursor); setGroupsLoaded(true);
    } catch (cause) { setError(cause instanceof Error ? cause.message : "집계 그룹을 불러오지 못했습니다."); }
    finally { active.current = false; setBusy(false); }
  }

  async function refreshPending() {
    if (active.current) return;
    active.current = true; setBusy(true); setError(""); setRefreshMessage("");
    try {
      const response = await api<{ status: "NO_PENDING" | "REFRESHED"; refreshed_group_count: number }>(
        `/api/customer/combi-quality-aggregates/meals/${encodeURIComponent(mealId)}/refresh-pending`, csrf, {});
      setRefreshMessage(response.status === "NO_PENDING" ? "처리할 변경 사항이 없습니다." :
        `연결된 집계 ${response.refreshed_group_count}개를 다시 계산했습니다.`);
      setVersions([]); setVersionsGroup(""); setVersionsCursor(null); setFreshness(null); setComparison(null);
    } catch (cause) { setError(cause instanceof Error ? cause.message : "대기 중인 집계를 처리하지 못했습니다."); }
    finally { active.current = false; setBusy(false); }
  }

  return <section className="binding-box"><h3>밥·고등어 품질 선택 표본 집계</h3>
    <p>같은 메뉴·장비·레시피·조리량 구간의 평가를 비교합니다. 합성 기준이며 선택한 표본만 집계합니다.</p>
    <p>장비 규모: {size === "MEDIUM_FIXED_RACK_10" ? "중형 고정 랙" : "대형 이동식 랙"}</p>
    <label>식사 배치 ID (쉼표 또는 줄바꿈으로 최대 20개)<textarea value={ids} onChange={event => setIds(event.target.value)} /></label>
    <div className="field-grid">
      <label>메뉴<select value={menu} onChange={event => {
        const next = event.target.value as Menu; setMenu(next); setRecipe(next === "RICE" ? riceRecipe ?? "" : mackerelRecipe ?? "");
      }}><option value="RICE">밥</option><option value="MACKEREL">고등어</option></select></label>
      <label>레시피 버전<input maxLength={80} value={recipe} onChange={event => setRecipe(event.target.value)} /></label>
      <label>조리량 구간<select value={band} onChange={event => setBand(event.target.value as LoadBand)}>
        <option value="PARTIAL">부분 적재</option><option value="REFERENCE">기준 적재</option><option value="OVER_RECOMMENDED">권장량 초과</option>
      </select></label>
      <label>시작일<input type="date" value={start} onChange={event => setStart(event.target.value)} /></label>
      <label>종료일<input type="date" value={end} onChange={event => setEnd(event.target.value)} /></label>
    </div>
    <button type="button" disabled={busy || !ids.trim() || !recipe.trim() || !start || !end} onClick={calculate}>품질 집계 저장</button>
    <div className="field-grid">
      <label>집계 그룹 ID<input value={groupId} onChange={event => { setGroupId(event.target.value); setVersions([]); setVersionsCursor(null); setVersionsGroup(""); }} /></label>
      <label>결과 ID<input value={resultId} onChange={event => setResultId(event.target.value)} /></label>
    </div>
    <button type="button" className="secondary" disabled={busy || !groupId.trim() || !resultId.trim()} onClick={open}>저장된 집계 조회</button>
    <button type="button" className="secondary" disabled={busy} onClick={() => listGroups()}>이 배치의 집계 그룹 목록</button>
    <button type="button" className="secondary" disabled={busy} onClick={refreshPending}>이 배치의 변경 사항 재집계</button>
    {refreshMessage && <p role="status">{refreshMessage}</p>}
    {groupsLoaded && <div><p>집계 그룹 {groups.length}건</p><ul>{groups.map(group => <li key={group.group_id}>
      <button type="button" className="secondary" disabled={busy} onClick={() => { setGroupId(group.group_id); setResultId(""); setVersions([]); setVersionsGroup(""); setVersionsCursor(null); }}>그룹 선택</button>
      {" "}{group.selection.menu === "RICE" ? "밥" : "고등어"} · {group.selection.recipe_version} · {group.selection.load_band}
    </li>)}</ul>{groupsCursor && <button type="button" className="secondary" disabled={busy} onClick={() => listGroups(groupsCursor)}>그룹 더 보기</button>}</div>}
    <button type="button" className="secondary" disabled={busy || !groupId.trim()} onClick={() => listVersions()}>이 그룹의 저장 결과 목록</button>
    {versionsGroup && <div><p>저장 결과 {versions.length}건</p><ul>{versions.map(version => <li key={version.result_id}>
      <button type="button" className="secondary" disabled={busy} onClick={() => setResultId(version.result_id)}>선택</button>
      {" "}{version.created_at} · 포함 {version.sample_count}건 · 제외 {version.excluded_count}건 · {version.result_id}
    </li>)}</ul>{versionsCursor && <button type="button" className="secondary" disabled={busy} onClick={() => listVersions(versionsCursor)}>목록 더 보기</button>}</div>}
    {error && <p role="alert">{error}</p>}
    {result && <div aria-live="polite"><p>저장 시점 스냅샷 · {result.created_at} · 포함 {result.sample_count}건 · 제외 {result.excluded_count}건</p>
      <p>{result.selection.menu === "RICE" ? "밥" : "고등어"} · {result.selection.equipment_size === "MEDIUM_FIXED_RACK_10" ? "중형 고정 랙" : "대형 이동식 랙"} · 레시피 {result.selection.recipe_version} · {({ PARTIAL: "부분 적재", REFERENCE: "기준 적재", OVER_RECOMMENDED: "권장량 초과" } as const)[result.selection.load_band]}</p>
      <p>평가 기간: {new Date(result.selection.period_start).toLocaleString()} 이상 ~ {new Date(result.selection.period_end).toLocaleString()} 미만</p>
      <p>평균 {result.mean_quality_score_percent === null ? "-" : `${result.mean_quality_score_percent}%`} · 품질 합격률 {result.quality_pass_rate_percent === null ? "-" : `${result.quality_pass_rate_percent}%`} · 목표 달성률 {result.quality_target_rate_percent === null ? "-" : `${result.quality_target_rate_percent}%`}</p>
      {result.item_score_means && <ul>{scoreItems.map(item => <li key={item.key}>{item.label}: {result.item_score_means?.[item.key] ?? "-"}</li>)}</ul>}
      {result.references.filter(item => !item.included).length > 0 && <ul>{result.references.filter(item => !item.included).map(item =>
        <li key={item.meal_id}>{item.meal_id}: {reasons[item.exclusion_reason ?? ""] ?? item.exclusion_reason}</li>)}</ul>}
      <button type="button" className="secondary" disabled={busy} onClick={checkFreshness}>배치 변경 여부 확인</button>
      <button type="button" className="secondary" disabled={busy} onClick={recalculate}>같은 조건으로 다시 집계</button>
      {freshness && <div><p>{freshness.status === "CHANGED" ? "집계 후 변경된 배치가 있습니다." : "확인 시점에 배치 버전이 변경되지 않았습니다."} · {freshness.checked_at}</p>
        {freshness.changed.length > 0 && <ul>{freshness.changed.map(item => <li key={item.meal_id}>{item.meal_id}: {item.reason === "UNAVAILABLE" ? "배치를 찾을 수 없음" : "배치 버전 변경"}</li>)}</ul>}</div>}
      <div className="field-grid"><label>기준 집계 그룹 ID<input value={baselineGroup} onChange={event => setBaselineGroup(event.target.value)} /></label>
        <label>기준 결과 ID<input value={baselineResult} onChange={event => setBaselineResult(event.target.value)} /></label></div>
      <button type="button" className="secondary" disabled={busy || !baselineGroup.trim() || !baselineResult.trim()} onClick={compare}>현재 결과와 비교</button>
      {comparison && <div><p>기준 {comparison.baseline.sample_count}건 · 비교 {comparison.candidate.sample_count}건</p>
        <p>레시피 {comparison.baseline.selection.recipe_version} → {comparison.candidate.selection.recipe_version}</p>
        <p>기준 기간: {new Date(comparison.baseline.selection.period_start).toLocaleString()} 이상 ~ {new Date(comparison.baseline.selection.period_end).toLocaleString()} 미만</p>
        <p>비교 기간: {new Date(comparison.candidate.selection.period_start).toLocaleString()} 이상 ~ {new Date(comparison.candidate.selection.period_end).toLocaleString()} 미만</p>
        <p>평균 점수 차이 {comparison.deltas.mean_quality_score_percent}%p · 합격률 차이 {comparison.deltas.quality_pass_rate_percent}%p · 목표 달성률 차이 {comparison.deltas.quality_target_rate_percent}%p</p>
        <ul>{scoreItems.map(item => <li key={item.key}>{item.label}: {comparison.item_score_deltas[item.key]}</li>)}</ul>
        {(comparison.baseline.freshness.status === "CHANGED" || comparison.candidate.freshness.status === "CHANGED") &&
          <p role="alert">집계 이후 변경되거나 없어진 배치가 포함돼 있습니다.</p>}
        <p className="small">선택 표본의 단순 차이이며 레시피가 차이를 유발했음을 뜻하지 않습니다.</p>
      </div>}
      <p className="small">이 결과는 저장 시점의 선택 표본입니다. 이후 변경은 자동 반영되지 않으며 전체 모집단을 대표하지 않습니다.</p>
    </div>}
  </section>;
}
