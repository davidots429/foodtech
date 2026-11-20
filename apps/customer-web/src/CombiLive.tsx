import { useEffect, useRef, useState } from "react";
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
    failure_codes: string[]; warning_codes: string[] } | null;
  transition_monitor?: { last_second: number; complete_from_start: boolean; humidity_source: string;
    vent_status: string; stable_seconds: number; ready_observed_at_second: number | null;
    ready_conditions_current: boolean; timeout: boolean } | null };

type EventPage = { history_from_sequence: number | null; next_cursor: number | null;
  events: { sequence: number; phase: string; phase_second: number; occurred_at: string;
    recorded_at: string; code: string; severity: string }[] };

export function CombiLive({ mealId }: { mealId: string }) {
  const [live, setLive] = useState<Live | null>(null);
  const [error, setError] = useState(false);
  const [events, setEvents] = useState<EventPage | null>(null);
  const [eventError, setEventError] = useState(false);
  const activeMeal = useRef(mealId);
  activeMeal.current = mealId;
  async function loadEvents() {
    const requestedMeal = mealId;
    try {
      const result = await api<EventPage>(`/api/customer/combi-meals/${encodeURIComponent(mealId)}/source-live/events`);
      if (activeMeal.current === requestedMeal) { setEvents(result); setEventError(false); }
    } catch { if (activeMeal.current === requestedMeal) setEventError(true); }
  }
  useEffect(() => {
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    setLive(null); setError(false); setEvents(null); setEventError(false);
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
    {live?.transition_monitor && <div>
      <p>건열 전환 수집 중 감지 · {live.transition_monitor.last_second}초까지
        {" "}· 안정 조건 {live.transition_monitor.stable_seconds}초 · 습도 출처 {live.transition_monitor.humidity_source}
        {" "}· 환기 {live.transition_monitor.vent_status}</p>
      {!live.transition_monitor.complete_from_start && <p role="alert">시작 표본 또는 필요한 센서·알람·환기 정보가 불완전합니다.</p>}
      {live.transition_monitor.ready_observed_at_second !== null && <p>준비 조건 관찰: {live.transition_monitor.ready_observed_at_second}초
        {" "}· 마지막 전환 표본 조건 {live.transition_monitor.ready_conditions_current ? "충족" : "미충족"}</p>}
      {live.transition_monitor.timeout && <p role="alert">건열 전환 300초 내 준비 실패 감지</p>}
      <p>이 관찰만으로 고등어 투입을 허용하지 않습니다. 배치 전환 확인이 별도로 필요합니다.</p>
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
    {live && live.state !== "NOT_STARTED" && <div>
      <button type="button" onClick={() => void loadEvents()}>최근 수집 이력 조회</button>
      {eventError && <p role="alert">수집 이력을 불러오지 못했습니다.</p>}
      {events && <div>
        {events.history_from_sequence !== null && events.history_from_sequence > 1 &&
          <p>이전 표본의 이력은 기록되지 않았습니다. 원표본 {events.history_from_sequence}번부터 기록합니다.</p>}
        {events.events.length === 0 ? <p>기록된 수집 이력이 없습니다.</p> :
          <ul>{events.events.map((event, index) => <li key={`${event.sequence}-${event.code}-${index}`}>
            {event.phase} {event.phase_second}초 · {event.severity} · {event.code} · 논리 시각 {event.occurred_at}
          </li>)}</ul>}
        {events.next_cursor !== null && <p>최근 25건을 표시합니다.</p>}
      </div>}
    </div>}
    <p>수집 중 감지는 합성 관찰값에 대한 부분 결과이며 최종 공정·안전 판정이 아닙니다.</p>
  </div>;
}
