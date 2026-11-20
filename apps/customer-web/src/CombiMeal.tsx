import { useRef, useState } from "react";
import { api } from "./cookingApi";

type Meal = {
  meal: { meal_batch_id: string; status: string; equipment_size: string; version: number };
  rice: { job_id: string; water_ratio_status: string; effective_water_ratio: string;
    target_added_water_kg: string | null; payload_kg: string; gross_kg: string;
    time_status?: string; program_cooking_duration?: number; thermal_exposure_duration?: number };
  transition: { transition_id: string; mackerel_load_allowed: boolean; status: string;
    humidity_verification_status?: string; dryness_verification_basis?: string; ready_at_second?: number | null };
  mackerel: { job_id: string; safety_status: string };
  events?: { event: string; observed_at: string }[];
};

const mealSteps: Record<string, { event: string; label: string }> = {
  PREPARING_RICE: { event: "RICE_STARTED", label: "밥 조리 시작 기록" },
  RICE_COOKING: { event: "RICE_PROGRAM_COMPLETED", label: "밥 프로그램 완료 기록" },
  RICE_AWAITING_UNLOAD: { event: "RICE_UNLOADED", label: "밥 인양 기록" },
  RICE_UNLOADED: { event: "MODE_TRANSITION_STARTED", label: "건열 전환 시작 기록" },
};
const mealLabels: Record<string, string> = { PREPARING_RICE: "밥 준비", RICE_COOKING: "밥 조리 중",
  RICE_AWAITING_UNLOAD: "밥 인양 대기", RICE_UNLOADED: "밥 인양 완료", MODE_TRANSITION: "건열 전환 중",
  MACKEREL_READY: "고등어 투입 준비", TRANSITION_TIMEOUT: "전환 시간 초과" };

