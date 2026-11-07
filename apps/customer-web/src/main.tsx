import { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { CookingWorkspace } from "./CookingWorkspace";
import { CredentialEnrollment } from "./CredentialEnrollment";
import { ExternalAiTrial } from "./ExternalAiTrial";
import "./style.css";

type Session = { email: string | null; surface: "customer"; csrf: string; tier: "FREE" };
type Example = { source: "SYNTHETIC"; readOnly: true; title: string; message: string };
const notice = "실제 과금이 발생하지 않으며 사업자 진위 조회를 수행하지 않는 비영리 시연 기능입니다.";

function App() {
  const [session, setSession] = useState<Session | null>(null);
  const [example, setExample] = useState<Example | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [totpSecret, setTotpSecret] = useState("");
  const [totpCode, setTotpCode] = useState("");
  const [totpStatus, setTotpStatus] = useState("");
  const [totpBusy, setTotpBusy] = useState(false);
  const [totpCompleted, setTotpCompleted] = useState(false);
  const totpRequestActive = useRef(false);

  useEffect(() => {
    fetch("/api/customer/me", { credentials: "same-origin" })
      .then(async (response) => response.ok ? setSession(await response.json()) : null)
      .catch(() => setError("인증 서버에 연결할 수 없습니다."))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    if (!session) return;
    fetch("/api/customer/example", { credentials: "same-origin" })
      .then(async (response) => response.ok ? setExample(await response.json()) : setError("예시 자료를 읽지 못했습니다."))
      .catch(() => setError("예시 자료를 읽지 못했습니다."));
  }, [session]);

  async function logout() {
    if (!session) return;
    const response = await fetch("/api/customer/logout", {
      method: "POST", credentials: "same-origin", headers: { "X-CSRF-Token": session.csrf },
    });
    if (!response.ok) { setError("로그아웃에 실패했습니다."); return; }
    const result: { logoutUrl: string } = await response.json();
    window.location.assign(result.logoutUrl);
  }

  async function startTotp() {
    if (!session || totpRequestActive.current || totpCompleted) return;
    totpRequestActive.current = true;
    setTotpBusy(true);
    setTotpStatus("");
    try {
      const response = await fetch("/api/customer/mfa/totp/start", {
        method: "POST", credentials: "same-origin", headers: { "X-CSRF-Token": session.csrf },
      });
      if (!response.ok) { setTotpStatus("인증 앱 등록을 시작하지 못했습니다. 다시 로그인해 보세요."); return; }
      const result: { secret: string } = await response.json();
      setTotpSecret(result.secret);
    } catch {
      setTotpStatus("인증 서버에 연결할 수 없습니다.");
    } finally {
      totpRequestActive.current = false;
      setTotpBusy(false);
    }
  }

  async function verifyTotp() {
    if (!session || !/^\d{6}$/.test(totpCode) || totpRequestActive.current || totpCompleted) return;
    totpRequestActive.current = true;
    setTotpBusy(true);
    try {
      const response = await fetch("/api/customer/mfa/totp/verify", {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": session.csrf },
        body: JSON.stringify({ code: totpCode }),
      });
      if (!response.ok) { setTotpStatus("코드를 확인하지 못했습니다. 인증 앱의 새 코드를 입력해 주세요."); return; }
      setTotpSecret("");
      setTotpCode("");
      setTotpCompleted(true);
      setTotpStatus("인증 앱 등록이 완료되었습니다. 다음 로그인부터 TOTP 인증을 사용합니다.");
    } catch {
      setTotpStatus("인증 서버에 연결할 수 없습니다.");
    } finally {
      totpRequestActive.current = false;
      setTotpBusy(false);
    }
  }

  return <main className="shell">
    <header><p className="eyebrow">Foodtech · 고객</p><h1>스마트키친 의사결정 지원</h1>
      <p>조리 데이터와 AI 권고를 운영자가 검토하는 서비스입니다.</p></header>
    {error && <p role="alert" className="error">{error}</p>}
    {loading ? <p>로그인 상태를 확인하는 중입니다.</p> : session ? <>
      <section className="account-summary"><div><h2>무료 체험</h2><p>{session.email ?? "고객"}님 · 무료 티어</p></div>
        <button onClick={logout}>로그아웃</button>
        {example && <div className="card"><h3>{example.title}</h3><p>{example.message}</p><small>합성 예시 · 읽기 전용</small></div>}
        <p className="small">챗봇과 AI 권고는 후속 구현에서 연결됩니다.</p></section>
      <CookingWorkspace csrf={session.csrf} />
      <CredentialEnrollment csrf={session.csrf} />
      <ExternalAiTrial csrf={session.csrf} />
      <section><h2>시연용 유료 티어</h2><p className="price">00원</p><p>{notice}</p>
        <p>전환 신청 기능은 아직 구현되지 않았습니다.</p></section>
      <section><h2>계정 보안</h2><p>관리자 추가 인증에 사용할 인증 앱을 등록할 수 있습니다.</p>
        {totpCompleted ? null : !totpSecret ? <button type="button" onClick={startTotp} disabled={totpBusy}>인증 앱 등록 시작</button> : <>
          <p>인증 앱에서 수동 설정을 선택하고 아래 등록 키를 입력하세요. 키와 인증 코드를 다른 사람에게 공유하지 마세요.</p>
          <code className="totp-secret">{totpSecret}</code>
          <label className="totp-label">인증 앱의 6자리 코드
            <input value={totpCode} onChange={(event) => setTotpCode(event.target.value.replace(/\D/g, "").slice(0, 6))}
              inputMode="numeric" autoComplete="one-time-code" maxLength={6} />
          </label>
          <button type="button" onClick={verifyTotp} disabled={totpCode.length !== 6 || totpBusy}>등록 확인</button>
        </>}
        {totpStatus && <p role="status">{totpStatus}</p>}
      </section>
    </> : <section><h2>시작하기</h2><p>가입할 때 사업자등록번호나 AI API 키가 필요하지 않습니다.</p>
      <div className="actions"><a className="button" href="/api/auth/customer/signup">무료 가입</a>
        <a className="button secondary" href="/api/auth/customer/login">로그인</a></div></section>}
  </main>;
}

createRoot(document.getElementById("root")!).render(<App />);

