import { useEffect, useState } from "react";
import { api } from "./cookingApi";

type Live = { state: "NOT_STARTED" | "COLLECTING" | "COMPLETED"; count: number; updated_at?: string;
  latest?: { phase: string; phase_second: number; occurred_at: string; chamber_celsius: string;
    door_state: string; fan_level: number };
  latest_window?: { phase: string; first_phase_second: number; last_phase_second: number;
    current_celsius: string; minimum_celsius: string; maximum_celsius: string;
    average_celsius: string; outside_target_seconds: number } | null;
  rice_monitor?: { last_second: number; complete_from_start: boolean; humidity_source: string;
    low_total: number; high_total: number; failure_codes: string[]; warning_codes: string[] } | null;
  mackerel_monitor?: { last_second: number; complete_from_start: boolean; recovered_at_second: number | null;
    door_open_seconds: number; door_max_open_seconds: number; candidate_door: string | null;
    failure_codes: string[]; warning_codes: string[] } | null };

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
    {live?.latest_window && <p>최근 완전한 5초 구간: {live.latest_window.phase} {live.latest_window.first_phase_second}~{live.latest_window.last_phase_second}초
      {" "}· 현재 {live.latest_window.current_celsius}°C · 최저 {live.latest_window.minimum_celsius}°C
      {" "}· 최고 {live.latest_window.maximum_celsius}°C · 평균 {live.latest_window.average_celsius}°C
      {" "}· 목표 범위 이탈 {live.latest_window.outside_target_seconds}초</p>}
    {live?.rice_monitor && <div>
      <p>밥 온도·습도 수집 중 감지 · {live.rice_monitor.last_second}초까지
        {" "}· 저온 이탈 누적 {live.rice_monitor.low_total}초 · 고온 이탈 누적 {live.rice_monitor.high_total}초
        {" "}· 습도 출처 {live.rice_monitor.humidity_source}</p>
      {!live.rice_monitor.complete_from_start && <p role="alert">처음부터 연속된 관찰이 아니거나 습도 출처가 변경되어 확인이 필요합니다.</p>}
      {live.rice_monitor.failure_codes.length > 0 && <p role="alert">공정 실패 조건 감지: {live.rice_monitor.failure_codes.join(", ")}</p>}
      {live.rice_monitor.warning_codes.length > 0 && <p>경고 조건 감지: {live.rice_monitor.warning_codes.join(", ")}</p>}
    </div>}
    {live?.mackerel_monitor && <div>
      <p>고등어 온도 복원·문 상태 수집 중 감지 · {live.mackerel_monitor.last_second}초까지
        {" "}· 온도 복원 {live.mackerel_monitor.recovered_at_second === null ? "미확인" : `${live.mackerel_monitor.recovered_at_second}초`}
        {" "}· 문 개방 누적 {live.mackerel_monitor.door_open_seconds}초 · 최대 {live.mackerel_monitor.door_max_open_seconds}초</p>
      {!live.mackerel_monitor.complete_from_start && <p role="alert">처음부터 연속된 표본 또는 최초 문 닫힘 확인이 없어 감지할 수 없습니다.</p>}
      {live.mackerel_monitor.candidate_door && <p>문 상태 확인 중</p>}
      {live.mackerel_monitor.failure_codes.length > 0 && <p role="alert">공정 실패 조건 감지: {live.mackerel_monitor.failure_codes.join(", ")}</p>}
      {live.mackerel_monitor.warning_codes.length > 0 && <p>경고 조건 감지: {live.mackerel_monitor.warning_codes.join(", ")}</p>}
    </div>}
    <p>수집 중 감지는 합성 관찰값에 대한 부분 결과이며 최종 공정·안전 판정이 아닙니다.</p>
  </div>;
}
