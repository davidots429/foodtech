import { useRef, useState } from "react";
import { api, scoreItems, type ScoreKey } from "./cookingApi";

type CurrentQuality = { version: number; quality_status: string; score_percent: string; quality_target: boolean };
type Props = { menu: "rice" | "mackerel"; mealId: string; version: number; safety: string; jobStatus: string;
  current?: CurrentQuality; csrf: string; busy: boolean; onBusy: (value: boolean) => void; onUpdated: () => Promise<void> };

const reasons = {
  rice: [["UNDERCOOKED", "설익음"], ["EXCESS_MOISTURE", "수분 과다"], ["OVERCOOKED", "과조리"],
    ["UNEVEN_COOK", "불균일 조리"], ["BAD_SMELL", "이취"], ["BURNT_BOTTOM", "바닥 눌음"], ["RICE_CLUMPED", "밥 뭉침"]],
  mackerel: [["UNDERCOOKED", "설익음"], ["EXCESS_MOISTURE", "수분 과다"], ["OVERCOOKED", "과조리"],
    ["UNEVEN_COOK", "불균일 조리"], ["BAD_SMELL", "이취"], ["BURNT_SURFACE", "표면 탄화"],
    ["DRY_TEXTURE", "과도한 건조"], ["SKIN_NOT_CRISP", "껍질 식감 부족"]],
};
const details = {
  rice: "팬별 익힘 균일성, 설익음·경도, 수분·건조, 뭉침과 위치 편차를 살펴보세요.",
  mackerel: "갈변, 껍질 식감, 내부 수분·건조, 비린내·탄화와 선반별 편차를 살펴보세요.",
};

export function CombiQuality({ menu, mealId, version, safety, jobStatus, current, csrf, busy, onBusy, onUpdated }: Props) {
  const [scores, setScores] = useState<Partial<Record<ScoreKey, number>>>({});
  const [observations, setObservations] = useState<Partial<Record<ScoreKey, string>>>({});
  const [decision, setDecision] = useState<"QUALITY_PASSED" | "QUALITY_FAILED">("QUALITY_PASSED");
  const [selected, setSelected] = useState<string[]>([]);
  const [planned, setPlanned] = useState("3");
  const [actual, setActual] = useState("3");
  const [satisfaction, setSatisfaction] = useState("");
  const [reason, setReason] = useState("");
  const [note, setNote] = useState("");
  const [error, setError] = useState("");
  const active = useRef(false);
  const canAssess = safety === "PASSED" && jobStatus === "UNLOADED";

  async function submit() {
    if (busy || !canAssess || active.current) return;
    active.current = true;
    onBusy(true); setError("");
    try {
      await api(`/api/customer/combi-meals/${encodeURIComponent(mealId)}/quality/${menu}`, csrf, {
        expected_version: version, expected_current_version: current?.version ?? 0,
        kind: current ? "REASSESSMENT" : "INITIAL", reason: current ? reason.trim() : null,
        scores, observations, quality_decision: decision,
        quality_reason_codes: decision === "QUALITY_FAILED" ? selected : [], quality_note: note.trim() || null,
        overall_satisfaction: satisfaction ? Number(satisfaction) : null,
        planned_portions: Number(planned), actual_portions: Number(actual),
      });
      await onUpdated();
      setReason("");
    } catch (cause) { setError(cause instanceof Error ? cause.message : "품질평가를 저장하지 못했습니다."); }
    finally { active.current = false; onBusy(false); }
  }

  return <div className="binding-box"><h4>{menu === "rice" ? "밥" : "고등어"} 품질평가</h4><p>{details[menu]}</p>
    {current && <p>현재 평가 v{current.version} · {current.quality_status === "QUALITY_PASSED" ? "합격" : "불합격"} · {current.score_percent}점 · 목표 {current.quality_target ? "달성" : "미달"}</p>}
    {!canAssess ? <p>인양과 안전 확인이 완료되면 평가할 수 있습니다. 현재 안전 상태: {safety}</p> : <>
      {scoreItems.map(item => <div key={item.key} className="field-row"><label>{item.label} 점수
        <select value={scores[item.key] ?? ""} onChange={event => setScores(previous => ({ ...previous,
          [item.key]: event.target.value ? Number(event.target.value) : undefined }))}>
          <option value="">선택</option>{[1, 2, 3, 4, 5].map(value => <option key={value} value={value}>{value}</option>)}
        </select></label><label className="grow">관찰 근거
        <input maxLength={500} value={observations[item.key] ?? ""}
          onChange={event => setObservations(previous => ({ ...previous, [item.key]: event.target.value }))} /></label></div>)}
      <div className="field-row"><label>계획 인분<input type="number" min="1" max="1000" value={planned} onChange={event => setPlanned(event.target.value)} /></label>
        <label>실제 인분<input type="number" min="1" max="1000" value={actual} onChange={event => setActual(event.target.value)} /></label>
        <label>전반적 만족도<select value={satisfaction} onChange={event => setSatisfaction(event.target.value)}><option value="">선택 안 함</option>
          {[1, 2, 3, 4, 5].map(value => <option key={value} value={value}>{value}</option>)}</select></label></div>
      <label>품질 판단<select value={decision} onChange={event => setDecision(event.target.value as typeof decision)}>
        <option value="QUALITY_PASSED">합격</option><option value="QUALITY_FAILED">불합격</option></select></label>
      {decision === "QUALITY_FAILED" && <fieldset><legend>품질 실패 사유</legend>{reasons[menu].map(([key, label]) =>
        <label key={key}><input type="checkbox" checked={selected.includes(key)} onChange={event =>
          setSelected(previous => event.target.checked ? [...previous, key] : previous.filter(item => item !== key))} />{label}</label>)}</fieldset>}
      <label>추가 의견<input maxLength={500} value={note} onChange={event => setNote(event.target.value)} /></label>
      {current && <label>재평가 사유<input maxLength={500} value={reason} onChange={event => setReason(event.target.value)} /></label>}
      <button type="button" disabled={busy} onClick={submit}>{current ? "재평가 기록" : "품질평가 완료"}</button>
    </>}
    {error && <p className="error" role="alert">{error}</p>}
  </div>;
}
