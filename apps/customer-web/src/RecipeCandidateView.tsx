import { useState } from "react";
import { api } from "./cookingApi";
import { parseRecipeCandidate } from "./recipeCandidate";
import type { RecipeReview } from "./recipeReview";

export function RecipeCandidateView({ review }: { review: RecipeReview }) {
  const [view, setView] = useState<ReturnType<typeof parseRecipeCandidate> | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function load() {
    if (busy) return;
    setBusy(true); setError(""); setView(null);
    try {
      const raw = await api<unknown>(`/api/customer/ai/recipe-candidates/${review.version}?job_id=${encodeURIComponent(review.job_id)}`);
      setView(parseRecipeCandidate(raw, review));
    } catch (cause) { setError(cause instanceof Error ? cause.message : "후보 버전을 읽지 못했습니다."); }
    finally { setBusy(false); }
  }
  if (review.decision !== "APPROVE") return null;
  return <div className="recipe-candidate">
    <button type="button" disabled={busy} onClick={() => void load()}>승인으로 저장된 후보 버전 확인</button>
    {view && <>
      <dl><div><dt>후보 버전</dt><dd>{view.candidate.candidate_id}</dd></div>
        <div><dt>기준 레시피 버전</dt><dd>{view.candidate.base_recipe_version}</dd></div>
        <div><dt>장비 프로파일</dt><dd>{view.candidate.equipment_profile}</dd></div></dl>
      <p>합성 권고의 미적용 후보입니다. 기존 레시피와 조리·안전 상태를 변경하지 않습니다.</p>
      {view.current_job_version !== review.job_version && <p>후보 저장 이후 조리 기록이 변경되었습니다.</p>}
      {view.current_review_version !== review.version && <p>이 승인 이후 다른 검토가 기록되었습니다. 검토 이력을 확인해 주세요.</p>}
    </>}
    {error && <p role="alert">{error}</p>}
  </div>;
}