export function CombiMeal({ csrf }: { csrf: string }) {
  const [size, setSize] = useState("MEDIUM_FIXED_RACK_10");
  const [racks, setRacks] = useState("1");
  const [pans, setPans] = useState("10");
  const [dry, setDry] = useState("20");
  const [drained, setDrained] = useState("24");
  const [water, setWater] = useState("20");
  const [panTare, setPanTare] = useState("10");
  const [rackTare, setRackTare] = useState("0");
  const [meal, setMeal] = useState<Meal | null>(null);
  const [lookup, setLookup] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [observedAt, setObservedAt] = useState("");
  const requestActive = useRef(false);

  async function create() {
    if (requestActive.current) return;
    requestActive.current = true;
    setBusy(true); setError("");
    try {
      const result = await api<Meal>("/api/customer/combi-meals", csrf, {
        equipment_size: size, rack_count: Number(racks), pan_count: Number(pans),
        dry_rice_kg: dry, drained_rice_kg: drained, added_water_kg: water,
        pan_tare_kg: panTare, rack_tare_kg: rackTare,
      });
      setMeal(result); setLookup(result.meal.meal_batch_id);
    } catch (cause) { setError(cause instanceof Error ? cause.message : "배치를 생성하지 못했습니다."); }
    finally { requestActive.current = false; setBusy(false); }
  }
  async function open() {
    if (requestActive.current) return;
    requestActive.current = true;
    setBusy(true); setError("");
    try { setMeal(await api<Meal>(`/api/customer/combi-meals/${encodeURIComponent(lookup.trim())}`)); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "배치를 찾지 못했습니다."); }
    finally { requestActive.current = false; setBusy(false); }
  }
  async function recordStep() {
    if (!meal || !mealSteps[meal.meal.status] || requestActive.current) return;
    const observed = new Date(observedAt);
    if (!observedAt || Number.isNaN(observed.getTime())) { setError("단계의 합성 관찰시각을 입력해 주세요."); return; }
    requestActive.current = true; setBusy(true); setError("");
    const path = `/api/customer/combi-meals/${encodeURIComponent(meal.meal.meal_batch_id)}`;
    try {
      await api(path + "/events", csrf, { expected_version: meal.meal.version,
        event: mealSteps[meal.meal.status].event, observed_at: observed.toISOString() });
      setMeal(await api<Meal>(path)); setObservedAt("");
    } catch (cause) { setError(cause instanceof Error ? cause.message : "단계를 기록하지 못했습니다."); }
    finally { requestActive.current = false; setBusy(false); }
  }
  async function simulateTransition(humiditySupported: boolean) {
    if (!meal || requestActive.current) return;
    requestActive.current = true; setBusy(true); setError("");
    const path = `/api/customer/combi-meals/${encodeURIComponent(meal.meal.meal_batch_id)}`;
    const count = humiditySupported ? 30 : 180;
    const samples = Array.from({ length: count }, (_, index) => ({ second: index + 1,
      chamber_celsius: "220", climate_setpoint_percent: "10", door_closed: true,
      critical_alarm: false, humidity_percent: humiditySupported ? "10" : null, vent_event: null }));
    try {
      await api(path + "/transition-evidence", csrf, { expected_version: meal.meal.version,
        humidity_supported: humiditySupported, vent_supported: false, samples });
      setMeal(await api<Meal>(path));
    } catch (cause) { setError(cause instanceof Error ? cause.message : "전환 표본을 기록하지 못했습니다."); }
    finally { requestActive.current = false; setBusy(false); }
  }
  return <section><h2>밥·고등어 합성 식사 배치</h2>
    <p>밥을 인양한 뒤 오븐을 건열로 전환하고 고등어를 조리하는 시연입니다. 장비를 제어하지 않습니다.</p>
    <div className="field-grid">
      <label>장비 규모<select value={size} onChange={event => {
        const large = event.target.value === "LARGE_ROLL_IN_RACK_20";
        setSize(event.target.value); setRacks(large ? "2" : "1"); setPans(large ? "20" : "10");
        setDry(large ? "40" : "20"); setDrained(large ? "48" : "24"); setWater(large ? "40" : "20");
        setPanTare(large ? "20" : "10"); setRackTare(large ? "30" : "0");
      }}><option value="MEDIUM_FIXED_RACK_10">중형 고정 랙</option><option value="LARGE_ROLL_IN_RACK_20">대형 이동식 랙</option></select></label>
      {[["랙 수", racks, setRacks], ["팬 수", pans, setPans], ["건조 쌀 kg", dry, setDry],
        ["배수 후 쌀 kg", drained, setDrained], ["추가 물 kg", water, setWater],
        ["팬 총중량 kg", panTare, setPanTare], ["이동식 랙 총중량 kg", rackTare, setRackTare]].map(([label, value, setter]) =>
        <label key={label as string}>{label as string}<input type="number" min="0" step="0.01" value={value as string}
          onChange={event => (setter as (value: string) => void)(event.target.value)} /></label>)}
    </div>
    <button type="button" onClick={create} disabled={busy}>합성 배치 생성</button>
    <div className="field-row"><label>식사 배치 ID<input value={lookup} onChange={event => setLookup(event.target.value)} /></label>
      <button type="button" className="secondary" onClick={open} disabled={busy || !lookup.trim()}>배치 불러오기</button></div>
    {error && <p role="alert" className="error">{error}</p>}
    {meal && <div className="card" aria-live="polite"><p>식사 배치 <code>{meal.meal.meal_batch_id}</code> · {mealLabels[meal.meal.status] ?? meal.meal.status}</p>
      <p>밥: 유효 수분비 {meal.rice.effective_water_ratio} · {meal.rice.water_ratio_status === "NORMAL" ? "권장 범위" : "권장 범위 밖"} · 목표 추가 물 {meal.rice.target_added_water_kg ?? "계산 불가"} kg</p>
      <p>내용물·팬 {meal.rice.payload_kg} kg · 랙 포함 {meal.rice.gross_kg} kg</p>
      <p>밥 작업 <code>{meal.rice.job_id}</code> → 모드 전환 <code>{meal.transition.transition_id}</code> → 고등어 작업 <code>{meal.mackerel.job_id}</code></p>
      {meal.rice.time_status && <p>밥 프로그램 시간 {meal.rice.program_cooking_duration}초 · 시간 판정 {meal.rice.time_status}
        {meal.rice.thermal_exposure_duration !== undefined && <> · 인양까지 열 노출 {meal.rice.thermal_exposure_duration}초</>}</p>}
      {mealSteps[meal.meal.status] && <div className="field-row"><label>합성 관찰시각
        <input type="datetime-local" step="1" value={observedAt} onChange={event => setObservedAt(event.target.value)} /></label>
        <button type="button" disabled={busy} onClick={recordStep}>{mealSteps[meal.meal.status].label}</button></div>}
      {meal.meal.status === "MODE_TRANSITION" && <div><p>합성 표본으로 전환 조건을 시험합니다. 전환 시작 시각 이후 30초 또는 180초가 경과해야 합니다.</p>
        <button type="button" disabled={busy} onClick={() => simulateTransition(true)}>습도 지원 합성 표본</button>{" "}
        <button type="button" disabled={busy} onClick={() => simulateTransition(false)}>습도 미지원 합성 표본</button></div>}
      <p>고등어 투입: {meal.transition.mackerel_load_allowed ? "허용" : "차단"}
        {meal.transition.humidity_verification_status && <> · 습도 검증 {meal.transition.humidity_verification_status} · 근거 {meal.transition.dryness_verification_basis}</>}</p>
      <p>시간 판정은 전체 공정·안전 판정과 별도입니다. 화면의 전환 자료는 합성 표본입니다.</p>
      {meal.events && <ul>{meal.events.map((event, index) => <li key={index}>{event.event} · {new Date(event.observed_at).toLocaleString()}</li>)}</ul>}
    </div>}
  </section>;
}
