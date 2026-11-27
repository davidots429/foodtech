import { useEffect, useRef, useState } from "react";
import { CombiProposalDetails } from "./CombiProposalDetails";
import { CombiProposalReview } from "./CombiProposalReview";
import { api } from "./cookingApi";
import { readCombiRecord } from "./combiRecipeResult";
import type { CombiMenu, CombiResult, CombiStatus } from "./combiRecipeResult";

export { CombiProposalDetails } from "./CombiProposalDetails";

const stateLabel = { PENDING: "실행 대기", RESERVED: "실행 중", SUCCEEDED: "실행 완료", FAILED: "실행 실패" };

export function CombiRecommendation({ mealId, jobId, menu, csrf }: { mealId: string; jobId: string; menu: CombiMenu; csrf: string }) {
  const [executionId, setExecutionId] = useState("");
  const [status, setStatus] = useState<CombiStatus | null>(null);
  const [result, setResult] = useState<CombiResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const inFlight = useRef(false);
  const generation = useRef(0);
  useEffect(() => {
    const current = ++generation.current;
    setStatus(null); setResult(null); setExecutionId(""); setError(""); setBusy(false); inFlight.current = false;
    return () => { if (generation.current === current) ++generation.current; };
  }, [mealId, jobId, menu]);

  async function lookup() {
    if (inFlight.current) return;
    const current = generation.current;
    const scope = { executionId: executionId.trim(), mealId, jobId, menu };
    inFlight.current = true; setBusy(true); setError(""); setStatus(null); setResult(null);
    try {
      const saved = await readCombiRecord(scope, path => api<unknown>(path), () => generation.current === current);
      if (saved === null || generation.current !== current) return;
      setStatus(saved.status); setResult(saved.result);
    } catch (cause) {
      if (generation.current === current) {
        setStatus(null); setResult(null);
        setError(cause instanceof Error ? cause.message : "콤비 권고 기록을 조회하지 못했습니다.");
      }
    } finally {
      if (generation.current === current) { inFlight.current = false; setBusy(false); }
    }
  }
  return <section><h3>{menu === "RICE" ? "밥" : "고등어"} AI 권고 기록</h3>
    <p>이 식사배치의 권고 실행 ID로 상태와 저장된 내용을 조회합니다. 조회로 모델을 실행하거나 장비에 값을 전송하지 않습니다.</p>
    <div className="lookup-row"><label>권고 실행 ID<input maxLength={36} value={executionId} disabled={busy} onChange={event => {
      setExecutionId(event.target.value); setStatus(null); setResult(null); setError("");
    }} /></label>
    <button type="button" disabled={busy || !executionId.trim()} onClick={() => void lookup()}>
      {busy ? "조회 중…" : "상태·저장 권고 조회"}</button></div>
    {error && <p role="alert">{error}</p>}
    {status && <div aria-live="polite"><p>실행 상태: {stateLabel[status.state]}</p>
      {status.job_changed && <p role="status">이 권고 이후 식사배치가 변경되었습니다. 과거 기록이며 현재 작업에 적용하지 마세요.</p>}
      {result?.proposal && <><CombiProposalDetails proposal={result.proposal} />
        <CombiProposalReview key={`${result.execution_id}-${result.job_version}`} csrf={csrf}
          scope={{ executionId: result.execution_id, mealId, jobId, menu }} version={result.job_version} proposal={result.proposal} /></>}
      {result?.projection_status === "UNAVAILABLE" && <p>저장된 결과의 형식을 확인할 수 없어 권고를 표시하지 않습니다.</p>}
      {result?.projection_status === "FAILED" && <p>실행이 실패하여 표시할 권고가 없습니다.</p>}
    </div>}
    <p>AI 권고는 안전 확인이나 사용 승인이 아닙니다. 권고를 사용하기 전에 운영자의 검토·승인과 현재 작업의 안전 확인이 필요합니다.</p>
  </section>;
}
