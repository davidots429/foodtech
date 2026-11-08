import { useEffect, useState } from "react";

type Application = { subject: string; application_id: string; business_name: string;
  business_number_last4: string; submitted_at: string };
const notice = "실제 과금이 발생하지 않으며 사업자 진위 조회를 수행하지 않는 비영리 시연 기능입니다.";

export function TierReview({ csrf }: { csrf: string }) {
  const [items, setItems] = useState<Application[]>([]);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);

  async function refresh() {
    try {
      const response = await fetch("/api/admin/tier-requests/pending", { credentials: "same-origin" });
      if (!response.ok) throw new Error();
      const result: { applications: Application[] } = await response.json();
      setItems(result.applications);
    } catch { setMessage("검토 대기 목록을 읽지 못했습니다."); }
  }
  useEffect(() => { void refresh(); }, [csrf]);

  async function decide(item: Application, decision: "APPROVED_FOR_DEMO" | "REJECTED") {
    if (busy) return;
    setBusy(true); setMessage("");
    try {
      const response = await fetch(`/api/admin/tier-requests/${encodeURIComponent(item.subject)}/${encodeURIComponent(item.application_id)}/decision`, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
        body: JSON.stringify({ decision }),
      });
      if (!response.ok) throw new Error();
      setMessage(decision === "APPROVED_FOR_DEMO" ? "시연용 전환을 승인했습니다." : "신청을 거절했습니다.");
      await refresh();
    } catch { setMessage("상태가 변경되었거나 처리하지 못했습니다. 목록을 다시 확인해 주세요."); await refresh(); }
    finally { setBusy(false); }
  }

  return <section><h2>시연용 유료 티어 신청 검토</h2><p className="price">00원</p><p>{notice}</p>
    <p>승인은 시연용 티어 상태만 변경하며, 사업자 확인이나 결제를 의미하지 않습니다.</p>
    <button type="button" onClick={() => void refresh()}>대기 목록 새로고침</button>
    {items.length === 0 ? <p>검토 대기 신청이 없습니다.</p> : items.map(item =>
      <article className="card" key={item.application_id}><h3>{item.business_name}</h3>
        <p>사업자등록번호 끝 {item.business_number_last4}자리 · 신청 {item.submitted_at}</p>
        <button type="button" disabled={busy} onClick={() => void decide(item, "APPROVED_FOR_DEMO")}>시연용 승인 · 00원</button>
        <button type="button" disabled={busy} onClick={() => void decide(item, "REJECTED")}>거절</button>
      </article>)}
    {message && <p role="status">{message}</p>}
  </section>;
}
