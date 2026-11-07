import { useRef, useState } from "react";

type Quote = {
  quote_id: string;
  execution_id: string;
  model_id: string;
  provider: string;
  fixture_id: string;
  notice_version: string;
  estimated_cost_usd: string;
  expires_at: string;
  transmission_location: "EXTERNAL";
  data_classification: "SYNTHETIC";
};
type Outcome = {
  execution_id: string;
  state: string;
  response_text: string | null;
  error_code: string | null;
  actual_cost_usd?: string | null;
  settlement_status?: string | null;
};

export function ExternalAiTrial({ csrf }: { csrf: string }) {
  const [credentialId, setCredentialId] = useState("");
  const [quote, setQuote] = useState<Quote | null>(null);
  const [agreed, setAgreed] = useState(false);
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const active = useRef(false);

  async function preview() {
    if (active.current) return;
    active.current = true;
    setBusy(true); setError(""); setQuote(null); setOutcome(null); setAgreed(false);
    try {
      const response = await fetch("/api/customer/ai/external/preview", {
        method: "POST", credentials: "same-origin", cache: "no-store",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
        body: JSON.stringify({ model_id: "gpt-4o-mini", credential_id: credentialId.trim() }),
      });
      if (!response.ok) throw new Error("견적을 만들지 못했습니다. 모델·자격증명·한도 또는 시연 설정을 확인해 주세요.");
      setQuote(await response.json() as Quote);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "견적을 만들지 못했습니다.");
    } finally { active.current = false; setBusy(false); }
  }

  async function confirm() {
    if (!quote || !agreed || active.current) return;
    active.current = true;
    setBusy(true); setError("");
    try {
      const response = await fetch(`/api/customer/ai/external/quotes/${quote.quote_id}/confirm`, {
        method: "POST", credentials: "same-origin", cache: "no-store",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
        body: JSON.stringify({ notice_version: quote.notice_version }),
      });
      if (!response.ok) throw new Error("결과를 확인하지 못했습니다. 같은 견적을 다시 실행하지 말고 실행 상태를 조회해 주세요.");
      setOutcome(await response.json() as Outcome);
      setQuote(null);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "결과를 확인하지 못했습니다.");
    } finally { active.current = false; setBusy(false); }
  }

  async function checkStatus() {
    if (!quote || active.current) return;
    active.current = true; setBusy(true); setError("");
    try {
      const response = await fetch(`/api/customer/ai/executions/${quote.execution_id}/status`, {
        credentials: "same-origin", cache: "no-store",
      });
      if (!response.ok) throw new Error("실행 상태를 읽지 못했습니다.");
      setOutcome(await response.json() as Outcome);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "실행 상태를 읽지 못했습니다.");
    } finally { active.current = false; setBusy(false); }
  }

  return <section aria-labelledby="external-ai-title">
    <h2 id="external-ai-title">외부 AI 합성 시험</h2>
    <p>서버가 만든 합성 조리 질문만 전송합니다. 실제 고객 질문·레시피·장비 데이터는 이 기능에서 전송하지 않습니다.</p>
    <p className="small">플랫폼 유료 티어의 실제 과금은 없는 비영리 시연입니다. 외부 AI 공급자 API는 등록한 키의 계정에 비용을 청구할 수 있으므로, 관리자가 시험을 활성화한 경우에만 실행됩니다.</p>
    <label>등록한 자격증명 ID
      <input value={credentialId} onChange={(event) => { setCredentialId(event.target.value); setQuote(null); setAgreed(false); }}
        maxLength={128} autoComplete="off" disabled={busy} />
    </label>
    <button type="button" onClick={preview} disabled={busy || !credentialId.trim()}>견적 확인</button>
    {quote && <div className="card">
      <p>모델: {quote.model_id} · 공급자: {quote.provider} · 전송 위치: 외부 · 자료: 합성 예시</p>
      <p>요청 전 예약할 비용 상한: 최대 ${quote.estimated_cost_usd} USD · 견적 만료: {new Date(quote.expires_at).toLocaleString()}</p>
      <p className="small">표시 금액은 승인된 가격 상한에 따른 추정치입니다. 공급자 청구 반영과 실제 정산액에는 차이가 있을 수 있습니다. 이 실행은 합성 질문을 외부 공급자에 보내며, 등록한 공급자 계정에 API 비용이 발생할 수 있습니다.</p>
      <label><input type="checkbox" checked={agreed} onChange={(event) => setAgreed(event.target.checked)} /> 이 실행의 외부 전송과 비용 가능성에 동의합니다.</label>
      <div className="actions"><button type="button" onClick={confirm} disabled={!agreed || busy || Date.parse(quote.expires_at) <= Date.now()}>동의하고 실행</button>
        <button type="button" className="secondary" onClick={checkStatus} disabled={busy}>실행 상태 조회</button></div>
    </div>}
    {outcome && <div role="status" className="card"><p>실행 {outcome.execution_id}: {outcome.state}</p>
      {outcome.response_text && <p>{outcome.response_text}</p>}
      {outcome.error_code && <p>오류 코드: {outcome.error_code}</p>}
      {outcome.actual_cost_usd && <p>정산된 추정 비용: ${outcome.actual_cost_usd} USD</p>}
      {outcome.settlement_status === "REVIEW_REQUIRED" && <p role="alert">공급자 보고 비용이 예약액을 초과해 후속 호출이 중단되었습니다. 비용 대사가 필요합니다.</p>}
    </div>}
    {error && <p role="alert" className="error">{error}</p>}
  </section>;
}
