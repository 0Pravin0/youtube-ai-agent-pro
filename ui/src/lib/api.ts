"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { desktopNotify, getEngine } from "./engine";

export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message); }
}

export async function api<T = any>(path: string, opts: { method?: string; body?: unknown; headers?: Record<string, string> } = {}): Promise<T> {
  const eng = await getEngine();
  let res: Response;
  try {
    res = await fetch(`${eng.url}/api${path}`, {
      method: opts.method || "GET",
      headers: { "content-type": "application/json", "x-yta-token": eng.token, ...(opts.headers || {}) },
      body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
    });
  } catch {
    throw new ApiError(0, "The local engine is not responding. If this lasts, restart YouTube AI Agent Pro.");
  }
  if (!res.ok) {
    let msg = res.statusText;
    try {
      const j = await res.json();
      msg = typeof j.detail === "string" ? j.detail : Array.isArray(j.detail) ? j.detail.map((d: any) => `${(d.loc || []).slice(-1)[0]}: ${d.msg}`).join("; ") : JSON.stringify(j.detail);
    } catch {}
    throw new ApiError(res.status, msg);
  }
  return res.json();
}

/** Fetch + refresh helper. `refreshMs` polls; live events also trigger reloads via `useLive`. */
export function useApi<T = any>(path: string | null, refreshMs = 0) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const load = useCallback(async () => {
    if (!path) return;
    try {
      setData(await api<T>(path));
      setError(null);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, [path]);
  useEffect(() => {
    load();
    if (!refreshMs) return;
    const t = setInterval(load, refreshMs);
    return () => clearInterval(t);
  }, [load, refreshMs]);
  return { data, error, loading, reload: load, setData };
}

export type LiveEvent = { type: string; [k: string]: any };

/** Server-Sent Events from /api/activity/stream (activity, project stage changes, notifications). */
export function useLive(onEvent: (e: LiveEvent) => void, enabled = true) {
  const cb = useRef(onEvent);
  cb.current = onEvent;
  const [connected, setConnected] = useState(false);
  useEffect(() => {
    if (!enabled) return;
    let es: EventSource | null = null;
    let retry: any;
    const open = async () => {
      const eng = await getEngine();
      es = new EventSource(`${eng.url}/api/activity/stream?token=${encodeURIComponent(eng.token)}`);
      es.onopen = () => setConnected(true);
      for (const t of ["activity", "project", "notification", "channel"]) {
        es.addEventListener(t, (m: MessageEvent) => {
          try {
            const ev = JSON.parse(m.data);
            if (ev.type === "notification") desktopNotify(ev.title, ev.body || "");
            cb.current(ev);
          } catch {}
        });
      }
      es.onerror = () => {
        setConnected(false);
        es?.close();
        retry = setTimeout(open, 5000);
      };
    };
    open();
    return () => { es?.close(); clearTimeout(retry); };
  }, [enabled]);
  return connected;
}

export const usd = (n: number | null | undefined, digits = 2) =>
  n == null ? "—" : `$${n.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;

export function ist(iso: string | null | undefined, tz = "Asia/Kolkata", withDate = true) {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString("en-IN", {
    timeZone: tz, hour: "2-digit", minute: "2-digit", hour12: false,
    ...(withDate ? { day: "numeric", month: "short" } : {}),
  });
}

export function ago(iso: string | null | undefined) {
  if (!iso) return "—";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return `${Math.max(1, Math.round(s))}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

export const STAGE_LABEL: Record<string, string> = {
  QUEUED: "Queued", RESEARCHING: "Researching", IDEA_SELECTED: "Idea selected", SCRIPT_GENERATION: "Writing script",
  SCRIPT_VALIDATION: "Checking script", STORYBOARD: "Storyboard", CHARACTER_GENERATION: "Characters",
  IMAGE_GENERATION: "Images", VIDEO_GENERATION: "Video clips", VOICE_GENERATION: "Voices", AUDIO_PROCESSING: "Audio mix",
  VIDEO_EDITING: "Editing", RENDERING: "Rendering", QUALITY_VALIDATION: "Quality checks", THUMBNAIL_GENERATION: "Thumbnails",
  SEO_GENERATION: "Metadata", READY_TO_UPLOAD: "Ready to upload", UPLOADING: "Uploading", YOUTUBE_PROCESSING: "YouTube processing",
  SCHEDULED: "Scheduled", PUBLISHED: "Published", ANALYTICS_MONITORING: "Tracking analytics", COMPLETED: "Completed",
  RETRYING: "Retrying", PAUSED: "Paused", FAILED: "Failed", CANCELLED: "Cancelled", BLOCKED_BY_PROVIDER: "Blocked",
};
