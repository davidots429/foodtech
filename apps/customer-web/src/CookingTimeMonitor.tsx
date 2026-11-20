import { useEffect, useState } from "react";
import { api } from "./cookingApi";

type Monitor = { status: string; elapsed_seconds: number | null;
  recommended_limit_seconds: number | null; maximum_seconds: number | null };

export function CookingTimeMonitor({ jobId }: { jobId: string }) {
  const [value, setValue] = useState<Monitor | null>(null);
  const [error, setError] = useState(false);
  useEffect(() => {
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    setValue(null); setError(false);
    async function poll() {
      try {
        const next = await api<Monitor>(`/api/customer/cooking-jobs/${encodeURIComponent(jobId)}/time-monitor`);
        if (!stopped) { setValue(next); setError(false); }
      } catch { if (!stopped) setError(true); }
      finally { if (!stopped) timer = setTimeout(poll, 5000); }
    }
    void poll();
    return () => { stopped = true; clearTimeout(timer); };
  }, [jobId]);
  if (error) return <p role="alert">투입 후 경과 시간을 확인할 수 없습니다.</p>;
  if (!value || value.status === "NOT_ACTIVE") return null;
  const labels: Record<string, string> = { MONITORING: "경과 중", DELAY_WARNING: "정상 시간 구간 초과",
    EXTENDED_COOKING: "권장 시간 초과", MAX_TIME_EXCEEDED: "최대 허용 시간 초과",
    NOT_APPLICABLE: "시간 기준 미적용", NOT_MEASURED: "투입 시각 미확인", CLOCK_MISMATCH: "투입 시각 확인 필요" };
  const alert = ["DELAY_WARNING", "EXTENDED_COOKING", "MAX_TIME_EXCEEDED", "CLOCK_MISMATCH"].includes(value.status);
  return <div role={alert ? "alert" : "status"}>
    <p>투입 후 경과 {value.elapsed_seconds === null ? "미확인" : `${value.elapsed_seconds}초`}
      {" "}· {labels[value.status] ?? "확인 필요"}
      {value.recommended_limit_seconds !== null && <> · 권장 상한 {value.recommended_limit_seconds}초</>}
      {value.maximum_seconds !== null && <> · 최대 {value.maximum_seconds}초</>}</p>
    <p className="small">기록된 투입 시각부터 현재까지의 안내입니다. 장비를 제어하지 않으며, 인양 후 실제 시간 판정은 별도로 기록됩니다.</p>
  </div>;
}
