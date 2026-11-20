import { useRef, useState } from "react";
import { api } from "./cookingApi";

type Meal = {
  meal: { meal_batch_id: string; status: string; equipment_size: string };
  rice: { job_id: string; water_ratio_status: string; effective_water_ratio: string;
    target_added_water_kg: string | null; payload_kg: string; gross_kg: string };
  transition: { transition_id: string; mackerel_load_allowed: boolean };
  mackerel: { job_id: string; safety_status: string };
};

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
    {meal && <div className="card" aria-live="polite"><p>식사 배치 <code>{meal.meal.meal_batch_id}</code> · 밥 준비 대기</p>
      <p>밥: 유효 수분비 {meal.rice.effective_water_ratio} · {meal.rice.water_ratio_status === "NORMAL" ? "권장 범위" : "권장 범위 밖"} · 목표 추가 물 {meal.rice.target_added_water_kg ?? "계산 불가"} kg</p>
      <p>내용물·팬 {meal.rice.payload_kg} kg · 랙 포함 {meal.rice.gross_kg} kg</p>
      <p>밥 작업 <code>{meal.rice.job_id}</code> → 모드 전환 <code>{meal.transition.transition_id}</code> → 고등어 작업 <code>{meal.mackerel.job_id}</code></p>
      <p>고등어 투입은 전환 검증 전까지 허용되지 않습니다. 조리·안전·품질 판정은 아직 기록되지 않았습니다.</p>
    </div>}
  </section>;
}
