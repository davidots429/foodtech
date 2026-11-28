import { useEffect, useRef, useState } from "react";
import { api, ApiError } from "./cookingApi";
import { parseCandidateMealReceipt } from "./combiCandidateMeal";
import type { CandidateMealInput, FreshCombiMeal } from "./combiCandidateMeal";
import type { CombiCandidate } from "./combiCandidate";
import type { CombiReview } from "./combiReview";

export function CombiCandidateMeal({ candidate, review, csrf, onCreated }: {
  candidate: CombiCandidate; review: CombiReview; csrf: string; onCreated?: (id: string) => Promise<void>;
}) {
  const [values, setValues] = useState({ rack_count: "", pan_count: "", dry_rice_kg: "", drained_rice_kg: "", added_water_kg: "", pan_tare_kg: "", rack_tare_kg: "" });
  const [otherRecipe, setOtherRecipe] = useState("");
  const [ack, setAck] = useState(false), [busy, setBusy] = useState(false), [error, setError] = useState("");
  const [saved, setSaved] = useState<string | null>(null), [pending, setPending] = useState<CandidateMealInput | null>(null);
  const active = useRef(false), generation = useRef(0);
  const signature = JSON.stringify([candidate, review, csrf]);
  useEffect(() => {
    const current = ++generation.current;
    setValues({ rack_count: "", pan_count: "", dry_rice_kg: "", drained_rice_kg: "", added_water_kg: "", pan_tare_kg: "", rack_tare_kg: "" });
    setOtherRecipe(""); setPending(null); setSaved(null); setAck(false); setBusy(false); setError(""); active.current = false;
    return () => { if (generation.current === current) ++generation.current; };
  }, [signature]);
  async function submit(retry = false) {
    if (active.current || (!retry && (!ack || pending)) || (retry && !pending)) return;
    const current = generation.current;
    let body = pending;
    if (!retry) {
      const fresh = { ...values, equipment_size: candidate.equipment_size, rack_count: Number(values.rack_count), pan_count: Number(values.pan_count) } as FreshCombiMeal;
      if (Object.values(values).some(v => !v.trim()) || !Number.isInteger(fresh.rack_count) || !Number.isInteger(fresh.pan_count) ||
        Object.values(values).some(v => !Number.isFinite(Number(v)))) { setError("이번 배치의 새 중량과 랙·팬 수를 모두 입력해 주세요."); return; }
      if (otherRecipe.trim()) fresh[candidate.menu === "RICE" ? "mackerel_recipe_version" : "rice_recipe_version"] = otherRecipe.trim();
      body = { request_id: crypto.randomUUID(), meal_id: candidate.meal_id, menu: candidate.menu, expected_meal_version: candidate.job_version,
        proposal_digest: candidate.proposal_digest, confirmed_pan_positions: candidate.proposal.data.pan_positions.map(p => ({ ...p })), new_meal: fresh, acknowledged: true };
      setPending(body);
    }
    if (!body) return;
    active.current = true; setBusy(true); setError("");
    try {
      const raw = await api<unknown>(`/api/customer/ai/combi/recipe-candidates/${candidate.review_version}/meals`, csrf, body);
      const receipt = parseCandidateMealReceipt(raw, candidate, review, body);
      if (generation.current !== current) return;
      setSaved(receipt.meal_id); setPending(null); setAck(false);
    } catch (cause) { if (generation.current === current) {
      setError(cause instanceof Error ? cause.message : "생성 여부를 확인하지 못했습니다.");
      if (cause instanceof ApiError && cause.status === 422) { setPending(null); setAck(false); }
    } }
    finally { if (generation.current === current) { active.current = false; setBusy(false); } }
  }
  const labels = { rack_count: "밥 조리에 설치할 랙 수", pan_count: "밥 팬 수", dry_rice_kg: "새 마른쌀 중량(kg)",
    drained_rice_kg: "새 배수 후 쌀 중량(kg)", added_water_kg: "실제 추가할 물(kg)", pan_tare_kg: "밥 빈 팬 총중량(kg)", rack_tare_kg: "밥 이동식 랙 총중량(kg, 중형은 0)" };
  return <fieldset disabled={busy}><legend>후보로 새 식사배치 만들기</legend>
    <p>기존 배치를 보존하고 선택 메뉴의 조리계획을 새 작업에 적용합니다. 고등어 후보라도 선행하는 밥 조리를 위한 새 입력이 필요합니다.
      전처리·제품 적재·공정·안전·품질은 이번 작업에서 다시 기록합니다. 장비로 값을 전송하지 않습니다.</p>
    <p>확인할 팬 배치: {candidate.proposal.data.pan_positions.map(p => `논리 랙 ${p.rack} / ${p.shelf}단`).join(", ")}</p>
    <div className="field-row">{(Object.keys(labels) as (keyof typeof labels)[]).map(name => <label key={name}>{labels[name]}
      <input type="number" min="0" step={name.endsWith("count") ? "1" : "0.01"} value={values[name]} disabled={pending !== null || saved !== null}
        onChange={e => { setValues(old => ({ ...old, [name]: e.target.value })); setAck(false); }} /></label>)}</div>
    <label>다른 메뉴의 새 기준 레시피 버전 (선택)<input maxLength={80} value={otherRecipe} disabled={pending !== null || saved !== null} onChange={e => { setOtherRecipe(e.target.value); setAck(false); }} /></label>
    <label><input type="checkbox" checked={ack} disabled={pending !== null || saved !== null} onChange={e => setAck(e.target.checked)} />보존된 후보의 팬 배치와 새 입력을 확인했고, 새 전처리·적재·안전 확인이 필요함을 이해했습니다.</label>
    <button type="button" disabled={!ack || pending !== null || saved !== null} onClick={() => void submit()}>새 식사배치 생성</button>
    {pending && <><p>요청 ID: <code>{pending.request_id}</code>. 입력과 요청 ID를 유지하고 있습니다. 다른 요청을 만들기 전에 같은 요청으로 생성 여부를 확인해 주세요.</p>
      <button type="button" onClick={() => void submit(true)}>같은 요청으로 확인·재시도</button></>}
    {error && <p role="alert">{error}</p>}
    {saved && <p role="status">새 식사배치가 생성되어 있습니다: <code>{saved}</code></p>}
    {saved && onCreated && <button type="button" disabled={false} onClick={() => { void onCreated(saved).catch(() => setError("새 배치를 열지 못했습니다. 표시된 ID로 배치를 조회해 주세요.")); }}>생성된 배치 열기</button>}
  </fieldset>;
}
