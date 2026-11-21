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
  const active = useRef(false);

  async function calculate() {
    if (active.current) return;
    active.current = true;
    setBusy(true); setError(""); setResult(null);
    try {
      const mealIds = ids.split(/[\s,]+/).filter(Boolean);
      const value = await api<Result>("/api/customer/combi-quality-aggregates", csrf, {
        meal_ids: mealIds, menu, equipment_size: size, recipe_version: recipe.trim(), load_band: band,
        period_start: dayBoundary(start, false), period_end: dayBoundary(end, true),
      });
      setResult(value); setGroupId(value.group_id); setResultId(value.result_id);
    } catch (cause) { setError(cause instanceof Error ? cause.message : "집계하지 못했습니다."); }
    finally { active.current = false; setBusy(false); }
  }

  async function open() {
    if (active.current) return;
    active.current = true;
    setBusy(true); setError(""); setResult(null);
    try {
      setResult(await api<Result>(`/api/customer/combi-quality-aggregates/${encodeURIComponent(groupId.trim())}/${encodeURIComponent(resultId.trim())}`));
    } catch (cause) { setError(cause instanceof Error ? cause.message : "집계를 불러오지 못했습니다."); }
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
      <label>집계 그룹 ID<input value={groupId} onChange={event => setGroupId(event.target.value)} /></label>
      <label>결과 ID<input value={resultId} onChange={event => setResultId(event.target.value)} /></label>
    </div>
    <button type="button" className="secondary" disabled={busy || !groupId.trim() || !resultId.trim()} onClick={open}>저장된 집계 조회</button>
    {error && <p role="alert">{error}</p>}
    {result && <div aria-live="polite"><p>저장 시점 스냅샷 · {result.created_at} · 포함 {result.sample_count}건 · 제외 {result.excluded_count}건</p>
      <p>{result.selection.menu === "RICE" ? "밥" : "고등어"} · {result.selection.equipment_size === "MEDIUM_FIXED_RACK_10" ? "중형 고정 랙" : "대형 이동식 랙"} · 레시피 {result.selection.recipe_version} · {({ PARTIAL: "부분 적재", REFERENCE: "기준 적재", OVER_RECOMMENDED: "권장량 초과" } as const)[result.selection.load_band]}</p>
      <p>평가 기간: {new Date(result.selection.period_start).toLocaleString()} 이상 ~ {new Date(result.selection.period_end).toLocaleString()} 미만</p>
      <p>평균 {result.mean_quality_score_percent === null ? "-" : `${result.mean_quality_score_percent}%`} · 품질 합격률 {result.quality_pass_rate_percent === null ? "-" : `${result.quality_pass_rate_percent}%`} · 목표 달성률 {result.quality_target_rate_percent === null ? "-" : `${result.quality_target_rate_percent}%`}</p>
      {result.item_score_means && <ul>{scoreItems.map(item => <li key={item.key}>{item.label}: {result.item_score_means?.[item.key] ?? "-"}</li>)}</ul>}
      {result.references.filter(item => !item.included).length > 0 && <ul>{result.references.filter(item => !item.included).map(item =>
        <li key={item.meal_id}>{item.meal_id}: {reasons[item.exclusion_reason ?? ""] ?? item.exclusion_reason}</li>)}</ul>}
      <p className="small">이 결과는 저장 시점의 선택 표본입니다. 이후 변경은 자동 반영되지 않으며 전체 모집단을 대표하지 않습니다.</p>
    </div>}
  </section>;
}
