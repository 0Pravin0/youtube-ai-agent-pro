"use client";
/** In-app updates (official Tauri updater; artifacts are signed and the signature is verified against the
 * public key built into this app before anything is installed).
 * Flow: check at start-up (non-blocking) and every 6 hours → "Update Available" → UPDATE NOW:
 *   1 engine checkpoints running jobs + backs up the database  2 download  3 signature verified by the plugin
 *   4 install over the existing app (no uninstall)  5 engine stopped cleanly  6 relaunch → migrations →
 *   jobs resume. User data stays in Application Support and the Keychain throughout. */
import { createContext, ReactNode, useCallback, useContext, useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { isDesktop } from "@/lib/engine";
import { Button } from "./ui";

type Avail = { version: string; current: string; notes: string; date?: string; size?: number | null; handle: any };
type Ctx = { available: Avail | null; checking: boolean; lastChecked: number | null; error: string | null;
  check: (manual?: boolean) => Promise<void>; open: () => void };
const UpdCtx = createContext<Ctx>({ available: null, checking: false, lastChecked: null, error: null, check: async () => {}, open: () => {} });
export const useUpdates = () => useContext(UpdCtx);
const SIX_HOURS = 6 * 3600 * 1000;
const fmt = (b?: number | null) => (b ? `${(b / 1e6).toFixed(0)} MB` : "unknown");

export function UpdateProvider({ children, enabled }: { children: ReactNode; enabled: boolean }) {
  const [available, setAvailable] = useState<Avail | null>(null);
  const [checking, setChecking] = useState(false);
  const [lastChecked, setLastChecked] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [show, setShow] = useState(false);

  const check = useCallback(async (manual = false) => {
    if (!isDesktop()) { if (manual) setError("Updates are checked by the installed Mac app."); return; }
    setChecking(true); setError(null);
    try {
      const { check: tauriCheck } = await import("@tauri-apps/plugin-updater");
      const u = await tauriCheck({ timeout: 30000 });
      setLastChecked(Date.now());
      if (!u) { setAvailable(null); return; }
      const url = (u.rawJson as any)?.platforms?.["darwin-aarch64"]?.url;
      let size: number | null = null;
      if (url) { try { size = (await api(`/update/size?url=${encodeURIComponent(url)}`)).bytes; } catch {} }
      const a = { version: u.version, current: u.currentVersion, notes: u.body || "", date: u.date, size, handle: u };
      setAvailable(a);
      let snoozed = 0;
      try { snoozed = Number(localStorage.getItem(`yta-snooze-${u.version}`) || 0); } catch {}
      if (manual || Date.now() > snoozed) setShow(true);
    } catch (e: any) {
      setError(`Couldn't check for updates: ${String(e?.message || e)}`);
    } finally { setChecking(false); }
  }, []);

  useEffect(() => {
    if (!enabled) return;
    const first = setTimeout(() => check(false), 8000); // never block the start-up
    const t = setInterval(() => check(false), SIX_HOURS);
    return () => { clearTimeout(first); clearInterval(t); };
  }, [enabled, check]);

  return (
    <UpdCtx.Provider value={{ available, checking, lastChecked, error, check, open: () => setShow(true) }}>
      {children}
      {show && available && <UpdateDialog a={available} onClose={(snooze) => {
        if (snooze) { try { localStorage.setItem(`yta-snooze-${available.version}`, String(Date.now() + 24 * 3600 * 1000)); } catch {} }
        setShow(false);
      }} />}
    </UpdCtx.Provider>
  );
}

function UpdateDialog({ a, onClose }: { a: Avail; onClose: (snooze: boolean) => void }) {
  const [phase, setPhase] = useState<string | null>(null);
  const [pct, setPct] = useState<number | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const busy = useRef(false);

  const updateNow = async () => {
    if (busy.current) return;
    busy.current = true; setErr(null);
    try {
      setPhase("Saving your work: running jobs are finishing their current step and the database is being backed up…");
      await api("/update/prepare", { method: "POST", body: { to_version: a.version, wait_seconds: 180 } });
      setPhase("Downloading the signed update…");
      let total = 0, got = 0;
      await a.handle.downloadAndInstall((ev: any) => {
        if (ev.event === "Started") total = ev.data.contentLength || 0;
        if (ev.event === "Progress") { got += ev.data.chunkLength; if (total) setPct(Math.round((100 * got) / total)); }
        if (ev.event === "Finished") setPhase("Signature verified. Installing over the current version…");
      });
      setPhase("Restarting into the new version… your channel, settings and videos are kept.");
      const { invoke } = await import("@tauri-apps/api/core");
      await invoke("prepare_restart");
      const { relaunch } = await import("@tauri-apps/plugin-process");
      await relaunch();
    } catch (e: any) {
      const msg = String(e?.message || e);
      setErr(`The update was not installed: ${msg}. Nothing changed — you're still on version ${a.current}.`);
      setPhase(null); busy.current = false;
      try { await api("/update/failed", { method: "POST", body: { to_version: a.version, error: msg.slice(0, 1900) } }); } catch {}
    }
  };

  return (
    <div role="dialog" aria-modal="true" aria-labelledby="upd-title" className="fixed inset-0 z-[60] grid place-items-center bg-black/40 p-6">
      <div className="w-full max-w-[560px] rounded-2xl bg-[var(--panel)] p-6 shadow-2xl">
        <h2 id="upd-title" className="text-[21px] font-semibold">Update Available</h2>
        <dl className="mt-3 grid grid-cols-2 gap-y-1 text-[15px]">
          <dt className="text-[var(--muted)]">Current version</dt><dd>{a.current}</dd>
          <dt className="text-[var(--muted)]">New version</dt><dd className="font-semibold">{a.version}</dd>
          <dt className="text-[var(--muted)]">Download size</dt><dd>{fmt(a.size)}</dd>
        </dl>
        <div className="mt-4 max-h-[200px] overflow-auto whitespace-pre-wrap rounded-lg bg-[var(--panel-2)] p-3 text-[14px]">{a.notes || "No release notes."}</div>
        {phase && <div className="mt-4 text-[14px]"><p>{phase}</p>{pct != null && <div className="mt-2 h-2 overflow-hidden rounded bg-[var(--panel-2)]"><div className="h-full bg-[var(--marigold)]" style={{ width: `${pct}%` }} /></div>}</div>}
        {err && <p role="alert" className="mt-4 text-[14px] text-[var(--kumkum)]">{err}</p>}
        <div className="mt-5 flex justify-end gap-2">
          <Button kind="ghost" disabled={!!phase} onClick={() => onClose(true)}>REMIND ME LATER</Button>
          <Button kind="primary" disabled={!!phase} onClick={updateNow}>UPDATE NOW</Button>
        </div>
        <p className="mt-3 text-[12px] text-[var(--muted)]">The update replaces only the app. Your channel connection, keys (Keychain), videos and history stay in place.</p>
      </div>
    </div>
  );
}
