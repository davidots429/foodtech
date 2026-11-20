import { useState } from "react";
import { api } from "./cookingApi";

type Window = { first_phase_second: number; last_phase_second: number; sample_count: number;
  complete_window: boolean; current_celsius: string; minimum_celsius: string; maximum_celsius: string;
  average_celsius: string; outside_target_seconds: number };
type Page = { phase: string; raw_sha256: string; windows: Window[]; next_cursor: number | null };

export function CombiWindows({ mealId }: { mealId: string }) {
  const [phase, setPhase] = useState("RICE");
  const [page, setPage] = useState<Page | null>(null);
  const [cursor, setCursor] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function load(offset: number) {
    if (busy) return;
    setBusy(true); setError("");
    try {
      const result = await api<Page>(`/api/customer/combi-meals/${encodeURIComponent(mealId)}/source-windows?phase=${encodeURIComponent(phase)}&cursor=${offset}`);
      setPage(result); setCursor(offset);
    } catch (cause) { setError(cause instanceof Error ? cause.message : "합성 원표본 요약을 읽지 못했습니다."); }
    finally { setBusy(false); }
  }
  return <section><h3>합성 원표본의 5초 요약</h3>
    <p>이미 연결된 1초 원표본으로 다시 계산한 요약입니다. 실시간 송신이나 안전 판정을 뜻하지 않습니다.</p>
    <label>조리 구간<select value={phase} disabled={busy} onChange={event => { setPhase(event.target.value); setPage(null); setCursor(0); setError(""); }}>
      <option value="RICE">밥</option><option value="TRANSITION">건열 전환</option><option value="MACKEREL">고등어</option>
    </select></label>
    <button type="button" disabled={busy} onClick={() => load(0)}>요약 조회</button>
    {error && <p role="alert">{error}</p>}
    {page && <div><p>합성 원본 SHA-256: <code>{page.raw_sha256}</code></p>
      <div style={{ overflowX: "auto" }}><table><thead><tr><th>구간(초)</th><th>표본</th><th>현재 °C</th><th>최소 °C</th><th>최대 °C</th><th>평균 °C</th><th>목표 밖(초)</th></tr></thead>
        <tbody>{page.windows.map(window => <tr key={window.first_phase_second}>
          <td>{window.first_phase_second}~{window.last_phase_second}</td><td>{window.sample_count}{window.complete_window ? "" : " (불완전)"}</td>
          <td>{window.current_celsius}</td><td>{window.minimum_celsius}</td><td>{window.maximum_celsius}</td>
          <td>{window.average_celsius}</td><td>{window.outside_target_seconds}</td></tr>)}</tbody></table></div>
      <button type="button" disabled={busy || cursor === 0} onClick={() => load(Math.max(0, cursor - 25))}>이전 25구간</button>{" "}
      <button type="button" disabled={busy || page.next_cursor === null} onClick={() => page.next_cursor !== null && load(page.next_cursor)}>다음 25구간</button>
    </div>}
  </section>;
}
