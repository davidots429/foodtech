import { TonkatsuBasisDetails } from "./TonkatsuBasisDetails";
import { RecipeCandidateView } from "./RecipeCandidateView";
import { useRef, useState } from "react";
import { api, ApiError } from "./cookingApi";
import { fryerSourceContract, parseRecipeReview, parseReviewContext, parseReviewPage } from "./recipeReview";
import type { RecipeReview, ReviewContext } from "./recipeReview";
import type { RecipeProposal } from "./recipeResult";

export function RecipeProposalReview({ jobId, executionId, jobVersion, proposal, csrf, onCreated, guardedFryer = false }: {
  jobId: string; executionId: string; jobVersion: number; proposal: RecipeProposal; csrf: string; guardedFryer?: boolean; onCreated?: (jobId: string) => void;
}) {
  const [context, setContext] = useState<ReviewContext | null>(null);
  const [decision, setDecision] = useState<"" | "APPROVE" | "REJECT">("");
  const [reason, setReason] = useState("");
  const [acknowledged, setAcknowledged] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [history, setHistory] = useState<RecipeReview[]>([]);
  const [nextCursor, setNextCursor] = useState<number | null>(null);
  const [historyJobVersion, setHistoryJobVersion] = useState<number | null>(null);
  const inFlight = useRef(false);
  const path = `/api/customer/ai/${guardedFryer ? "tonkatsu-" : ""}recipe-executions/${executionId}/review`;

  async function loadContext() {
    if (busy) return;
    setBusy(true); setContext(null); setError(""); setMessage(""); setAcknowledged(false);
    try {
      const raw = await api<unknown>(`${path}?job_id=${encodeURIComponent(jobId)}`);
      setContext(parseReviewContext(raw, executionId, jobId, jobVersion, proposal));
    } catch (cause) { setError(cause instanceof Error ? cause.message : "검토 기록을 읽지 못했습니다."); }
    finally { setBusy(false); }
  }
  async function save() {
    if (busy || inFlight.current || !context?.can_review || !decision || !reason.trim() || !acknowledged) return;
    inFlight.current = true; setBusy(true); setError(""); setMessage("");
    try {
      const raw = await api<{ review: unknown; job_changed: boolean }>(path, csrf, { job_id: jobId,
        expected_job_version: jobVersion, expected_review_version: context.review_version,
        proposal_digest: context.proposal_digest, request_id: crypto.randomUUID(), decision,
        reason: reason.trim(), acknowledged: true });
      const saved = parseRecipeReview(raw.review, jobId);
      if (typeof raw.job_changed !== "boolean" || (saved.source_contract === fryerSourceContract) !== guardedFryer ||
        saved.execution_id !== executionId ||
        saved.proposal_digest !== context.proposal_digest || saved.version !== context.review_version + 1 ||
        saved.job_version !== jobVersion || JSON.stringify(saved.proposal) !== JSON.stringify(context.proposal) ||
        saved.decision !== decision || saved.reason !== reason.trim()) throw new Error("검토 응답을 확인하지 못했습니다.");
      setContext({ ...context, review_version: saved.version, latest_review: saved, can_review: !raw.job_changed });
      setDecision(""); setReason(""); setAcknowledged(false); setHistory([]); setNextCursor(null);
      setMessage(saved.decision === "APPROVE" ? "검토 승인과 미적용 후보 버전을 함께 저장했습니다." :
        "검토 거절을 기록했습니다. 이전 후보 버전은 이력으로 보존됩니다.");
    } catch (cause) {
      setContext(null); setAcknowledged(false);
      setError(cause instanceof ApiError && cause.status !== 0 ? cause.message :
        "검토 응답을 확인하지 못했습니다. 다시 저장하기 전에 검토 기록을 조회해 주세요.");
    } finally { inFlight.current = false; setBusy(false); }
  }
  async function loadHistory(more = false) {
    if (busy || (more && nextCursor === null)) return;
    setBusy(true); setError("");
    try {
      const params = new URLSearchParams({ job_id: jobId, limit: "20" });
      if (more && nextCursor !== null) params.set("cursor", String(nextCursor));
      const raw = await api<unknown>(`/api/customer/ai/recipe-reviews?${params}`);
      const page = parseReviewPage(raw, jobId, more ? nextCursor : null);
      setHistory(old => more ? [...old, ...page.items] : page.items); setNextCursor(page.next_cursor);
      setHistoryJobVersion(page.current_job_version);
    } catch (cause) { setError(cause instanceof Error ? cause.message : "검토 이력을 읽지 못했습니다."); }
    finally { setBusy(false); }
  }
  const latest = context?.latest_review;
  const latestMatches = latest?.execution_id === executionId && latest?.proposal_digest === context?.proposal_digest;
  const fields = [["temperature_c", "조리 온도", "°C"], ["rpm", "교반 속도", "rpm"],
    ["duration_seconds", "조리 시간", "초"], ["load_kg", "조리량", "kg"],
    ["vegetable_interval_seconds", "채소 투입 간격", "초"], ["sauce_interval_seconds", "소스 투입 간격", "초"]] as const;
  return <section className="binding-box recipe-review" aria-label="권고 내용 검토">
    <h5>권고 내용 검토</h5>
    <p>승인 시 기존 레시피를 덮어쓰지 않는 후보 버전을 함께 보존합니다. 조리 조건 적용과 레시피 확정은 별도 확인이 필요합니다.</p>
    <button type="button" disabled={busy} onClick={() => void loadContext()}>현재 검토 기록 확인</button>
    {context && <>
      <p>검토 기록 버전: {context.review_version}</p>
      {latest && <p>{latestMatches ? "이 권고" : "현재 표시 내용과 다른 권고"}의 최근 검토:
        {latest.decision === "APPROVE" ? "검토 승인" : "검토 거절"} · {latest.reason} · {latest.reviewed_at}</p>}
      {latest && <RecipeCandidateView key={latest.version} review={latest} csrf={csrf} onCreated={onCreated} />}
      {!context.can_review && <p>권고 생성 이후 조리 기록이 변경되어 새 검토를 저장할 수 없습니다.</p>}
      <label>검토 결과<select value={decision} disabled={busy || !context.can_review}
        onChange={event => { setDecision(event.target.value as "" | "APPROVE" | "REJECT"); setAcknowledged(false); }}>
        <option value="">선택해 주세요</option><option value="APPROVE">검토 승인</option><option value="REJECT">검토 거절</option>
      </select></label>
      <label>검토 사유<textarea value={reason} disabled={busy || !context.can_review} maxLength={1000}
        onChange={event => { setReason(event.target.value); setAcknowledged(false); }} /></label>
      <label><input type="checkbox" checked={acknowledged} disabled={busy || !context.can_review}
        onChange={event => setAcknowledged(event.target.checked)} />표시된 권고와 작업 버전을 확인했습니다.</label>
      <button type="button" disabled={busy || !context.can_review || !decision || !reason.trim() || !acknowledged}
        onClick={() => void save()}>검토 결과 저장</button>
    </>}
    {message && <p role="status">{message}</p>}{error && <p role="alert">{error}</p>}
    <details><summary>이 작업의 검토 이력</summary>
      <button type="button" disabled={busy} onClick={() => void loadHistory()}>검토 이력 조회</button>
      {history.length > 0 && <ol>{history.map(item => <li key={item.version}>버전 {item.version} ·
        {item.execution_id === executionId ? "같은 실행의 기록" : "다른 실행의 기록"} · 작업 버전 {item.job_version} ·
        {item.decision === "APPROVE" ? "검토 승인" : "검토 거절"} · {item.reason} · {item.reviewed_at}
        {historyJobVersion !== null && item.job_version !== historyJobVersion && <p>조리 기록 변경 이전의 검토입니다.</p>}
        <RecipeCandidateView review={item} csrf={csrf} onCreated={onCreated} />
        <details><summary>검토 당시 권고 값</summary>{item.proposal.review_basis ? <TonkatsuBasisDetails proposal={item.proposal} /> :
          <dl>{fields.filter(([field]) => field in item.proposal.data)
          .map(([field, label, unit]) => <div key={field}><dt>{label}</dt><dd>{item.proposal.data[field]} {unit}</dd></div>)}</dl>}</details>
      </li>)}</ol>}
      {nextCursor !== null && <button type="button" disabled={busy} onClick={() => void loadHistory(true)}>다음 검토 이력</button>}
    </details>
  </section>;
}
