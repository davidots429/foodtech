import { useState } from "react";
import { api, jobPath, type CookingJob } from "./cookingApi";

function localTime(value: string | null | undefined): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

export function TonkatsuInitialContext({ job, csrf, onSaved }: { job: CookingJob; csrf: string; onSaved: () => Promise<void> }) {
  const saved = job.tonkatsu_initial_context;
  const [temperature, setTemperature] = useState(saved?.ingredient_temperature_c ?? "");
  const [observed, setObserved] = useState(localTime(saved?.temperature_observed_at));
  const [storage, setStorage] = useState(saved?.storage_state ?? "UNKNOWN");
  const [capacity, setCapacity] = useState(saved?.oil_capacity_l ?? "");
  const [count, setCount] = useState(saved?.oil_batches_since_change?.toString() ?? "");
  const [mass, setMass] = useState(saved?.oil_cooked_mass_kg ?? "");
  const [changed, setChanged] = useState(localTime(saved?.oil_changed_at));
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const editable = ["PENDING", "READY"].includes(job.status) && !!job.tonkatsu_batch;
  const stale = !!saved && saved.batch_version !== job.tonkatsu_batch_version;
  async function save() {
    if (busy) return;
    if (!!temperature !== !!observed || (count && !/^\d+$/.test(count)) ||
        [observed, changed].some(value => value && Number.isNaN(new Date(value).getTime()))) {
      setMessage("온도와 관찰 시각을 함께 입력하고 배치 수와 시각을 확인해 주세요."); return;
    }
    setBusy(true); setMessage("");
    try {
      await api(jobPath(job.job_id) + "/tonkatsu-initial-context", csrf, {
        expected_version: job.version, ingredient_temperature_c: temperature || null,
        temperature_observed_at: observed ? new Date(observed).toISOString() : null,
        storage_state: storage, oil_capacity_l: capacity || null,
        oil_batches_since_change: count ? Number(count) : null, oil_cooked_mass_kg: mass || null,
        oil_changed_at: changed ? new Date(changed).toISOString() : null,
      });
      await onSaved(); setMessage("합성 초기 정보를 기록했습니다.");
    } catch (cause) { setMessage(cause instanceof Error ? cause.message : "초기 정보를 기록하지 못했습니다."); }
    finally { setBusy(false); }
  }
  return <section className="binding-box"><h4>재료 초기 상태와 기름 이력</h4>
    <p className="small">합성 시연값을 입력합니다. 기록된 값은 별도 검증 전이며 실제 측정이나 안전 확인을 의미하지 않습니다.</p>
    {!job.tonkatsu_batch && <p>먼저 제품별 배치를 기록해 주세요.</p>}
    {stale && <p className="warning">배치가 변경되어 이전 초기 정보는 현재 배치의 근거로 사용되지 않습니다.</p>}
    {saved && !stale && <><p>합성 입력 기록됨 · 검증 전 · 배치 v{saved.batch_version}</p>
      <p className="small">재료 온도 {saved.ingredient_temperature_c ?? "미기록"}°C · 보관 {({CHILLED: "냉장", FROZEN: "냉동", OTHER: "기타", UNKNOWN: "미확인"})[saved.storage_state]}
        {" "}· 기름 용량 {saved.oil_capacity_l ?? "미기록"}L · 교체 후 {saved.oil_batches_since_change ?? "미기록"}회 · 누적 {saved.oil_cooked_mass_kg ?? "미기록"}kg</p>
      <p className="small">온도 관찰 {saved.temperature_observed_at ? new Date(saved.temperature_observed_at).toLocaleString() : "미기록"}
        {" "}· 기름 교체 {saved.oil_changed_at ? new Date(saved.oil_changed_at).toLocaleString() : "미기록"}</p></>}
    {editable && <><div className="field-grid">
      <label>재료 초기 온도(°C) <input type="number" step="0.01" value={temperature} onChange={event => setTemperature(event.target.value)} /></label>
      <label>온도 관찰 시각 <input type="datetime-local" step="1" value={observed} onChange={event => setObserved(event.target.value)} /></label>
      <label>보관 상태 <select value={storage} onChange={event => setStorage(event.target.value as typeof storage)}>
        <option value="UNKNOWN">미확인</option><option value="CHILLED">냉장</option><option value="FROZEN">냉동</option><option value="OTHER">기타</option>
      </select></label>
      <label>기름 용량(L) <input type="number" min="0" step="0.01" value={capacity} onChange={event => setCapacity(event.target.value)} /></label>
      <label>교체 후 배치 수 <input type="number" min="0" step="1" value={count} onChange={event => setCount(event.target.value)} /></label>
      <label>교체 후 누적 조리량(kg) <input type="number" min="0" step="0.001" value={mass} onChange={event => setMass(event.target.value)} /></label>
      <label>기름 교체 시각 <input type="datetime-local" step="1" value={changed} onChange={event => setChanged(event.target.value)} /></label>
    </div><button type="button" onClick={() => void save()} disabled={busy}>초기 정보 기록</button></>}
    {message && <p role="status">{message}</p>}
  </section>;
}
