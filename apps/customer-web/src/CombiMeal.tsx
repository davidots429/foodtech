import { useRef, useState } from "react";
import { api } from "./cookingApi";
import { CombiQuality } from "./CombiQuality";
import { CombiWindows } from "./CombiWindows";
import { CombiRecommendation } from "./CombiRecommendation";
import { CombiLive } from "./CombiLive";
import { RicePreparation, type RicePreparationRecord } from "./RicePreparation";
import { CombiQualityAggregate } from "./CombiQualityAggregate";

type FanObservation = { status: string; target_level: number; deviation_seconds: number; missing_seconds: number };
type ClimateObservation = { status: string; target_percent: string; deviation_seconds: number; missing_seconds: number };
type ProbeSummary = { probe_id: string; product_id: string; rack_id: string; shelf: number; pan_id: string;
  status: string; full_interval_min_celsius: string | null; longest_75c_hold_seconds: number | null };
type Meal = {
  meal: { meal_batch_id: string; status: string; equipment_size: string; version: number;
    rice_recipe_version?: string; mackerel_recipe_version?: string;
    source?: { device_id: string; record_count: number; raw_sha256: string; first_observed_at?: string;
      phase_counts: { RICE: number; TRANSITION: number; MACKEREL: number } } };
  rice: { job_id: string; status: string; safety_status: string; quality_current?: { version: number;
    quality_status: string; score_percent: string; quality_target: boolean; usable?: boolean };
    water_ratio_status: string; effective_water_ratio: string;
    rice_preparation?: RicePreparationRecord;
    target_added_water_kg: string | null; payload_kg: string; gross_kg: string;
    time_status?: string; program_cooking_duration?: number; thermal_exposure_duration?: number;
    monitored_process_source?: { raw_sha256: string; phase: string };
    monitored_process_samples?: unknown[];
    monitored_process_assessment?: { status: string; humidity_value_source: string; reason_codes: string[];
      door_assessment?: { status: string; door_open_seconds?: number }; fan_observation?: FanObservation; climate_observation?: ClimateObservation } };
  transition: { transition_id: string; mackerel_load_allowed: boolean; status: string;
    humidity_verification_status?: string; dryness_verification_basis?: string; ready_at_second?: number | null;
    samples?: unknown[]; source?: { raw_sha256: string; phase: string } };
  mackerel: { job_id: string; status: string; safety_status: string; food_unloaded_at?: string;
    load_snapshot?: { rack_count: number; pan_count: number; ingredient_kg: string; pan_tare_kg: string;
      rack_tare_kg: string; payload_kg: string; gross_kg: string; rated_capacity_kg: string;
      load_basis: string; load_ratio: string; observed_at: string };
    current_unloaded_at?: string;
    quality_current?: { version: number; quality_status: string; score_percent: string; quality_target: boolean; usable?: boolean };
    time_status?: string; program_cooking_duration?: number; thermal_exposure_duration?: number;
    batch_representative_core_temperature?: string | null;
    safety_probe_summaries?: ProbeSummary[];
    monitored_process_source?: { raw_sha256: string; phase: string };
    monitored_process_samples?: unknown[];
    monitored_process_assessment?: { status: string; recovered_at_second: number | null;
      door_open_seconds: number; reason_codes: string[]; fan_observation?: FanObservation; climate_observation?: ClimateObservation };
    products?: { product_id: string; rack_id: string; shelf: number; pan_id: string;
      position?: string | null; mass_kg: string; thickness_cm: string }[] };
  events?: { event: string; observed_at: string; selection_issues?: string[] }[];
};

const mealSteps: Record<string, { event: string; label: string }> = {
  PREPARING_RICE: { event: "RICE_STARTED", label: "밥 조리 시작 기록" },
  RICE_COOKING: { event: "RICE_PROGRAM_COMPLETED", label: "밥 프로그램 완료 기록" },
  RICE_AWAITING_UNLOAD: { event: "RICE_UNLOADED", label: "밥 인양 기록" },
  RICE_UNLOADED: { event: "MODE_TRANSITION_STARTED", label: "건열 전환 시작 기록" },
  MACKEREL_READY: { event: "MACKEREL_STARTED", label: "고등어 합성 제품 투입·조리 시작" },
  MACKEREL_COOKING: { event: "MACKEREL_PROGRAM_COMPLETED", label: "고등어 프로그램 완료 기록" },
  MACKEREL_AWAITING_UNLOAD: { event: "MACKEREL_UNLOADED", label: "고등어 인양 기록" },
};
const mealLabels: Record<string, string> = { PREPARING_RICE: "밥 준비", RICE_COOKING: "밥 조리 중",
  RICE_AWAITING_UNLOAD: "밥 인양 대기", RICE_UNLOADED: "밥 인양 완료", MODE_TRANSITION: "건열 전환 중",
  MACKEREL_READY: "고등어 투입 준비", TRANSITION_TIMEOUT: "전환 시간 초과",
  MACKEREL_COOKING: "고등어 조리 중", MACKEREL_AWAITING_UNLOAD: "고등어 인양 대기",
  MACKEREL_UNLOADED: "고등어 안전 확인 대기", MACKEREL_SAFETY_PASSED: "고등어 안전 표본 통과",
  MACKEREL_SAFETY_FAILED: "고등어 안전 표본 실패" };

