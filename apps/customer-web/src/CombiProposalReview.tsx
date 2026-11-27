import { useEffect, useRef, useState } from "react";
import { CombiCandidateView } from "./CombiCandidateView";
import { api } from "./cookingApi";
import { parseCombiReviewReceipt, parseCombiReviewContext, parseCombiReviewPage } from "./combiReview";
import type { CombiReview, CombiReviewContext } from "./combiReview";
import type { CombiProposal, CombiScope } from "./combiRecipeResult";

export function CombiProposalReview({ scope, version, proposal, csrf }: {
  scope: CombiScope; version: number; proposal: CombiProposal; csrf: string;
}) {
  const [context, setContext] = useState<CombiReviewContext | null>(null);
  const [decision, setDecision] = useState<"" | "APPROVE" | "REJECT">("");
  const [reason, setReason] = useState("");
  const [acknowledged, setAcknowledged] = useState(false);
  const [history, setHistory] = useState<CombiReview[]>([]);
  const [cursor, setCursor] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const pending = useRef(false);
  const generation = useRef(0);
  const signature = JSON.stringify([scope, version, proposal, csrf]);
  useEffect(() => {
    const current = ++generation.current;
    setContext(null); setDecision(""); setReason(""); setAcknowledged(false); setHistory([]);
    setCursor(null); setError(""); setMessage(""); setBusy(false); pending.current = false;
    return () => { if (generation.current === current) ++generation.current; };
  }, [signature]);
  const path = `/api/customer/ai/combi/recipe-executions/${encodeURIComponent(scope.executionId)}/review`;
  const params = new URLSearchParams({ meal_id: scope.mealId, menu: scope.menu });
  async function run(action: (current: () => boolean) => Promise<void>) {
    if (pending.current) return;
    const current = generation.current;
    pending.current = true; setBusy(true); setError(""); setMessage("");
    try { await action(() => generation.current === current); }
    catch (cause) {
      if (generation.current === current) {
        setContext(null); setAcknowledged(false);
        setError(cause instanceof Error ? cause.message : "검토 처리 여부를 확인하지 못했습니다. 검토 이력을 조회해 주세요.");
      }
    } finally { if (generation.current === current) { pending.current = false; setBusy(false); } }
  }
  async function loadContext() {
    await run(async current => {
      setContext(null); setAcknowledged(false);
      const value = parseCombiReviewContext(await api<unknown>(`${path}?${params}`), scope, version, proposal);
      if (current()) setContext(value);
    });
  }
  async function save() {
    if (!context?.can_review || !decision || !reason.trim() || !acknowledged) return;
    const body = { meal_id: scope.mealId, menu: scope.menu, expected_meal_version: version,
      expected_review_version: context.review_version, request_id: crypto.randomUUID(),
      proposal_digest: context.proposal_digest, decision, reason: reason.trim(), acknowledged: true };
    await run(async current => {
      let raw: unknown;
      try { raw = await api(path, csrf, body); }
      catch { throw new Error("검토 저장 여부를 확인하지 못했습니다. 다시 저장하기 전에 검토 이력을 조회해 주세요."); }
      const saved = parseCombiReviewReceipt(raw, scope, version, context, body);
      if (!current()) return;
      setContext({ ...context, review_version: saved.version, latest_review: saved });
      setDecision(""); setReason(""); setAcknowledged(false); setHistory([]); setCursor(null);
      setMessage("검토 결과를 기록했습니다. 조리 상태·안전 판정·레시피 적용은 변경되지 않았습니다.");
    });
  }
  async function loadHistory(more = false) {
    if (more && cursor === null) return;
    await run(async current => {
      const query = new URLSearchParams(params); query.set("limit", "20");
      if (more && cursor !== null) query.set("cursor", String(cursor));
      const page = parseCombiReviewPage(await api<unknown>(`/api/customer/ai/combi/recipe-reviews?${query}`), scope, more ? cursor : null);
      if (current()) { setHistory(old => more ? [...old, ...page.items] : page.items); setCursor(page.next_cursor); }
    });
  }
  return <section className="recipe-review" aria-label="콤비 권고 검토"><h4>권고 내용 검토</h4>
    <p>표시된 권고의 승인·반려와 사유를 기록합니다. 검토 승인은 조리조건 적용이나 안전 합격 판정이 아닙니다.</p>
    <button type="button" disabled={busy} onClick={() => void loadContext()}>현재 검토 기록 확인</button>
    {context && <><p>메뉴별 검토 버전: {context.review_version}</p>
      {context.latest_review && <p>최근 검토: {context.latest_review.decision === "APPROVE" ? "승인" : "반려"} · {context.latest_review.reason}
        {context.latest_review.execution_id !== scope.executionId && " (다른 실행의 검토)"}</p>}
      {context.latest_review?.decision === "APPROVE" && <CombiCandidateView
        key={`current-${context.latest_review.version}`} scope={scope} csrf={csrf} review={context.latest_review}
        canPreserve={context.can_review && context.latest_review.execution_id === scope.executionId &&
          context.latest_review.proposal_digest === context.proposal_digest} />}
      {!context.can_review && <p>식사배치가 변경되어 과거 권고에 새 검토를 저장할 수 없습니다.</p>}
      <label>검토 결과<select value={decision} disabled={busy || !context.can_review} onChange={event => {
        setDecision(event.target.value as "" | "APPROVE" | "REJECT"); setAcknowledged(false);
      }}><option value="">선택해 주세요</option><option value="APPROVE">검토 승인</option><option value="REJECT">검토 반려</option></select></label>
      <label>검토 사유<textarea maxLength={1000} value={reason} disabled={busy || !context.can_review} onChange={event => {
        setReason(event.target.value); setAcknowledged(false);
      }} /></label>
      <label><input type="checkbox" checked={acknowledged} disabled={busy || !context.can_review}
        onChange={event => setAcknowledged(event.target.checked)} />표시된 메뉴·권고·식사배치 버전을 확인했습니다.</label>
      <button type="button" disabled={busy || !context.can_review || !decision || !reason.trim() || !acknowledged}
        onClick={() => void save()}>검토 결과 저장</button></>}
    {message && <p role="status">{message}</p>}{error && <p role="alert">{error}</p>}
    <details><summary>이 메뉴의 검토 이력</summary>
      <button type="button" disabled={busy} onClick={() => void loadHistory()}>검토 이력 조회</button>
      <ol>{history.map(item => <li key={item.version}>검토 버전 {item.version} · 식사배치 버전 {item.job_version} ·
        {item.decision === "APPROVE" ? "승인" : "반려"} · {item.reason} · {item.reviewed_at}
        {item.execution_id !== scope.executionId && " (다른 실행의 검토)"}
        {item.decision === "APPROVE" && <CombiCandidateView scope={scope} review={item} csrf={csrf} />}</li>)}</ol>
      {cursor !== null && <button type="button" disabled={busy} onClick={() => void loadHistory(true)}>다음 검토 이력</button>}
    </details>
  </section>;
}
