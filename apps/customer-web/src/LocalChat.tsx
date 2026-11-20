import { useEffect, useRef, useState } from "react";

type Message = { role: "user" | "assistant"; content: string };
type Outcome = { execution_id: string; state: string; response_text: string | null; error_code: string | null };

export function LocalChat({ csrf }: { csrf: string }) {
  const [messages, setMessages] = useState<Message[]>([]);
  const [draft, setDraft] = useState("");
  const [pending, setPending] = useState("");
  const [error, setError] = useState("");
  const active = useRef(false);
  const end = useRef<HTMLDivElement>(null);

  useEffect(() => { end.current?.scrollIntoView({ behavior: "smooth", block: "nearest" }); }, [messages, pending]);

  async function send(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const question = draft.trim();
    if (!question || question.length > 1000 || active.current) return;
    active.current = true;
    setPending(question);
    setDraft("");
    setError("");
    const context = [...messages.slice(-4), { role: "user" as const, content: question }];
    while (context.length > 1 && JSON.stringify(context).length > 3400) context.splice(0, 2);
    try {
      const response = await fetch("/api/customer/ai/chat", {
        method: "POST", credentials: "same-origin", cache: "no-store",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
        body: JSON.stringify({ messages: context }),
      });
      if (!response.ok) {
        const errorBody = await response.json().catch(() => ({})) as { detail?: unknown };
        const messages: Record<string, string> = {
          AI_USAGE_LIMIT_UNAVAILABLE: "현재 AI 이용 한도에 도달했거나 시연용 한도가 준비되지 않았습니다.",
          AI_MODEL_UNAVAILABLE: "로컬 AI 모델이 준비되지 않았습니다. 잠시 후 다시 시도해 주세요.",
          AI_TEMPORARILY_UNAVAILABLE: "AI 실행이 일시적으로 지연되고 있습니다. 잠시 후 다시 시도해 주세요.",
        };
        throw new Error(typeof errorBody.detail === "string" && Object.hasOwn(messages, errorBody.detail)
          ? messages[errorBody.detail] : response.status === 503
            ? "로컬 AI가 준비되지 않았습니다. 잠시 후 다시 시도해 주세요."
            : "답변을 받지 못했습니다. 질문을 확인하고 다시 시도해 주세요.");
      }
      const outcome = await response.json() as Outcome;
      if (outcome.state !== "SUCCEEDED" || !outcome.response_text) {
        const messages: Record<string, string> = {
          OLLAMA_UNAVAILABLE: "로컬 AI에 연결할 수 없습니다. 잠시 후 다시 시도해 주세요.",
          OLLAMA_TIMEOUT: "답변 생성 시간이 초과되었습니다. 잠시 후 다시 시도해 주세요.",
          OLLAMA_HTTP_ERROR: "로컬 AI가 요청을 처리하지 못했습니다. 잠시 후 다시 시도해 주세요.",
        };
        throw new Error((outcome.error_code && Object.hasOwn(messages, outcome.error_code) && messages[outcome.error_code]) ||
          "답변을 완료하지 못했습니다. 잠시 후 다시 시도해 주세요.");
      }
      setMessages(previous => [...previous, { role: "user", content: question },
        { role: "assistant", content: outcome.response_text! }]);
    } catch (failure) {
      setDraft(question);
      setError(failure instanceof Error ? failure.message : "답변을 받지 못했습니다.");
    } finally {
      setPending("");
      active.current = false;
    }
  }

  return <section className="chat" aria-labelledby="local-chat-title">
    <div className="section-heading"><h2 id="local-chat-title">조리 상담 챗봇</h2>
      <span className="tag">로컬 AI</span></div>
    <p className="small">이 대화는 현재 브라우저 화면에만 유지되며 새로고침하면 사라집니다. 최근 대화만 다음 질문의 맥락으로 보냅니다.</p>
    <p className="small">답변은 운영자 검토용 권고입니다. 장비 설정을 자동 변경하지 않으며 안전 이상은 현장에서 확인해야 합니다.</p>
    <div className="chat-messages" role="log" aria-label="챗봇 대화" aria-live="polite">
      {messages.length === 0 && !pending && <p className="muted">예: 합성 돈까스 작업에서 온도 경고를 어떻게 확인하나요?</p>}
      {messages.map((message, index) => <div className={`chat-message ${message.role}`} key={index}>
        <strong>{message.role === "user" ? "나" : "조리 상담"}</strong>
        <p>{message.content}</p>
      </div>)}
      {pending && <><div className="chat-message user"><strong>나</strong><p>{pending}</p></div>
        <p className="muted" role="status">답변을 생성하고 있습니다…</p></>}
      <div ref={end} />
    </div>
    <form className="chat-form" onSubmit={send}>
      <label htmlFor="chat-question">질문
        <textarea id="chat-question" value={draft} onChange={event => setDraft(event.target.value)}
          maxLength={1000} rows={3} disabled={!!pending} placeholder="조리 데이터나 권고에 대해 질문하세요." />
      </label>
      <div className="actions"><button type="submit" disabled={!!pending || !draft.trim()}>질문 보내기</button>
        <button type="button" className="secondary" disabled={!!pending || messages.length === 0}
          onClick={() => { setMessages([]); setError(""); }}>대화 지우기</button>
        <span className="small">{draft.length}/1000</span></div>
    </form>
    {error && <p role="alert" className="error">{error}</p>}
  </section>;
}
