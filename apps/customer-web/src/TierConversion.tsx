import { useEffect, useState } from "react";

type Application = { application_id: string; business_name: string; business_number_last4: string;
  status: "REQUESTED" | "APPROVED_FOR_DEMO" | "REJECTED"; tier: "FREE" | "DEMO_PAID" };
const notice = "실제 과금이 발생하지 않으며 사업자 진위 조회를 수행하지 않는 비영리 시연 기능입니다.";

export function TierConversion({ csrf, onTierChanged }: { csrf: string; onTierChanged: (tier: "FREE" | "DEMO_PAID") => void }) {
  const [application, setApplication] = useState<Application | null>(null);
  const [name, setName] = useState("");
  const [number, setNumber] = useState("");
  const [accepted, setAccepted] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");

  async function refresh() {
    try {
      const response = await fetch("/api/customer/tier-requests/current", { credentials: "same-origin" });
      if (!response.ok) throw new Error();
      const result: { application: Application | null } = await response.json();
      setApplication(result.application);
      onTierChanged(result.application?.tier ?? "FREE");
    } catch { setMessage("전환 상태를 조회하지 못했습니다."); }
  }

  useEffect(() => { void refresh(); }, [csrf]);

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) return;
    setBusy(true); setMessage("");
    try {
      const response = await fetch("/api/customer/tier-requests", {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
        body: JSON.stringify({ business_name: name, business_registration_number: number, notice_accepted: accepted }),
      });
      if (!response.ok) { setMessage(response.status === 409 ? "이미 처리 중이거나 승인된 신청이 있습니다." : "신청 내용을 확인해 주세요."); return; }
      const result: { application: Application } = await response.json();
      setApplication(result.application); setNumber(""); setAccepted(false);
      setMessage("시연용 전환 신청이 접수되었습니다. 관리자 검토 후 상태를 새로고침해 주세요.");
    } catch { setMessage("신청을 전송하지 못했습니다."); }
    finally { setBusy(false); }
  }

  return <section><h2>시연용 유료 티어 전환</h2><p className="price">00원</p><p>{notice}</p>
    {application && <p>현재 상태: {application.status === "REQUESTED" ? "관리자 검토 대기" : application.status === "APPROVED_FOR_DEMO" ? "시연용 유료 티어 승인" : "거절"} · {application.business_name} · 사업자등록번호 끝 {application.business_number_last4}자리</p>}
    <button type="button" onClick={() => void refresh()}>전환 상태 새로고침</button>
    {(!application || application.status === "REJECTED") && <form onSubmit={submit}>
      <p>시험용 사업자등록번호를 입력해 주세요. 실제 사업자 진위 조회는 하지 않습니다.</p>
      <label>사업자명<input value={name} onChange={event => setName(event.target.value)} minLength={2} maxLength={80} required /></label>
      <label>사업자등록번호<input value={number} onChange={event => setNumber(event.target.value)}
        inputMode="numeric" pattern="[0-9]{3}-?[0-9]{2}-?[0-9]{5}" placeholder="000-00-00000" required /></label>
      <label><input type="checkbox" checked={accepted} onChange={event => setAccepted(event.target.checked)} required /> {notice}</label>
      <button disabled={busy}>{busy ? "신청 중" : "시연용 전환 신청 · 00원"}</button>
    </form>}
    {message && <p role="status">{message}</p>}
  </section>;
}
