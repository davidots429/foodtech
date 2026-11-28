import { useRef, useState } from "react";
export type CandidateProduct = { product_id: string; rack_id: string; shelf: number; pan_id: string; mass_kg: string; thickness_cm: string };
export type CandidateLoad = { observed_at: string; products: CandidateProduct[]; candidate_rack_order: string[]; pan_tare_kg: string };
const blank = () => ({ product_id: "", rack_id: "", shelf: "", pan_id: "", mass_kg: "", thickness_cm: "" });
export function CombiCandidateLoad({ positions, large, busy, onSubmit }: {
  positions: { rack: number; shelf: number }[]; large: boolean; busy: boolean; onSubmit: (body: CandidateLoad) => Promise<void>;
}) {
  const [rackCount, setRackCount] = useState(""); const [racks, setRacks] = useState(["", ""]);
  const [products, setProducts] = useState([blank()]); const [tare, setTare] = useState("");
  const [observed, setObserved] = useState(""); const [ack, setAck] = useState(false); const [error, setError] = useState("");
  const active = useRef(false);
  async function submit() {
    if (active.current || busy || !ack) return;
    const selected = racks.slice(0, Number(rackCount)).map(v => v.trim()), at = new Date(observed);
    if (!rackCount || selected.some(v => !v) || new Set(selected).size !== selected.length || !observed || !Number.isFinite(at.getTime()) ||
      !tare.trim() || !Number.isFinite(Number(tare)) || Number(tare) < 0 || products.some(p =>
        Object.values(p).some(v => !v.trim()) || !selected.includes(p.rack_id) || !Number.isInteger(Number(p.shelf)) || Number(p.shelf) < 1 || Number(p.shelf) > 10 ||
        !Number.isFinite(Number(p.mass_kg)) || !Number.isFinite(Number(p.thickness_cm)))) { setError("실제 설치 랙·제품·팬·중량·시각을 모두 확인해 주세요."); return; }
    active.current = true; setError("");
    try { await onSubmit({ observed_at: at.toISOString(), candidate_rack_order: selected, pan_tare_kg: tare,
      products: products.map(p => ({ ...p, shelf: Number(p.shelf) })) }); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "투입 기록을 저장하지 못했습니다."); }
    finally { active.current = false; }
  }
  return <fieldset disabled={busy}><legend>후보 배치도의 새 고등어 적재</legend>
    <p>확인할 위치: {positions.map(p => `논리 랙 ${p.rack} / ${p.shelf}단`).join(", ")}. 실제 랙 ID를 논리 번호 순서로 입력합니다.
      비어 있어도 설치한 랙은 포함합니다. 대형 이동식 랙은 각각 15kg을 반영합니다.</p>
    <label>이번에 설치한 랙 수<select value={rackCount} onChange={e => { setRackCount(e.target.value); setAck(false); }}>
      <option value="">선택</option><option value="1">1개</option>{large && <option value="2">2개</option>}</select></label>
    <div className="field-row">{Array.from({ length: Number(rackCount) }, (_, i) => <label key={i}>논리 랙 {i + 1}의 실제 ID
      <input maxLength={80} value={racks[i]} onChange={e => { setRacks(old => old.map((v, n) => n === i ? e.target.value : v)); setAck(false); }} /></label>)}</div>
    {products.map((p, i) => <fieldset key={i}><legend>이번 합성 제품 {i + 1}</legend><div className="field-row">
      {(Object.keys(p) as (keyof typeof p)[]).map(name => <label key={name}>
        {{ product_id: "제품 ID", rack_id: "실제 랙 ID", shelf: "선반(단)", pan_id: "팬 ID", mass_kg: "제품 중량(kg, 0.12~0.15)", thickness_cm: "두께(cm, 1.5~2.0)" }[name]}
        <input maxLength={80} value={p[name]} onChange={e => { setProducts(old => old.map((row, n) => n === i ? { ...row, [name]: e.target.value } : row)); setAck(false); }} /></label>)}</div>
      {products.length > 1 && <button type="button" onClick={() => { setProducts(old => old.filter((_, n) => n !== i)); setAck(false); }}>이 제품 제거</button>}
    </fieldset>)}
    <button type="button" disabled={products.length >= 200} onClick={() => { setProducts(old => [...old, blank()]); setAck(false); }}>제품 추가</button>
    <div className="field-row"><label>이번 빈 팬 총중량(kg)<input type="number" min="0" step="0.01" value={tare} onChange={e => { setTare(e.target.value); setAck(false); }} /></label>
      <label>합성 투입 관찰시각<input type="datetime-local" step="0.001" value={observed} onChange={e => { setObserved(e.target.value); setAck(false); }} /></label></div>
    <button type="button" onClick={() => { const now = new Date();
      setObserved(new Date(now.getTime() - now.getTimezoneOffset() * 60000).toISOString().slice(0, 23)); setAck(false);
    }}>현재 시각 입력</button>
    <label><input type="checkbox" checked={ack} onChange={e => setAck(e.target.checked)} />이번 제품의 팬·랙 위치와 중량을 새로 확인했습니다.</label>
    <button type="button" disabled={!ack} onClick={() => void submit()}>후보 배치도 확인·고등어 투입 기록</button>
    {error && <p role="alert">{error}</p>}
  </fieldset>;
}
