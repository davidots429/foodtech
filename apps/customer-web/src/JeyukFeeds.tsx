import { useState } from "react";
import { api, jobPath, type CookingJob } from "./cookingApi";

const names = { meat: "고기", vegetable: "채소", sauce: "소스" } as const;
type Ingredient = keyof typeof names;
const displayTime = (value: string) => new Date(value).toLocaleString();

export function JeyukFeeds({ job, csrf, onSaved }: { job: CookingJob; csrf: string; onSaved: () => Promise<void> }) {
  const [ingredient, setIngredient] = useState<Ingredient>("meat");
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [mass, setMass] = useState("");
  const [temperature, setTemperature] = useState("");
  const [observed, setObserved] = useState("");
  const [clearTemperature, setClearTemperature] = useState(false);
  const [discharge, setDischarge] = useState("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  async function save(kind: "feed" | "discharge") {
    if (busy) return;
    const times = kind === "feed" ? [start, end] : [discharge];
    if (times.some(value => !value || Number.isNaN(new Date(value).getTime())) ||
        (kind === "feed" && ((!clearTemperature && (!!temperature !== !!observed)) ||
          (clearTemperature && (!!temperature || !!observed)) ||
          (observed && Number.isNaN(new Date(observed).getTime())))) ||
        (kind === "feed" && (!mass || !Number.isFinite(Number(mass)) || Number(mass) <= 0))) {
      setMessage("시각과 투입량을 확인해 주세요."); return;
    }
    setBusy(true); setMessage("");
    try {
      await api(jobPath(job.job_id) + (kind === "feed" ? `/jeyuk-feeds/${ingredient}` : "/jeyuk-discharge"), csrf,
        kind === "feed" ? { expected_version: job.version, started_at: new Date(start).toISOString(),
          completed_at: new Date(end).toISOString(), mass_kg: mass, reason: reason.trim() || null,
          ...(clearTemperature ? {initial_temperature_c: null, temperature_observed_at: null} : temperature
            ? {initial_temperature_c: temperature, temperature_observed_at: new Date(observed).toISOString()} : {}) }
          : { expected_version: job.version, completed_at: new Date(discharge).toISOString(), reason: reason.trim() || null });
      await onSaved(); setReason(""); setTemperature(""); setObserved(""); setClearTemperature(false); setMessage("합성 기록을 저장했습니다.");
    } catch (error) { setMessage(error instanceof Error ? error.message : "기록을 저장하지 못했습니다."); }
    finally { setBusy(false); }
  }
  return <section className="binding-box"><h4>재료 투입과 배출</h4>
    <p className="small">합성 시연 기록입니다. 안전 확인 시 마지막 채소·소스 투입과 수집 표본의 시각을 대조합니다.</p>
    {job.jeyuk_feed_safety_aligned === true ? <p className="small">재료 투입과 수집 안전 표본의 시각이 일치합니다.</p>
      : <p className="small">재료 투입과 수집 안전 표본의 일치가 아직 확인되지 않았습니다.</p>}
    {Object.entries(names).map(([key, label]) => {
      const feed = job.jeyuk_feeds?.[key.toUpperCase()];
      return <p className="small" key={key}>{label}: {feed ? `${displayTime(feed.started_at)} ~ ${displayTime(feed.completed_at)} · ${feed.mass_kg}kg · 초기 온도 ${feed.initial_temperature_c != null && feed.temperature_observed_at ? `${feed.initial_temperature_c}°C (${displayTime(feed.temperature_observed_at)})` : "미기록"}` : "미기록"}</p>;
    })}
    {job.jeyuk_feed_mass_status && <p className="small">합계 {job.jeyuk_feed_total_kg}kg / 적재량 {job.actual_load_kg}kg · {({INCOMPLETE: "투입 기록 미완료", MATCHED: "합계 일치", MISMATCH: "합계 불일치"})[job.jeyuk_feed_mass_status]}
      {job.jeyuk_additions_simultaneous != null && ` · 채소·소스 시작 간격 ${job.jeyuk_additions_simultaneous ? "30초 이내" : "30초 초과"}`}</p>}
    {job.status !== "CANCELED" && <><div className="field-grid">
      <label>재료 <select value={ingredient} onChange={event => setIngredient(event.target.value as Ingredient)}>{Object.entries(names).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label>
      <label>투입 시작 <input type="datetime-local" step="1" value={start} onChange={event => setStart(event.target.value)} /></label>
      <label>투입 완료 <input type="datetime-local" step="1" value={end} onChange={event => setEnd(event.target.value)} /></label>
      <label>투입량(kg) <input type="number" min="0.001" step="0.001" value={mass} onChange={event => setMass(event.target.value)} /></label>
      <label>재료 초기 온도(°C) <input type="number" step="0.01" value={temperature} onChange={event => setTemperature(event.target.value)} disabled={clearTemperature} /></label>
      <label>온도 관찰 시각 <input type="datetime-local" step="1" value={observed} onChange={event => setObserved(event.target.value)} disabled={clearTemperature} /></label>
      <label><input type="checkbox" checked={clearTemperature} onChange={event => {setClearTemperature(event.target.checked); setTemperature(""); setObserved("");}} /> 기존 초기 온도 기록 삭제</label>
      <label>수정 사유 <input maxLength={500} value={reason} onChange={event => setReason(event.target.value)} /></label>
    </div><p className="small">온도와 시각을 비워두면 기존 온도 기록을 유지합니다. 합성 입력은 적합성 판정이 아닙니다.</p>
    <button type="button" disabled={busy} onClick={() => void save("feed")}>투입 기록</button></>}
    {job.jeyuk_discharge && <p className="small">배출 완료 {displayTime(job.jeyuk_discharge.completed_at)} · 배출 {job.jeyuk_discharge.duration_seconds}초 · 전체 {job.jeyuk_discharge.total_process_duration_seconds ?? "미확인"}초</p>}
    {job.actual_lifted_at && ["COMPLETED", "FAILED"].includes(job.status) && <div className="field-row"><label>배출 완료 <input type="datetime-local" step="1" value={discharge} onChange={event => setDischarge(event.target.value)} /></label>
      <button type="button" disabled={busy} onClick={() => void save("discharge")}>배출 완료 기록</button></div>}
    {message && <p role="status">{message}</p>}
  </section>;
}
