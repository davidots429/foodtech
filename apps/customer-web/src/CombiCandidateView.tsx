import { useEffect, useRef, useState } from "react";
import { api } from "./cookingApi";
import { CombiProposalDetails } from "./CombiProposalDetails";
import { parseCombiCandidateReceipt } from "./combiCandidate";
import type { CombiReview } from "./combiReview";
import type { CombiScope } from "./combiRecipeResult";

type Receipt = ReturnType<typeof parseCombiCandidateReceipt>;
export function CombiCandidateView({ scope, review, csrf, canPreserve = false }: {
  scope: CombiScope; review: CombiReview; csrf: string; canPreserve?: boolean;
}) {
  const [receipt, setReceipt] = useState<Receipt | null>(null);
  const [acknowledged, setAcknowledged] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const inFlight = useRef(false), generation = useRef(0);
  const signature = JSON.stringify([scope, review, csrf]);
  useEffect(() => {
    const current = ++generation.current;
    setReceipt(null); setAcknowledged(false); setBusy(false); setError(""); inFlight.current = false;
    return () => { if (generation.current === current) ++generation.current; };
  }, [signature]);
  const path = `/api/customer/ai/combi/recipe-candidates/${review.version}`;
  async function load(preserve = false) {
    if (inFlight.current || (preserve && (!canPreserve || !acknowledged)) || review.decision !== "APPROVE") return;
    const current = generation.current;
    inFlight.current = true; setBusy(true); setError(""); setReceipt(null);
    try {
      const query = new URLSearchParams({ meal_id: scope.mealId, menu: scope.menu });
      const raw = preserve ? await api<unknown>(path, csrf, { meal_id: scope.mealId, menu: scope.menu,
        expected_meal_version: review.job_version, proposal_digest: review.proposal_digest, acknowledged: true }) :
        await api<unknown>(`${path}?${query}`);
      const checked = parseCombiCandidateReceipt(raw, scope, review);
      if (generation.current === current) { setReceipt(checked); setAcknowledged(false); }
    } catch (cause) {
      if (generation.current === current) {
        setAcknowledged(false);
        setError(preserve ? "후보 저장 여부를 확인하지 못했습니다. 다시 보존하기 전에 같은 검토 버전의 후보를 조회해 주세요." :
          cause instanceof Error ? cause.message : "후보를 조회하지 못했습니다.");
      }
    } finally { if (generation.current === current) { inFlight.current = false; setBusy(false); } }
  }
  if (review.decision !== "APPROVE") return null;
  const historical = receipt !== null && (receipt.current_meal_version !== review.job_version || receipt.current_review_version !== review.version);
  return <div className="binding-box"><h5>미적용 레시피 후보</h5>
    <p>승인 검토의 권고를 별도 후보 버전으로 보존합니다. 현재 식사배치·기존 레시피·안전 판정은 바꾸지 않습니다.</p>
    <button type="button" disabled={busy} onClick={() => void load()}>이 검토의 후보 조회</button>
    {canPreserve && <><label><input type="checkbox" checked={acknowledged} disabled={busy}
      onChange={event => setAcknowledged(event.target.checked)} />현재 승인 검토를 미적용 후보로 보존함을 확인했습니다.</label>
      <button type="button" disabled={busy || !acknowledged} onClick={() => void load(true)}>후보 버전 보존</button></>}
    {error && <p role="alert">{error}</p>}
    {receipt && <><p role="status">후보를 보존한 상태이며 아직 적용하지 않았습니다.</p>
      <p>후보 버전: <code>{receipt.candidate.candidate_id}</code></p>
      <p>원래 레시피 버전: {receipt.candidate.base_recipe_version ?? "없음 (버전 미지정)"} ·
        장비 규모: {receipt.candidate.equipment_size === "MEDIUM_FIXED_RACK_10" ? "중형 고정 랙 10단" : "대형 이동 랙 20단"}</p>
      {historical && <p>이후 배치 또는 검토가 변경되었습니다. 과거 후보이며 승인이 자동 복원되지 않습니다.</p>}
      <details><summary>보존된 후보의 조리조건</summary><CombiProposalDetails proposal={receipt.candidate.proposal} reviewed /></details>
    </>}
  </div>;
}
