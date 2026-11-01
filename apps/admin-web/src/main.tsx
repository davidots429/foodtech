import { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import "./style.css";

type Session = { email: string | null; surface: "admin"; csrf: string };

function App() {
  const [session, setSession] = useState<Session | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

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

  return <main className="shell"><header><p className="eyebrow">Foodtech · 프로젝트 관리자</p>
    <h1>관리자 작업 공간</h1><p>고객 서비스와 분리된 인증·권한 경계입니다.</p></header>
    {error && <p role="alert" className="error">{error}</p>}
    {loading ? <p>로그인 상태를 확인하는 중입니다.</p> : session ? <section>
      <h2>관리자 인증됨</h2><p>{session.email ?? "관리자"}</p>
      <p>관리 기능은 후속 구현에서 연결됩니다.</p><button onClick={logout}>로그아웃</button>
    </section> : <section><h2>관리자 로그인</h2><p>관리자 계정은 공개 가입으로 생성할 수 없습니다.</p>
      <a className="button" href="/api/auth/admin/login">로그인</a></section>}
  </main>;
}

createRoot(document.getElementById("root")!).render(<App />);

