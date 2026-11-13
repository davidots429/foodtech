import { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { TierReview } from "./TierReview";
import "./style.css";

type Session = { email: string | null; surface: "admin"; csrf: string };

function App() {
  const [session, setSession] = useState<Session | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [challenge, setChallenge] = useState(false);
  const [busy, setBusy] = useState(false);
  const requestActive = useRef(false);

  useEffect(() => {
    fetch("/api/admin/me", { credentials: "same-origin" })
      .then(async (response) => response.ok ? setSession(await response.json()) : null)
      .catch(() => setError("인증 서버에 연결할 수 없습니다."))
      .finally(() => setLoading(false));
  }, []);

  async function logout() {
    if (!session) return;
    const response = await fetch("/api/admin/logout", {
      method: "POST", credentials: "same-origin", headers: { "X-CSRF-Token": session.csrf },
    });
    if (!response.ok) { setError("로그아웃에 실패했습니다."); return; }
    const result: { logoutUrl: string } = await response.json();
    window.location.assign(result.logoutUrl);
  }

  async function authenticate(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (requestActive.current) return;
    requestActive.current = true;
    setBusy(true); setError("");
    const next = challenge;
    try {
      const response = await fetch(next ? "/api/auth/admin/totp" : "/api/auth/admin/password", {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(next ? { code } : { username, password }),
      });
      setPassword(""); setCode("");
      if (!response.ok) {
        setError(next ? "인증 코드를 확인하지 못했습니다. 다시 로그인해 주세요." : "로그인 정보를 확인하지 못했습니다.");
        if (next) setChallenge(false);
        return;
      }
      if (next) {
        const me = await fetch("/api/admin/me", { credentials: "same-origin" });
        if (!me.ok) throw new Error("session");
        setSession(await me.json()); setChallenge(false);
      } else setChallenge(true);
    } catch { setError("인증 서버에 연결할 수 없습니다."); }
    finally { requestActive.current = false; setBusy(false); }
  }

  return <main className="shell"><header><p className="eyebrow">Foodtech · 프로젝트 관리자</p>
    <h1>관리자 작업 공간</h1><p>고객 서비스와 분리된 인증·권한 경계입니다.</p></header>
    {error && <p role="alert" className="error">{error}</p>}
    {loading ? <p>로그인 상태를 확인하는 중입니다.</p> : session ? <section>
      <h2>관리자 인증됨</h2><p>{session.email ?? "관리자"}</p>
      <button onClick={logout}>로그아웃</button><TierReview csrf={session.csrf} />
    </section> : <section><h2>관리자 로그인</h2><p>관리자 권한은 공개 가입으로 부여되지 않습니다.</p>
      <form onSubmit={authenticate}>
        {challenge ? <><label htmlFor="totp">인증 앱의 6자리 코드</label><input id="totp" value={code} onChange={e => setCode(e.target.value)} inputMode="numeric" autoComplete="one-time-code" maxLength={6} required /></>
          : <><label htmlFor="username">이메일</label><input id="username" type="email" value={username} onChange={e => setUsername(e.target.value)} autoComplete="username" required />
            <label htmlFor="password">비밀번호</label><input id="password" type="password" value={password} onChange={e => setPassword(e.target.value)} autoComplete="current-password" required /></>}
        <button disabled={busy}>{busy ? "확인 중" : challenge ? "추가 인증" : "로그인"}</button>
      </form></section>}
  </main>;
}

createRoot(document.getElementById("root")!).render(<App />);