export function CombiMeal({ csrf }: { csrf: string }) {
  const [size, setSize] = useState("MEDIUM_FIXED_RACK_10");
  const [racks, setRacks] = useState("1");
  const [pans, setPans] = useState("10");
  const [dry, setDry] = useState("20");
  const [drained, setDrained] = useState("24");
  const [water, setWater] = useState("20");
  const [panTare, setPanTare] = useState("10");
  const [rackTare, setRackTare] = useState("0");
  const [mackerelPanTare, setMackerelPanTare] = useState("");
  const [riceRecipe, setRiceRecipe] = useState("");
  const [mackerelRecipe, setMackerelRecipe] = useState("");
  const [meal, setMeal] = useState<Meal | null>(null);
  const [lookup, setLookup] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [observedAt, setObservedAt] = useState("");
  const [sourceDevice, setSourceDevice] = useState("synthetic-combi-medium");
  const [sourceFile, setSourceFile] = useState<File | null>(null);
  const [riceSafetyObservation, setRiceSafetyObservation] = useState<"" | "NO_ANOMALY" | "SUSPECTED">("");
  const [riceSafetyNote, setRiceSafetyNote] = useState("");
  const [sampleLayout, setSampleLayout] = useState("THREE_SHELVES");
  const [selectedProducts, setSelectedProducts] = useState<string[] | null>(null);
  const [selectionNote, setSelectionNote] = useState("");
  const [reheatStart, setReheatStart] = useState("");
  const [reheatEnd, setReheatEnd] = useState("");
  const [reheatReason, setReheatReason] = useState("");
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
        ...(riceRecipe.trim() ? { rice_recipe_version: riceRecipe.trim() } : {}),
        ...(mackerelRecipe.trim() ? { mackerel_recipe_version: mackerelRecipe.trim() } : {}),
      });
      setMeal(result); setLookup(result.meal.meal_batch_id); setSourceFile(null); setSelectedProducts(null); setSelectionNote(""); setSampleLayout("THREE_SHELVES"); setMackerelPanTare("");
    } catch (cause) { setError(cause instanceof Error ? cause.message : "배치를 생성하지 못했습니다."); }
    finally { requestActive.current = false; setBusy(false); }
  }
  async function importSource() {
    if (!meal || !sourceFile || requestActive.current) return;
    if (sourceFile.size > 2_500_000) { setError("합성 원천 파일은 2.5MB 이하이어야 합니다."); return; }
    requestActive.current = true; setBusy(true); setError("");
    const path = `/api/customer/combi-meals/${encodeURIComponent(meal.meal.meal_batch_id)}`;
    try {
      const lines = (await sourceFile.text()).split(/\r?\n/).filter(line => line.length > 0);
      await api(path + "/source-observations", csrf, { expected_version: meal.meal.version,
        device_id: sourceDevice, records: lines });
      setMeal(await api<Meal>(path)); setSourceFile(null);
    } catch (cause) { setError(cause instanceof Error ? cause.message : "합성 원천 자료를 연결하지 못했습니다."); }
    finally { requestActive.current = false; setBusy(false); }
  }
  async function confirmRiceSafety() {
    if (!meal || !riceSafetyObservation || !riceSafetyNote.trim() || requestActive.current) return;
    requestActive.current = true; setBusy(true); setError("");
    const path = `/api/customer/combi-meals/${encodeURIComponent(meal.meal.meal_batch_id)}`;
    try {
      await api(path + "/rice-safety", csrf, { expected_version: meal.meal.version,
        observation: riceSafetyObservation, note: riceSafetyNote.trim() });
      setMeal(await api<Meal>(path)); setRiceSafetyObservation(""); setRiceSafetyNote("");
    } catch (cause) { setError(cause instanceof Error ? cause.message : "밥 안전 확인을 기록하지 못했습니다."); }
    finally { requestActive.current = false; setBusy(false); }
  }
  function downloadMockFixture() {
    if (!meal) return;
    const segment = (seconds: number, chamber: string, climate: string, humidity: string | null,
      source: string, fan: number) => ({ seconds, chamberCelsius: chamber, climateSetpointPercent: climate,
      measuredHumidityPercent: humidity, humidityValueSource: source, fanLevel: fan, doorState: "CLOSED",
      criticalAlarm: false, ventSupported: false, ventEvent: null });
    const fixture = { provenance: "SYNTHETIC", profileId: "mockprofile-120", mockInstanceId: "mockprofile-120-a-001",
      mealBatchId: meal.meal.meal_batch_id, deviceId: sourceDevice, generatorVersion: "combi-generator-v1", seed: 2026,
      logicalStartAt: new Date(Date.now() - 2_701_000).toISOString(), phases: [
        { phase: "RICE", cookingJobId: meal.rice.job_id, programVersion: "rice-steam-v1",
          segments: [segment(1800, "100", "100", "98", "MEASURED", 4)] },
        { phase: "TRANSITION", cookingJobId: meal.transition.transition_id, programVersion: "dry-transition-v1",
          segments: [segment(180, "220", "10", null, "NOT_SUPPORTED", 3)] },
        { phase: "MACKEREL", cookingJobId: meal.mackerel.job_id, programVersion: "mackerel-dry-v1",
          segments: [segment(720, "220", "10", "10", "MEASURED", 3)] },
      ] };
    const url = URL.createObjectURL(new Blob([JSON.stringify(fixture, null, 2)], { type: "application/json" }));
    const link = document.createElement("a"); link.href = url; link.download = "combi-meal-fixture.json";
    link.click(); URL.revokeObjectURL(url);
  }
  async function open() {
    if (requestActive.current) return;
    requestActive.current = true;
    setBusy(true); setError("");
    try { setMeal(await api<Meal>(`/api/customer/combi-meals/${encodeURIComponent(lookup.trim())}`)); setSelectedProducts(null); setSelectionNote(""); setSampleLayout("THREE_SHELVES"); setMackerelPanTare(""); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "배치를 찾지 못했습니다."); }
    finally { requestActive.current = false; setBusy(false); }
  }
  async function recordStep() {
    if (!meal || !mealSteps[meal.meal.status] || requestActive.current) return;
    const observed = new Date(observedAt);
    if (!observedAt || Number.isNaN(observed.getTime())) { setError("단계의 합성 관찰시각을 입력해 주세요."); return; }
    if (meal.meal.status === "MACKEREL_READY" && (!mackerelPanTare.trim() ||
        !Number.isFinite(Number(mackerelPanTare)) || Number(mackerelPanTare) < 0)) {
      setError("고등어 적재에 사용하는 빈 팬의 총중량(kg)을 입력해 주세요."); return;
    }
    requestActive.current = true; setBusy(true); setError("");
    const path = `/api/customer/combi-meals/${encodeURIComponent(meal.meal.meal_batch_id)}`;
    try {
      const event = mealSteps[meal.meal.status].event;
      const locations = sampleLayout === "ONE_SHELF_TWO" ? [[1, 1, "LEFT"], [1, 1, "RIGHT"]] :
        sampleLayout === "ONE_SHELF" ? [[1, 1, "LEFT"], [1, 1, "CENTER"], [1, 1, "RIGHT"]] :
        sampleLayout === "TWO_SHELVES_FOUR" ? [[1, 1, "LEFT"], [1, 2, "LEFT"], [1, 2, "RIGHT"], [1, 1, "RIGHT"]] :
        sampleLayout === "TWO_RACKS_EQUAL" ? [[1, 1, "LEFT"], [1, 2, "LEFT"], [2, 2, "LEFT"], [2, 3, "LEFT"]] :
        sampleLayout === "TWO_SHELVES" ? [[1, 1, "LEFT"], [1, 2, "LEFT"], [1, 2, "RIGHT"]] :
        sampleLayout === "TWO_RACKS" ? [[1, 1, "LEFT"], [1, 2, "LEFT"], [2, 3, "LEFT"]] :
        [[1, 1, "LEFT"], [1, 2, "CENTER"], [1, 3, "RIGHT"]];
      const products = event === "MACKEREL_STARTED" ? locations.map(([rack, shelf, position], index) => ({
        product_id: `synthetic-fish-${index + 1}`, rack_id: `synthetic-rack-${rack}`, shelf,
        pan_id: `synthetic-pan-${rack}-${shelf}`, position, mass_kg: "0.13", thickness_cm: index === 1 ? "2.0" : "1.5",
      })) : undefined;
      await api(path + "/events", csrf, { expected_version: meal.meal.version,
        event, observed_at: observed.toISOString(), ...(products ? { products, pan_tare_kg: mackerelPanTare } : {}) });
      setMeal(await api<Meal>(path)); setObservedAt("");
      if (event === "MACKEREL_STARTED") { setSelectedProducts(null); setSelectionNote(""); setMackerelPanTare(""); }
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
  async function simulateMackerelSafety() {
    if (!meal?.mackerel.food_unloaded_at || !meal.mackerel.products || requestActive.current) return;
    const chosen = selectedProducts ?? meal.mackerel.products.slice(0, 3).map(product => product.product_id);
    if (chosen.length !== Math.min(3, meal.mackerel.products.length)) {
      setError(`표본 ${Math.min(3, meal.mackerel.products.length)}개를 선택해 주세요.`); return;
    }
    requestActive.current = true; setBusy(true); setError("");
    const path = `/api/customer/combi-meals/${encodeURIComponent(meal.meal.meal_batch_id)}`;
    const probes = chosen.map((productId, index) => ({ product_id: productId,
      probe_id: `synthetic-probe-${index + 1}`, calibration_status: "OK",
      readings: Array.from({ length: 60 }, (_, second) => ({ second, celsius: "75" })) }));
    try {
      await api(path + "/mackerel-safety", csrf, { expected_version: meal.meal.version,
        measured_at: meal.mackerel.current_unloaded_at ?? meal.mackerel.food_unloaded_at, probes,
        selection_note: selectionNote.trim() || null });
      setMeal(await api<Meal>(path));
    } catch (cause) { setError(cause instanceof Error ? cause.message : "안전 표본을 기록하지 못했습니다."); }
    finally { requestActive.current = false; setBusy(false); }
  }
  async function recordReheat() {
    if (!meal || requestActive.current) return;
    const start = new Date(reheatStart), end = new Date(reheatEnd);
    if (!reheatStart || !reheatEnd || Number.isNaN(start.getTime()) || Number.isNaN(end.getTime()) || !reheatReason.trim()) {
      setError("추가 가열의 시작·인양 시각과 사유를 입력해 주세요."); return;
    }
    requestActive.current = true; setBusy(true); setError("");
    const path = `/api/customer/combi-meals/${encodeURIComponent(meal.meal.meal_batch_id)}`;
    try {
      await api(path + "/mackerel-reheat", csrf, { expected_version: meal.meal.version,
        started_at: start.toISOString(), unloaded_at: end.toISOString(), reason: reheatReason.trim() });
      setMeal(await api<Meal>(path)); setReheatStart(""); setReheatEnd(""); setReheatReason("");
    } catch (cause) { setError(cause instanceof Error ? cause.message : "추가 가열을 기록하지 못했습니다."); }
    finally { requestActive.current = false; setBusy(false); }
  }
  async function simulateMackerelProcess() {
    if (!meal?.mackerel.program_cooking_duration || requestActive.current) return;
    const duration = meal.mackerel.program_cooking_duration;
    if (duration > 900) { setError("이 합성 표본 시험은 최대 900초까지 지원합니다."); return; }
    requestActive.current = true; setBusy(true); setError("");
    const path = `/api/customer/combi-meals/${encodeURIComponent(meal.meal.meal_batch_id)}`;
    const samples = Array.from({ length: duration }, (_, index) => ({ second: index + 1,
      chamber_celsius: "220", door: "CLOSED", fan_level: 3, climate_setpoint_percent: "10" }));
    try {
      await api(path + "/mackerel-process", csrf, { expected_version: meal.meal.version, samples });
      setMeal(await api<Meal>(path));
    } catch (cause) { setError(cause instanceof Error ? cause.message : "공정 표본을 기록하지 못했습니다."); }
    finally { requestActive.current = false; setBusy(false); }
  }
  async function simulateRiceProcess(humiditySupported: boolean) {
    if (!meal?.rice.program_cooking_duration || requestActive.current) return;
    const duration = meal.rice.program_cooking_duration;
    if (duration > 2000) { setError("이 합성 표본 시험은 최대 2000초까지 지원합니다."); return; }
    requestActive.current = true; setBusy(true); setError("");
    const path = `/api/customer/combi-meals/${encodeURIComponent(meal.meal.meal_batch_id)}`;
    const samples = Array.from({ length: duration }, (_, index) => ({ second: index + 1,
      chamber_celsius: "100", humidity_percent: humiditySupported ? "98" : null, door: "CLOSED", fan_level: 4, climate_setpoint_percent: "100" }));
    try {
      await api(path + "/rice-process", csrf, { expected_version: meal.meal.version,
        humidity_source: humiditySupported ? "MEASURED" : "NOT_SUPPORTED", samples });
      setMeal(await api<Meal>(path));
    } catch (cause) { setError(cause instanceof Error ? cause.message : "밥 공정 표본을 기록하지 못했습니다."); }
    finally { requestActive.current = false; setBusy(false); }
  }
  async function assessSource(phase: "rice" | "transition" | "mackerel", reassessLegacyFan = false, reassessManualTransition = false, reassessManualMackerel = false, reassessManualRice = false, reassessLegacyClimate = false) {
    if (!meal || requestActive.current) return;
    requestActive.current = true; setBusy(true); setError("");
    const path = `/api/customer/combi-meals/${encodeURIComponent(meal.meal.meal_batch_id)}`;
    try {
      await api(path + `/source-assessment/${phase}`, csrf, { expected_version: meal.meal.version,
        reassess_legacy_fan: reassessLegacyFan, reassess_manual_transition: reassessManualTransition,
        reassess_manual_mackerel: reassessManualMackerel, reassess_manual_rice: reassessManualRice,
        reassess_legacy_climate: reassessLegacyClimate });
      setMeal(await api<Meal>(path));
    } catch (cause) { setError(cause instanceof Error ? cause.message : "원표본 판정을 기록하지 못했습니다."); }
    finally { requestActive.current = false; setBusy(false); }
  }
  function useSourceTime() {
    const source = meal?.meal.source;
    const step = meal && mealSteps[meal.meal.status]?.event;
    if (!source?.first_observed_at || !step) return;
    const offsets: Record<string, number> = { RICE_STARTED: 0,
      RICE_PROGRAM_COMPLETED: source.phase_counts.RICE, RICE_UNLOADED: source.phase_counts.RICE,
      MODE_TRANSITION_STARTED: source.phase_counts.RICE,
      MACKEREL_STARTED: source.phase_counts.RICE + source.phase_counts.TRANSITION,
      MACKEREL_PROGRAM_COMPLETED: source.record_count, MACKEREL_UNLOADED: source.record_count };
    if (!(step in offsets)) return;
    const value = new Date(new Date(source.first_observed_at).getTime() + offsets[step] * 1000);
    setObservedAt(new Date(value.getTime() - value.getTimezoneOffset() * 60_000).toISOString().slice(0, 23));
  }
  const products = meal?.mackerel.products ?? [];
  const chosen = selectedProducts ?? products.slice(0, 3).map(product => product.product_id);
  return <section><h2>밥·고등어 합성 식사 배치</h2>
    <p>밥을 인양한 뒤 오븐을 건열로 전환하고 고등어를 조리하는 시연입니다. 장비를 제어하지 않습니다.</p>
    <div className="field-grid">
      <label>장비 규모<select value={size} onChange={event => {
        const large = event.target.value === "LARGE_ROLL_IN_RACK_20";
        setSize(event.target.value); setSampleLayout("THREE_SHELVES"); setRacks(large ? "2" : "1"); setPans(large ? "20" : "10");
        setDry(large ? "40" : "20"); setDrained(large ? "48" : "24"); setWater(large ? "40" : "20");
        setPanTare(large ? "20" : "10"); setRackTare(large ? "30" : "0");
      }}><option value="MEDIUM_FIXED_RACK_10">중형 고정 랙</option><option value="LARGE_ROLL_IN_RACK_20">대형 이동식 랙</option></select></label>
      <label>밥 레시피 버전 (선택)<input maxLength={80} value={riceRecipe} onChange={event => setRiceRecipe(event.target.value)} /></label>
      <label>고등어 레시피 버전 (선택)<input maxLength={80} value={mackerelRecipe} onChange={event => setMackerelRecipe(event.target.value)} /></label>
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
      <p>레시피 버전: 밥 {meal.meal.rice_recipe_version ?? "미기록"} · 고등어 {meal.meal.mackerel_recipe_version ?? "미기록"}</p>
      <p>밥: 유효 수분비 {meal.rice.effective_water_ratio} · {meal.rice.water_ratio_status === "NORMAL" ? "권장 범위" : "권장 범위 밖"} · 목표 추가 물 {meal.rice.target_added_water_kg ?? "계산 불가"} kg</p>
      <p>내용물·팬 {meal.rice.payload_kg} kg · 랙 포함 {meal.rice.gross_kg} kg</p>
      <p>밥 작업 <code>{meal.rice.job_id}</code> → 모드 전환 <code>{meal.transition.transition_id}</code> → 고등어 작업 <code>{meal.mackerel.job_id}</code></p>
      <RicePreparation key={`rice-preparation-${meal.meal.meal_batch_id}`} mealId={meal.meal.meal_batch_id} version={meal.meal.version}
        editable={meal.meal.status === "PREPARING_RICE"} current={meal.rice.rice_preparation} csrf={csrf}
        onSaved={async () => setMeal(await api<Meal>(`/api/customer/combi-meals/${encodeURIComponent(meal.meal.meal_batch_id)}`))} />
      {meal.meal.source ? <p>연결된 합성 Mock: {meal.meal.source.device_id} · 원표본 {meal.meal.source.record_count}건 · SHA-256 <code>{meal.meal.source.raw_sha256}</code></p>
        : meal.meal.status === "PREPARING_RICE" && <div><p>별도 Mock에서 위 식사·작업 ID로 생성한 JSONL 원표본을 조리 시작 전에 연결할 수 있습니다. 판정은 이후 단계에서 수행합니다.</p>
          <label>합성 장비 ID<input value={sourceDevice} maxLength={80} onChange={event => setSourceDevice(event.target.value)} /></label>
          <button type="button" className="secondary" disabled={busy || !sourceDevice} onClick={downloadMockFixture}>이 배치용 Mock fixture 받기</button>
          <label>Mock 원표본 JSONL<input type="file" accept=".jsonl,.ndjson,application/x-ndjson" onChange={event => setSourceFile(event.target.files?.[0] ?? null)} /></label>
          <button type="button" disabled={busy || !sourceFile || !sourceDevice} onClick={importSource}>합성 원천 자료 연결</button></div>}
      {meal.meal.source && <CombiWindows key={`combi-windows-${meal.meal.meal_batch_id}`} mealId={meal.meal.meal_batch_id} />}
      {meal.rice.time_status && <p>밥 프로그램 시간 {meal.rice.program_cooking_duration}초 · 시간 판정 {meal.rice.time_status}
        {meal.rice.thermal_exposure_duration !== undefined && <> · 인양까지 열 노출 {meal.rice.thermal_exposure_duration}초</>}</p>}
      {meal.rice.thermal_exposure_duration !== undefined && !meal.rice.monitored_process_assessment && <div>
        <p>밥 조리 구간의 100°C·문 닫힘 합성 표본으로 온도·습도와 문 상태를 확인합니다.</p>
        {meal.meal.source && <button type="button" disabled={busy} onClick={() => assessSource("rice")}>연결된 밥 원표본 판정</button>}{" "}
        {!meal.meal.source && <><button type="button" disabled={busy} onClick={() => simulateRiceProcess(true)}>습도 지원 합성 표본</button>{" "}
        <button type="button" disabled={busy} onClick={() => simulateRiceProcess(false)}>습도 미지원 합성 표본</button></>}</div>}
      {meal.rice.monitored_process_assessment && <p>밥 온도·습도·문·시간 부분 판정: {meal.rice.monitored_process_assessment.status}
        {" "}· 습도 출처 {meal.rice.monitored_process_assessment.humidity_value_source}
        {" "}· 문 상태 {meal.rice.monitored_process_assessment.door_assessment?.status ?? "NOT_MEASURED"}
        {meal.rice.monitored_process_assessment.reason_codes.length > 0 && <> · {meal.rice.monitored_process_assessment.reason_codes.join(", ")}</>}</p>}
      {meal.rice.monitored_process_assessment?.fan_observation && <p>밥 팬 단계 관찰: {meal.rice.monitored_process_assessment.fan_observation.status}
        {" "}· 목표 4 · 다른 단계 {meal.rice.monitored_process_assessment.fan_observation.deviation_seconds}초 · 미관찰 {meal.rice.monitored_process_assessment.fan_observation.missing_seconds}초</p>}
      {meal.rice.monitored_process_assessment?.climate_observation && <p>밥 기후 설정 관찰: {meal.rice.monitored_process_assessment.climate_observation.status}
        {" "}· 목표 100% · 다른 설정 {meal.rice.monitored_process_assessment.climate_observation.deviation_seconds}초 · 미관찰 {meal.rice.monitored_process_assessment.climate_observation.missing_seconds}초</p>}
      {meal.meal.source && meal.rice.monitored_process_assessment && "fan_observation" in meal.rice.monitored_process_assessment &&
       !("climate_observation" in meal.rice.monitored_process_assessment) &&
       meal.rice.monitored_process_source?.raw_sha256 === meal.meal.source.raw_sha256 &&
       <div><p className="small">이전 밥 판정에 기후 설정 관찰이 없습니다. 원표본 재판정 후 안전을 다시 확인합니다.</p>
         <button type="button" disabled={busy} onClick={() => assessSource("rice", false, false, false, false, true)}>밥 기후 설정 다시 판정</button></div>}
      {meal.meal.source && meal.rice.monitored_process_assessment && !("fan_observation" in meal.rice.monitored_process_assessment) &&
       meal.rice.monitored_process_source?.raw_sha256 === meal.meal.source.raw_sha256 &&
       <div><p className="small">이전 공정 판정에 팬 관찰이 없습니다. 보존된 원표본을 다시 확인한 뒤 안전 확인이 필요합니다.</p>
         <button type="button" disabled={busy} onClick={() => assessSource("rice", true)}>밥 원표본 다시 판정</button></div>}
      {meal.mackerel.time_status && <p>고등어 프로그램 시간 {meal.mackerel.program_cooking_duration}초 · 시간 판정 {meal.mackerel.time_status}
        {meal.mackerel.thermal_exposure_duration !== undefined && <> · 인양까지 열 노출 {meal.mackerel.thermal_exposure_duration}초</>}</p>}
      {meal.mackerel.load_snapshot ? <p>고등어 투입 적재량: 식재료 {meal.mackerel.load_snapshot.ingredient_kg}kg ·
        빈 팬 {meal.mackerel.load_snapshot.pan_tare_kg}kg ({meal.mackerel.load_snapshot.pan_count}개) ·
        랙 {meal.mackerel.load_snapshot.rack_tare_kg}kg ({meal.mackerel.load_snapshot.rack_count}개) ·
        정격 반영 {meal.mackerel.load_snapshot.load_basis === "PAYLOAD_ONLY" ? meal.mackerel.load_snapshot.payload_kg : meal.mackerel.load_snapshot.gross_kg}kg
        / {meal.mackerel.load_snapshot.rated_capacity_kg}kg</p> : meal.mackerel.status !== "PENDING" && <p>고등어 투입 적재량: 미기록</p>}
      {meal.meal.status === "MACKEREL_READY" && <div><p>합성 제품을 각 130g, 두께 1.5~2.0cm로 투입합니다.</p>
        <label>고등어용 빈 팬 총중량(kg)<input type="number" min="0" step="0.01" value={mackerelPanTare}
          onChange={event => setMackerelPanTare(event.target.value)} /></label>
        <p className="small">이번 적재의 빈 팬 중량을 모두 합해 입력합니다. 밥의 팬 중량은 가져오지 않습니다.
          대형 합성 프로파일은 사용한 이동식 랙마다 15kg을 포함합니다.</p>
        <label>합성 적재 위치<select value={sampleLayout} onChange={event => setSampleLayout(event.target.value)}>
          <option value="THREE_SHELVES">한 랙 · 세 선반</option><option value="TWO_SHELVES">한 랙 · 두 선반</option>
          <option value="TWO_SHELVES_FOUR">한 랙 · 두 선반 · 네 제품</option>
          <option value="ONE_SHELF">한 랙 · 한 선반</option><option value="ONE_SHELF_TWO">한 랙 · 한 선반 · 두 제품</option>
          {meal.meal.equipment_size === "LARGE_ROLL_IN_RACK_20" && <option value="TWO_RACKS">두 랙</option>}
          {meal.meal.equipment_size === "LARGE_ROLL_IN_RACK_20" && <option value="TWO_RACKS_EQUAL">두 랙 · 동일 적재량 · 네 제품</option>}
        </select></label></div>}
      {mealSteps[meal.meal.status] && <div className="field-row"><label>합성 관찰시각
        <input type="datetime-local" step="0.001" value={observedAt} onChange={event => setObservedAt(event.target.value)} /></label>
        {meal.meal.source?.first_observed_at && <button type="button" className="secondary" disabled={busy} onClick={useSourceTime}>원표본 시각 입력</button>}
        <button type="button" disabled={busy || (meal.meal.status === "MACKEREL_READY" && !!meal.meal.source &&
          (meal.transition.source?.raw_sha256 !== meal.meal.source.raw_sha256 || meal.transition.source?.phase !== "TRANSITION"))}
          onClick={recordStep}>{mealSteps[meal.meal.status].label}</button></div>}
      {meal.meal.status === "MODE_TRANSITION" && <div><p>합성 표본으로 전환 조건을 시험합니다. 전환 시작 시각 이후 30초 또는 180초가 경과해야 합니다.</p>
        {meal.meal.source && !meal.transition.samples?.length && <button type="button" disabled={busy} onClick={() => assessSource("transition")}>연결된 전환 원표본 판정</button>}{" "}
        {!meal.meal.source && <><button type="button" disabled={busy} onClick={() => simulateTransition(true)}>습도 지원 합성 표본</button>{" "}
        <button type="button" disabled={busy} onClick={() => simulateTransition(false)}>습도 미지원 합성 표본</button></>}</div>}
      {meal.meal.status === "MACKEREL_UNLOADED" && <div><p>인양 후 제품 {Math.min(3, products.length)}개를 선택해 각 75°C 합성 표본을 60초간 확인합니다. 인양 후 60초가 경과해야 합니다.</p>
        <fieldset><legend>합성 안전 표본 선택</legend>
          {products.map(product => <label key={product.product_id} style={{ display: "block" }}>
            <input type="checkbox" checked={chosen.includes(product.product_id)}
              disabled={busy || (!chosen.includes(product.product_id) && chosen.length >= 3)}
              onChange={event => setSelectedProducts(event.target.checked ? [...chosen, product.product_id] : chosen.filter(id => id !== product.product_id))} />
            {product.product_id} · {product.rack_id} / {product.shelf}단 / {product.pan_id} · 위치 {product.position ?? "미기록"}
            {" "}· {product.mass_kg}kg · 두께 {product.thickness_cm}cm
          </label>)}
        </fieldset>
        <label>표본 선정 근거 (두 랙의 적재량이 같으면 필수)
          <input maxLength={500} value={selectionNote} onChange={event => setSelectionNote(event.target.value)} /></label>
        <p>선반·랙·두께와 위치 기준은 서버에서 확인하며, 기준에 맞지 않으면 안전 결과는 미완료입니다.</p>
        <button type="button" disabled={busy} onClick={simulateMackerelSafety}>합성 심부온도 확인</button></div>}
      {meal.meal.status === "MACKEREL_SAFETY_FAILED" && <div><p>실패 이력을 보존하고 별도의 추가 가열 구간을 기록한 뒤 안전 표본을 다시 확인합니다.</p>
        <div className="field-row"><label>추가 가열 시작<input type="datetime-local" step="0.001" value={reheatStart} onChange={event => setReheatStart(event.target.value)} /></label>
        <label>추가 가열 후 인양<input type="datetime-local" step="0.001" value={reheatEnd} onChange={event => setReheatEnd(event.target.value)} /></label>
        <label>사유<input maxLength={500} value={reheatReason} onChange={event => setReheatReason(event.target.value)} /></label>
        <button type="button" disabled={busy} onClick={recordReheat}>추가 가열 구간 기록</button></div></div>}
      {["MACKEREL_UNLOADED", "MACKEREL_SAFETY_PASSED", "MACKEREL_SAFETY_FAILED"].includes(meal.meal.status) &&
        !meal.mackerel.monitored_process_assessment && <div><p>조리 구간의 220°C·문 닫힘 합성 표본으로 온도 복원과 문 상태를 확인합니다.</p>
          {meal.meal.source && <button type="button" disabled={busy} onClick={() => assessSource("mackerel")}>연결된 고등어 원표본 판정</button>}{" "}
          {!meal.meal.source && <button type="button" disabled={busy} onClick={simulateMackerelProcess}>합성 공정 표본 확인</button>}</div>}
      {meal.meal.source && meal.transition.samples?.length &&
       (meal.transition.source?.raw_sha256 !== meal.meal.source.raw_sha256 || meal.transition.source?.phase !== "TRANSITION") &&
        <div><p className="warning">기록된 전환 판정은 연결된 원표본으로 확인되지 않았습니다.</p>
          {["MODE_TRANSITION", "MACKEREL_READY"].includes(meal.meal.status) &&
            <button type="button" disabled={busy} onClick={() => assessSource("transition", false, true)}>전환 원표본 다시 판정</button>}</div>}
      {meal.meal.source && meal.rice.monitored_process_assessment &&
       (meal.rice.monitored_process_source?.raw_sha256 !== meal.meal.source.raw_sha256 || meal.rice.monitored_process_source?.phase !== "RICE") &&
        <div><p className="warning">기록된 밥 공정 판정은 연결된 원표본으로 확인되지 않았습니다.</p>
          {!!meal.rice.monitored_process_samples?.length && <button type="button" disabled={busy}
            onClick={() => assessSource("rice", false, false, false, true)}>밥 원표본 다시 판정</button>}</div>}
      {meal.meal.source && meal.mackerel.monitored_process_assessment &&
       (meal.mackerel.monitored_process_source?.raw_sha256 !== meal.meal.source.raw_sha256 || meal.mackerel.monitored_process_source?.phase !== "MACKEREL") &&
        <div><p className="warning">기록된 고등어 공정 판정은 연결된 원표본으로 확인되지 않았습니다.</p>
          {!!meal.mackerel.monitored_process_samples?.length && <button type="button" disabled={busy}
            onClick={() => assessSource("mackerel", false, false, true)}>고등어 원표본 다시 판정</button>}</div>}
      {meal.mackerel.monitored_process_assessment && <p>고등어 온도 복원·문·시간 부분 판정: {meal.mackerel.monitored_process_assessment.status}
        {meal.mackerel.monitored_process_assessment.reason_codes.length > 0 && <> · {meal.mackerel.monitored_process_assessment.reason_codes.join(", ")}</>}</p>}
      {meal.mackerel.monitored_process_assessment?.fan_observation && <p>고등어 팬 단계 관찰: {meal.mackerel.monitored_process_assessment.fan_observation.status}
        {" "}· 목표 3 · 다른 단계 {meal.mackerel.monitored_process_assessment.fan_observation.deviation_seconds}초 · 미관찰 {meal.mackerel.monitored_process_assessment.fan_observation.missing_seconds}초</p>}
      {meal.mackerel.monitored_process_assessment?.climate_observation && <p>고등어 기후 설정 관찰: {meal.mackerel.monitored_process_assessment.climate_observation.status}
        {" "}· 목표 10% · 다른 설정 {meal.mackerel.monitored_process_assessment.climate_observation.deviation_seconds}초 · 미관찰 {meal.mackerel.monitored_process_assessment.climate_observation.missing_seconds}초</p>}
      {meal.meal.source && meal.mackerel.monitored_process_assessment &&
       !("climate_observation" in meal.mackerel.monitored_process_assessment) &&
       meal.mackerel.monitored_process_source?.raw_sha256 === meal.meal.source.raw_sha256 &&
       <div><p className="small">이전 고등어 판정에 기후 설정 관찰이 없습니다.</p>
         <button type="button" disabled={busy} onClick={() => assessSource("mackerel", false, false, false, false, true)}>고등어 기후 설정 다시 판정</button></div>}
      {meal.mackerel.safety_status !== "NOT_MEASURED" && <p>고등어 합성 안전 표본: {meal.mackerel.safety_status}
        {meal.mackerel.batch_representative_core_temperature && <> · 최저 {meal.mackerel.batch_representative_core_temperature}°C</>}</p>}
      {!!meal.mackerel.safety_probe_summaries?.length && <div><p>각 탐침의 60초 측정 결과 (표본 선정과 배치 안전 판정은 별도)</p>
        <ul>{meal.mackerel.safety_probe_summaries.map(probe => <li key={probe.probe_id}>
          {probe.probe_id} · {probe.product_id} · {probe.rack_id} / {probe.shelf}단 / {probe.pan_id} · {probe.status}
          {" "}· 60초 최저 {probe.full_interval_min_celsius === null ? "미확인" : `${probe.full_interval_min_celsius}°C`}
          {" "}· 75°C 연속 유지 {probe.longest_75c_hold_seconds === null ? "미확인" : `${probe.longest_75c_hold_seconds}초`}
        </li>)}</ul></div>}
      {meal.events?.filter(event => event.event === "MACKEREL_CORE_CHECK").slice(-1)[0]?.selection_issues?.length ?
        <p role="alert">표본 선정 확인 필요: {meal.events.filter(event => event.event === "MACKEREL_CORE_CHECK").slice(-1)[0].selection_issues?.join(", ")}</p> : null}
      {meal.rice.status === "UNLOADED" && <div className="binding-box"><h4>밥 안전 확인</h4>
        <p>별도 측정 없이 기록된 공정 판정과 운영자가 확인한 안전 이상 여부를 함께 확인합니다. 현재 상태: {meal.rice.safety_status}</p>
        <label>안전 이상 여부<select value={riceSafetyObservation} onChange={event => setRiceSafetyObservation(event.target.value as typeof riceSafetyObservation)}>
          <option value="">선택</option><option value="NO_ANOMALY">안전 이상 없음</option><option value="SUSPECTED">안전 이상 의심</option>
        </select></label>
        <label>확인 근거<input maxLength={500} value={riceSafetyNote} onChange={event => setRiceSafetyNote(event.target.value)} /></label>
        <button type="button" disabled={busy || !riceSafetyObservation || !riceSafetyNote.trim()} onClick={confirmRiceSafety}>안전 확인 기록</button>
      </div>}
      {(["rice", "mackerel"] as const).map(menu => <CombiQuality key={`${meal.meal.meal_batch_id}-${menu}`}
        menu={menu} mealId={meal.meal.meal_batch_id} version={meal.meal.version}
        safety={meal[menu].safety_status} jobStatus={meal[menu].status} current={meal[menu].quality_current}
        sourceReady={!meal.meal.source || (meal[menu].monitored_process_source?.raw_sha256 === meal.meal.source.raw_sha256 &&
          meal[menu].monitored_process_source?.phase === (menu === "rice" ? "RICE" : "MACKEREL"))}
        csrf={csrf} busy={busy} onBusy={setBusy} onUpdated={async () =>
          setMeal(await api<Meal>(`/api/customer/combi-meals/${encodeURIComponent(meal.meal.meal_batch_id)}`))} />)}
      {(["RICE", "MACKEREL"] as const).map(menu => <CombiRecommendation
        key={`combi-recommendation-${meal.meal.meal_batch_id}-${menu}-${meal.meal.version}`}
        mealId={meal.meal.meal_batch_id} jobId={menu === "RICE" ? meal.rice.job_id : meal.mackerel.job_id} menu={menu} csrf={csrf} />)}
      <CombiLive mealId={meal.meal.meal_batch_id} />
      <p>고등어 투입: {meal.transition.mackerel_load_allowed ? "허용" : "차단"}
        {meal.transition.humidity_verification_status && <> · 습도 검증 {meal.transition.humidity_verification_status} · 근거 {meal.transition.dryness_verification_basis}</>}</p>
      <p>시간 판정은 전체 공정·안전 판정과 별도입니다. 화면의 전환 자료는 합성 표본입니다.</p>
      {meal.events && <ul>{meal.events.map((event, index) => <li key={index}>{event.event} · {new Date(event.observed_at).toLocaleString()}</li>)}</ul>}
      <CombiQualityAggregate key={`combi-aggregate-${meal.meal.meal_batch_id}`} csrf={csrf} mealId={meal.meal.meal_batch_id}
        mealVersion={meal.meal.version} size={meal.meal.equipment_size} riceRecipe={meal.meal.rice_recipe_version} mackerelRecipe={meal.meal.mackerel_recipe_version} />
    </div>}
  </section>;
}
