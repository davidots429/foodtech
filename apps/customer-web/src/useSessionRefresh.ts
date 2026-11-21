import { useEffect } from "react";

type RefreshSession = { csrf: string; refresh_available: boolean; expires_in: number };

/** Refresh only when configured; the server retains the refresh token. */
export function useSessionRefresh(surface: "customer" | "admin", session: RefreshSession | null,
                                  report: (message: string) => void) {
  useEffect(() => {
    if (!session?.refresh_available) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let conflicts = 0;
    const schedule = (seconds: number) => {
      timer = setTimeout(() => { void refresh(); }, Math.max(10, seconds - 300) * 1000);
    };
    const refresh = async () => {
      try {
        const response = await fetch(`/api/${surface}/session/refresh`, {
          method: "POST", credentials: "same-origin", signal: controller.signal,
          headers: { "X-CSRF-Token": session.csrf },
        });
        if (response.status === 409) {
          if (++conflicts >= 6) { report("로그인 상태 갱신이 지연되었습니다. 다시 로그인해 주세요."); return; }
          timer = setTimeout(() => { void refresh(); }, 10_000); return;
        }
        if (response.status === 401 || response.status === 403) {
          report("로그인 상태를 갱신하지 못했습니다. 다시 로그인해 주세요."); return;
        }
        if (!response.ok) throw new Error("refresh unavailable");
        const value: { expires_in: number } = await response.json();
        if (!Number.isFinite(value.expires_in) || value.expires_in <= 0) throw new Error("invalid expiry");
        conflicts = 0;
        schedule(value.expires_in);
      } catch {
        if (!controller.signal.aborted) {
          report("로그인 상태 갱신을 다시 시도합니다.");
          timer = setTimeout(() => { void refresh(); }, 60_000);
        }
      }
    };
    schedule(session.expires_in);
    return () => { controller.abort(); if (timer !== undefined) clearTimeout(timer); };
  }, [surface, session?.csrf, session?.refresh_available, report]);
}
