import { useRef, useState } from "react";
import { api, ApiError, jobPath } from "./cookingApi";
import { parseCandidateJobReceipt, parseCandidateJobRecovery } from "./candidateCookingJob";
import type { CandidateJobInput } from "./candidateCookingJob";
import type { RecipeCandidate } from "./recipeCandidate";
import { fryerSourceContract } from "./recipeReview";
import type { RecipeReview } from "./recipeReview";

export function CandidateCookingJob({ candidate, review, csrf, onCreated, eligible }: { candidate: RecipeCandidate;
  review: RecipeReview; csrf: string; onCreated?: (jobId: string) => void; eligible: boolean }) {
  const [portions, setPortions] = useState("");
  const [measurement, setMeasurement] = useState("");
  const [acknowledged, setAcknowledged] = useState(false);
  const [attempt, setAttempt] = useState<CandidateJobInput | null>(null);
  const [jobId, setJobId] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const inFlight = useRef(false);
  const jeyuk = candidate.proposal.menu === "JEYUK";
  const valid = Number.isSafeInteger(Number(portions)) && Number(portions) > 0 && Number(portions) <= 1000 &&
    measurement.trim() !== "" && Number.isFinite(Number(measurement)) && Number(measurement) > 0;
  async function create() {
    if (inFlight.current || busy || attempt || !eligible || !valid || !acknowledged) return;
    inFlight.current = true; setBusy(true); setError("");
    const input: CandidateJobInput = { request_id: crypto.randomUUID(), expected_job_version: candidate.job_version,
      proposal_digest: candidate.proposal_digest, planned_portions: Number(portions),
      thickness_cm: jeyuk ? null : measurement, actual_load_kg: jeyuk ? measurement : null, acknowledged: true };
    setAttempt(input);
    try {
      const prefix = candidate.source_contract === fryerSourceContract ? "tonkatsu-" : "";
      const raw = await api<unknown>(`/api/customer/ai/${prefix}recipe-candidates/${candidate.review_version}/cooking-jobs?job_id=${candidate.job_id}`, csrf, input);
      const receipt = parseCandidateJobReceipt(raw, candidate, input);
      setJobId(receipt.job_id); onCreated?.(receipt.job_id);
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 422) {
        // This endpoint validates input before its transaction; no job was written.
        setAttempt(null); setAcknowledged(false); setError(cause.message);
      } else setError("생성 결과를 확인하지 못했습니다. 자동 재전송하지 않았습니다. 생성 요청 ID로 작업을 조회해 주세요.");
    }
    finally { inFlight.current = false; setBusy(false); }
  }
  async function recover() {
    if (busy || !attempt) return;
    setBusy(true); setError("");
    try {
      const raw = await api<unknown>(jobPath(attempt.request_id));
      const receipt = parseCandidateJobRecovery(raw, candidate, review, attempt);
      setJobId(receipt.job_id); onCreated?.(receipt.job_id);
    } catch { setError("이 생성 요청의 작업을 확인하지 못했습니다. 같은 요청 ID로 다시 조회해 주세요. 새 생성 요청을 자동으로 보내지 않습니다."); }
    finally { setBusy(false); }
  }
  return <div className="candidate-job">
    <h6>후보를 참조하는 새 합성 작업</h6>
    <p>새 배치의 수량과 측정값을 입력합니다. 권고는 계획 참조로 연결하며 장비 설정·공정 판정 기준을 자동 변경하지 않습니다. 안전과 품질 평가는 새로 진행해야 합니다.</p>
    {!attempt && !eligible && <p>현재 승인·작업 상태로는 새 작업을 만들 수 없습니다. 후보를 다시 확인해 주세요.</p>}
    {!attempt && eligible && <>
      <label>새 작업 예정 인분<input type="number" min="1" max="1000" step="1" value={portions} disabled={busy}
        onChange={e => { setPortions(e.target.value); setAcknowledged(false); }} /></label>
      <label>{jeyuk ? "새 작업 실제 투입량 (kg)" : "새 작업 두께 (cm)"}<input type="number" min="0.01" step="0.01" value={measurement} disabled={busy}
        onChange={e => { setMeasurement(e.target.value); setAcknowledged(false); }} /></label>
      <label><input type="checkbox" checked={acknowledged} disabled={busy}
        onChange={e => setAcknowledged(e.target.checked)} />새 작업의 안전·평가를 처음부터 진행하고 권고를 자동 적용하지 않음을 확인했습니다.</label>
      <button type="button" disabled={busy || !valid || !acknowledged} onClick={() => void create()}>후보를 연결해 새 작업 만들기</button>
    </>}
    {attempt && <p>생성 요청 ID: <code>{attempt.request_id}</code></p>}
    {jobId ? <p role="status">새 작업 ID: <code>{jobId}</code> · 조리 작업에서 불러올 수 있습니다.</p> :
      attempt && <button type="button" disabled={busy} onClick={() => void recover()}>생성 요청 ID로 작업 조회</button>}
    {error && <p role="alert">{error}</p>}
  </div>;
}
