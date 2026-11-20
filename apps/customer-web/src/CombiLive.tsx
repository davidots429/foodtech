import { useEffect, useState } from "react";
import { api } from "./cookingApi";

type Live = { state: "NOT_STARTED" | "COLLECTING" | "COMPLETED"; count: number; updated_at?: string;
  latest?: { phase: string; phase_second: number; occurred_at: string; chamber_celsius: string;
    door_state: string; fan_level: number } };

export function CombiLive({ mealId }: { mealId: string }) {
  const [live, setLive] = useState<Live | null>(null);
  const [error, setError] = useState(false);
  useEffect(() => {
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    setLive(null); setError(false);
    async function poll() {
      try {
        const value = await api<Live>(`/api/customer/combi-meals/${encodeURIComponent(mealId)}/source-live`);
        if (!stopped) { setLive(value); setError(false); }
      } catch {
        if (!stopped) setError(true);
      } finally {
        if (!stopped) timer = setTimeout(poll, 5000);
      }
    }
    void poll();
    return () => { stopped = true; clearTimeout(timer); };
  }, [mealId]);
  if (!live && !error) return null;
  return <div aria-live="polite">
    <p>합성 원표본 수집: {error ? "조회할 수 없음" : live?.state === "NOT_STARTED" ? "시작 전" :
      live?.state === "COMPLETED" ? "완료" : "수집 중"}
      {live && live.state !== "NOT_STARTED" && <> · {live.count}건 · 마지막 수신 {live.updated_at ? new Date(live.updated_at).toLocaleString() : "미확인"}</>}</p>
    {live?.latest && <p>최근 표본: {live.latest.phase} {live.latest.phase_second}초 · 챔버 {live.latest.chamber_celsius}°C
      {" "}· 문 {live.latest.door_state} · 팬 {live.latest.fan_level} · 논리 시각 {live.latest.occurred_at}</p>}
    <p>수집 중 표본은 합성 관찰값이며 안전·공정 판정이 아닙니다.</p>
  </div>;
}
