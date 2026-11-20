import { useEffect, useState } from "react";
import { api, jobPath } from "./cookingApi";

type LiveJob = {
  version: number;
  status: string;
  process_assessment_status: string;
  process_assessment?: { second?: number; reason_codes?: string[] } | null;
  auto_safety_status?: string;
  mixture_hold_seconds?: number;
};
type Snapshot = { job: LiveJob };

export function JeyukLiveProcess({ jobId, knownVersion, refreshDisabled, onRefresh }: {
  jobId: string; knownVersion: number; refreshDisabled: boolean; onRefresh: () => void;
}) {
  const [live, setLive] = useState<LiveJob | null>(null);
  const [error, setError] = useState(false);
  useEffect(() => {
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    setLive(null); setError(false);
    async function poll() {
      try {
        const snapshot = await api<Snapshot>(jobPath(jobId));
        if (!stopped) { setLive(snapshot.job); setError(false); }
      } catch { if (!stopped) setError(true); }
      finally { if (!stopped) timer = setTimeout(poll, 5000); }
    }
    void poll();
    return () => { stopped = true; clearTimeout(timer); };
  }, [jobId]);
  if (error) return <p role="alert">현재 공정 상태를 확인할 수 없습니다. 마지막 화면 값은 최신이 아닐 수 있습니다.</p>;
  if (!live) return <p className="small">현재 공정 상태를 조회하고 있습니다.</p>;
  if (live.version < knownVersion) return <p className="small">최신 작업의 공정 상태를 다시 조회하고 있습니다.</p>;
  const labels: Record<string, string> = { INSUFFICIENT_DATA: "표본 대기", NORMAL: "정상", WARNING: "경고", FAILED: "실패" };
  const jobLabels: Record<string, string> = { PENDING: "준비 전", READY: "조리 준비", RUNNING: "조리 중", PAUSED: "일시 정지",
    SAFETY_HOLD: "안전 확인 보류", COMPLETED: "조리 완료", FAILED: "실패", CANCELED: "취소" };
  const safetyLabels: Record<string, string> = { NOT_MEASURED: "미측정", PENDING: "확인 중", PASSED: "통과",
    ASSESSMENT_INCOMPLETE: "확인 미완료" };
  const reasons: Record<string, string> = { LOW_SURFACE_TEMPERATURE: "표면온도 저온 지속",
    HIGH_SURFACE_TEMPERATURE: "표면온도 고온 지속", AGITATOR_FAILURE: "교반기 걸림 또는 정지",
    LOW_RPM_DURATION: "교반속도 저하 지속", HIGH_RPM_DURATION: "교반속도 초과 지속",
    RPM_RECOVERY_TIMEOUT: "교반속도 복원 지연", RATED_LOAD_EXCEEDED: "정격 적재량 초과",
    LOW_SURFACE_TEMPERATURE_WARNING: "표면온도 낮음", HIGH_SURFACE_TEMPERATURE_WARNING: "표면온도 높음",
    LOW_RPM_WARNING: "교반속도 낮음", HIGH_RPM_WARNING: "교반속도 높음", HIGH_MOTOR_LOAD_WARNING: "모터 부하 높음",
    AGITATOR_JAM_WARNING: "교반기 걸림 의심", OVER_RECOMMENDED_LOAD: "권장 적재량 초과",
    UNCLASSIFIED_RPM_WHILE_STOPPED: "정지 중 교반속도 확인 필요" };
  const urgent = live.process_assessment_status === "FAILED" || ["FAILED", "SAFETY_HOLD"].includes(live.status);
  return <div role={urgent ? "alert" : "status"}>
    <p>현재 공정 {labels[live.process_assessment_status] ?? "확인 필요"} · 작업 상태 {jobLabels[live.status] ?? "확인 필요"}
      {live.process_assessment?.second != null && <> · 마지막 처리 표본 {live.process_assessment.second}초</>}
      {live.auto_safety_status && <> · 혼합물 안전 {safetyLabels[live.auto_safety_status] ?? "확인 필요"} ({live.mixture_hold_seconds ?? 0}초)</>}</p>
    {!!live.process_assessment?.reason_codes?.length && <p>판정 사유: {live.process_assessment.reason_codes.map(
      code => reasons[code] ?? "추가 확인 필요").join(", ")}</p>}
    {live.version > knownVersion && <p>새 작업 기록이 있습니다. <button type="button" disabled={refreshDisabled} onClick={onRefresh}>최신 작업 정보 반영</button></p>}
    <p className="small">5초마다 저장된 작업 상태를 조회합니다. 장비를 제어하지 않으며, 표시 지연은 별도로 발생할 수 있습니다.</p>
  </div>;
}
