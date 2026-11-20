import { useState } from "react";
import { api, jobPath, type CookingJob } from "./cookingApi";

export function TonkatsuBatch({ job, csrf, onSaved }: { job: CookingJob; csrf: string; onSaved: () => Promise<void> }) {
  const [weights, setWeights] = useState((job.tonkatsu_batch?.product_weights_g ?? []).join(", "));
  const [mode, setMode] = useState<"REFERENCE" | "PARTIAL">(job.tonkatsu_batch?.mode ?? "REFERENCE");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  async function save() {
    if (busy) return;
    const parts = weights.split(",").map(part => part.trim());
    if (parts.some(part => !/^\d+$/.test(part))) { setMessage("제품별 중량을 쉼표로 나누어 정수 g으로 입력해 주세요."); return; }
    setBusy(true); setMessage("");
    try {
      await api(jobPath(job.job_id) + "/tonkatsu-batch", csrf,
        { expected_version: job.version, mode, product_weights_g: parts.map(Number) });
      await onSaved(); setMessage("합성 배치를 기록했습니다.");
    } catch (cause) { setMessage(cause instanceof Error ? cause.message : "배치를 기록하지 못했습니다."); }
    finally { setBusy(false); }
  }
  const editable = ["PENDING", "READY"].includes(job.status);
  const reference = job.thickness_profile === "THIN" ? [18, 135] : job.thickness_profile === "STANDARD" ? [15, 160] :
    job.thickness_profile === "THICK" ? [12, 200] : null;
  return <section className="binding-box"><h4>돈까스 합성 배치</h4>
    <p className="small">정격 4.0kg · 기준 배치 2.3~2.5kg · 제품별 중량은 두께별 목표 ±5g입니다. 실제 장비 규격이 아닙니다.</p>
    {job.tonkatsu_batch && <p>기록됨: {job.tonkatsu_batch.product_count}개 · {job.tonkatsu_batch.total_mass_kg}kg · 정격 대비 {job.tonkatsu_batch.load_percent}%</p>}
    {editable && <><label>배치 유형 <select value={mode} onChange={event => setMode(event.target.value as typeof mode)}>
      <option value="REFERENCE">기준 배치</option><option value="PARTIAL">부분 배치</option></select></label>
      <label>제품별 중량(g) <input value={weights} onChange={event => setWeights(event.target.value)}
        placeholder="예: 160, 160, 160" /></label>
      {reference && <button type="button" className="secondary" disabled={busy} onClick={() => {
        setMode("REFERENCE"); setWeights(Array(reference[0]).fill(reference[1]).join(", "));
      }}>기준 배치 {reference[0]}개 × {reference[1]}g 채우기</button>}{" "}
      <button type="button" onClick={() => void save()} disabled={busy}>배치 기록</button></>}
    {message && <p role="status">{message}</p>}
  </section>;
}
