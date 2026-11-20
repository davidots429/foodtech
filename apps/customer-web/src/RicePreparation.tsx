import { useState } from "react";
import { api } from "./cookingApi";

export type RicePreparationRecord = { wash_count: number; soaking_water_celsius: string;
  washed_rice_kg: string | null; soaked_rice_kg: string | null;
  soaking_started_at: string; soaking_completed_at: string; draining_completed_at: string;
  soaking_seconds: string; draining_seconds: string };

export function RicePreparation({ mealId, version, editable, current, csrf, onSaved }: {
  mealId: string; version: number; editable: boolean; current?: RicePreparationRecord;
  csrf: string; onSaved: () => Promise<void> }) {
  const [wash, setWash] = useState("3");
  const [temperature, setTemperature] = useState("20");
  const [washed, setWashed] = useState("");
  const [soakedMass, setSoakedMass] = useState("");
  const [clearMasses, setClearMasses] = useState(false);
  const [start, setStart] = useState("");
  const [soaked, setSoaked] = useState("");
  const [drained, setDrained] = useState("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  async function save() {
    if (busy) return;
    const times = [start, soaked, drained];
    if (times.some(value => !value || Number.isNaN(new Date(value).getTime())) ||
        !/^\d+$/.test(wash) || !temperature || !Number.isFinite(Number(temperature))) {
      setMessage("횟수·온도와 세 시각을 확인해 주세요."); return;
    }
    setBusy(true); setMessage("");
    try {
      await api(`/api/customer/combi-meals/${encodeURIComponent(mealId)}/rice-preparation`, csrf, {
        expected_version: version, wash_count: Number(wash), soaking_water_celsius: temperature,
        ...(clearMasses ? {washed_rice_kg: null, soaked_rice_kg: null} : {
          ...(washed ? {washed_rice_kg: washed} : {}), ...(soakedMass ? {soaked_rice_kg: soakedMass} : {})}),
        soaking_started_at: new Date(start).toISOString(), soaking_completed_at: new Date(soaked).toISOString(),
        draining_completed_at: new Date(drained).toISOString(), reason: reason.trim() || null });
      await onSaved(); setReason(""); setWashed(""); setSoakedMass(""); setClearMasses(false); setMessage("합성 전처리 기록을 저장했습니다.");
    } catch (error) { setMessage(error instanceof Error ? error.message : "전처리를 기록하지 못했습니다."); }
    finally { setBusy(false); }
  }
  return <section className="binding-box"><h4>밥 전처리 기록</h4>
    <p className="small">합성 기준은 세척 3회, 약 20°C 물에서 30분 불림과 5분 배수입니다. 기록 자체는 적합성 판정이 아닙니다.</p>
    {current && <p>세척 {current.wash_count}회 · 불림물 {current.soaking_water_celsius}°C · 불림 {current.soaking_seconds}초 · 배수 {current.draining_seconds}초
      {" "}· 세척 후 {current.washed_rice_kg ?? "미기록"}kg · 불림 후 {current.soaked_rice_kg ?? "미기록"}kg</p>}
    {current && <p className="small">불림 시작 {new Date(current.soaking_started_at).toLocaleString()} · 불림 완료 {new Date(current.soaking_completed_at).toLocaleString()}
      {" "}· 배수 완료 {new Date(current.draining_completed_at).toLocaleString()}</p>}
    {editable && <><div className="field-grid">
      <label>세척 횟수<input type="number" min="0" max="20" step="1" value={wash} onChange={event => setWash(event.target.value)} /></label>
      <label>불림물 온도(°C)<input type="number" min="0" max="100" step="0.01" value={temperature} onChange={event => setTemperature(event.target.value)} /></label>
      <label>세척 후 중량(kg)<input type="number" min="0.01" step="0.01" value={washed} disabled={clearMasses} onChange={event => setWashed(event.target.value)} /></label>
      <label>불림 후 중량(kg)<input type="number" min="0.01" step="0.01" value={soakedMass} disabled={clearMasses} onChange={event => setSoakedMass(event.target.value)} /></label>
      <label><input type="checkbox" checked={clearMasses} onChange={event => {setClearMasses(event.target.checked); setWashed(""); setSoakedMass("");}} /> 기존 세척·불림 후 중량 삭제</label>
      <label>불림 시작<input type="datetime-local" step="1" value={start} onChange={event => setStart(event.target.value)} /></label>
      <label>불림 완료<input type="datetime-local" step="1" value={soaked} onChange={event => setSoaked(event.target.value)} /></label>
      <label>배수 완료<input type="datetime-local" step="1" value={drained} onChange={event => setDrained(event.target.value)} /></label>
      <label>수정 사유<input maxLength={500} value={reason} onChange={event => setReason(event.target.value)} /></label>
    </div><p className="small">중량 입력을 비워두면 기존 기록을 유지합니다.</p>
    <button type="button" disabled={busy} onClick={() => void save()}>전처리 기록</button></>}
    {message && <p role="status">{message}</p>}
  </section>;
}
