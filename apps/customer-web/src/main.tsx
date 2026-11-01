import { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import "./style.css";

type Session = { email: string | null; surface: "customer"; csrf: string; tier: "FREE" };
type Example = { source: "SYNTHETIC"; readOnly: true; title: string; message: string };
const notice = "실제 과금이 발생하지 않으며 사업자 진위 조회를 수행하지 않는 비영리 시연 기능입니다.";

function App() {
  const [session, setSession] = useState<Session | null>(null);
  const [example, setExample] = useState<Example | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

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

  return <main className="shell">
    <header><p className="eyebrow">Foodtech · 고객</p><h1>스마트키친 의사결정 지원</h1>
      <p>조리 데이터와 AI 권고를 운영자가 검토하는 서비스입니다.</p></header>
    {error && <p role="alert" className="error">{error}</p>}
    {loading ? <p>로그인 상태를 확인하는 중입니다.</p> : session ? <>
      <section><h2>무료 체험</h2><p>{session.email ?? "고객"}님 · 무료 티어</p>
        {example && <div className="card"><h3>{example.title}</h3><p>{example.message}</p><small>합성 예시 · 읽기 전용</small></div>}
        <p>챗봇과 실제 업무 기능은 후속 구현에서 연결됩니다.</p>
        <button onClick={logout}>로그아웃</button></section>
      <section><h2>시연용 유료 티어</h2><p className="price">00원</p><p>{notice}</p>
        <p>전환 신청 기능은 아직 구현되지 않았습니다.</p></section>
    </> : <section><h2>시작하기</h2><p>가입할 때 사업자등록번호나 AI API 키가 필요하지 않습니다.</p>
      <div className="actions"><a className="button" href="/api/auth/customer/signup">무료 가입</a>
        <a className="button secondary" href="/api/auth/customer/login">로그인</a></div></section>}
  </main>;
}

createRoot(document.getElementById("root")!).render(<App />);

