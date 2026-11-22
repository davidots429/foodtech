import { useEffect, useRef, useState } from "react";

type Pending = { status: "PENDING"; cancel_before: number; csrf?: string };

export function AccountWithdrawal({ csrf, onRequested, onAccountFlow }: {
  csrf?: string; onRequested: () => void; onAccountFlow: (active: boolean) => void;
}) {
  const [enabled, setEnabled] = useState(false);
  const [pending, setPending] = useState<Pending | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [canceled, setCanceled] = useState(false);
  const active = useRef(false);
  const requestId = useRef(crypto.randomUUID());

  useEffect(() => { onAccountFlow(pending !== null || canceled); }, [pending, canceled, onAccountFlow]);

  useEffect(() => {
    let stopped = false;
    fetch("/api/customer/withdrawal/options", { credentials: "same-origin", cache: "no-store" })
      .then(async response => {
        if (!response.ok) return;
        const options: { enabled: boolean } = await response.json();
        if (stopped) return;
        setEnabled(options.enabled);
        if (options.enabled && !csrf) {
          const recovery = await fetch("/api/customer/withdrawal/status", { credentials: "same-origin", cache: "no-store" });
          if (recovery.ok && !stopped) setPending(await recovery.json() as Pending);
        }
      })
      .catch(() => { if (!stopped) setMessage("탈퇴 상태를 확인하지 못했습니다."); });
    return () => { stopped = true; };
  }, [csrf]);

  async function requestWithdrawal() {
    if (!csrf || !confirmed || active.current) return;
    active.current = true;
    setBusy(true);
    setMessage("");
    try {
      const response = await fetch("/api/customer/withdrawal/request", {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
        body: JSON.stringify({ request_id: requestId.current, confirmed: true }),
      });
      if (!response.ok) throw new Error();
      setPending(await response.json() as Pending);
      onRequested();
    } catch {
      setMessage("탈퇴 요청 결과를 확인하지 못했습니다. 다시 로그인하거나 탈퇴 취소용 본인 인증으로 상태를 확인해 주세요.");
    } finally {
      active.current = false;
      setBusy(false);
    }
  }

  async function cancelWithdrawal() {
    if (!pending?.csrf || active.current) return;
    active.current = true;
    setBusy(true);
    setMessage("");
    try {
      const response = await fetch("/api/customer/withdrawal/cancel", {
        method: "POST", credentials: "same-origin", headers: { "X-CSRF-Token": pending.csrf },
      });
      if (!response.ok) throw new Error();
      setPending(null);
      setCanceled(true);
    } catch {
      setMessage("탈퇴를 취소하지 못했습니다. 취소 기한이나 계정 상태가 변경되었을 수 있습니다. 본인 인증부터 다시 진행해 주세요.");
    } finally {
      active.current = false;
      setBusy(false);
    }
  }

  if (!enabled) return null;
  return <section aria-label="계정 탈퇴">
    <h2>계정 탈퇴</h2>
    {canceled ? <>
      <p role="status">탈퇴가 취소되었습니다. 기존 계정을 이용하려면 다시 로그인해 주세요.</p>
      <a href="/api/auth/customer/login">로그인</a>
    </> : pending ? <>
      <p>탈퇴 대기 중이며 일반 서비스와 AI 호출을 이용할 수 없습니다.</p>
      <p>취소 기한: <time dateTime={new Date(pending.cancel_before * 1000).toISOString()}>
        {new Date(pending.cancel_before * 1000).toLocaleString("ko-KR", { timeZoneName: "short" })}</time> 직전까지</p>
      <p>기한이 지나면 취소할 수 없으며 기존 계정의 영구 삭제를 진행합니다. 같은 이메일로 가입해도 과거 데이터가 연결되지 않는 새 계정이 됩니다.</p>
      {pending.csrf ? <button type="button" disabled={busy} onClick={cancelWithdrawal}>탈퇴 취소</button> : null}
      <a href="/api/customer/withdrawal/login">탈퇴 취소를 위한 본인 인증</a>
    </> : csrf ? <>
      <p>요청 즉시 서비스와 AI 호출이 중단됩니다. 요청 시각부터 7일 미만에는 본인 인증 후 취소할 수 있으며, 7일이 되는 시각부터 기존 계정의 영구 삭제를 진행합니다.</p>
      <p>삭제 후 백업에 정보가 최대 7일 더 남을 수 있습니다. 이후 같은 이메일로 가입하면 새 계정으로 처리됩니다.</p>
      <label><input type="checkbox" checked={confirmed} disabled={busy}
        onChange={event => setConfirmed(event.target.checked)} /> 탈퇴 대기기간과 삭제·재가입 안내를 확인했습니다.</label>
      <button type="button" disabled={!confirmed || busy} onClick={requestWithdrawal}>탈퇴 요청</button>
    </> : <>
      <p>탈퇴를 요청한 계정은 요청 후 7일 미만에 본인 인증을 거쳐 취소할 수 있습니다.</p>
      <a href="/api/customer/withdrawal/login">탈퇴 취소를 위한 본인 인증</a>
    </>}
    {message && <p role="alert">{message}</p>}
  </section>;
}
