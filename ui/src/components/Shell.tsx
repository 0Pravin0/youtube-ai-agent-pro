"use client";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { ReactNode, createContext, useContext, useEffect, useState } from "react";
import { api, useApi, useLive, usd, type LiveEvent } from "@/lib/api";
import { getEngine, syncTray } from "@/lib/engine";
import { Button, Pill, useToast } from "./ui";
import { UpdateProvider, useUpdates } from "./Updater";

const NAV: [string, string][] = [
  ["/", "Dashboard"], ["/channel/", "My Channel"], ["/agents/", "AI Agent Control"], ["/videos/", "Video Production"],
  ["/stories/", "Story Library"], ["/characters/", "Character Library"], ["/library/", "Video Preview"],
  ["/calendar/", "Publishing Calendar"], ["/analytics/", "YouTube Analytics"], ["/tools/", "Free Tools Manager"],
  ["/mcp/", "MCP Connections"], ["/models/", "Model Manager"], ["/health/", "System Health"], ["/updates/", "Updates"],
  ["/settings/", "Settings"],
];

type Ctx = { overview: any; reload: () => void; lastEvent: LiveEvent | null; live: boolean; toast: (m: string, bad?: boolean) => void };
const ShellCtx = createContext<Ctx>({ overview: null, reload: () => {}, lastEvent: null, live: false, toast: () => {} });
export const useShell = () => useContext(ShellCtx);

export function Shell({ children }: { children: ReactNode }) {
  const path = usePathname() || "/";
  const router = useRouter();
  const [engineReady, setEngineReady] = useState(false);
  const [engineError, setEngineError] = useState<string | null>(null);
  const { data: ov, reload } = useApi<any>(engineReady ? "/overview" : null, 20000);
  const [lastEvent, setLastEvent] = useState<LiveEvent | null>(null);
  const [theme, setTheme] = useState<"dark" | "light">("light");
  const { show, node } = useToast();
  const live = useLive((e) => { setLastEvent(e); if (e.type !== "activity") reload(); }, engineReady);
  const inSetup = path.startsWith("/setup");

  useEffect(() => {
    let alive = true;
    getEngine()
      .then(async (e) => {
        // a database problem (e.g. data from a newer version) is reported by the engine instead of crashing
        try { const h = await (await fetch(`${e.url}/api/health`)).json(); if (h.fatal) { alive && setEngineError(h.fatal); return; } } catch {}
        alive && setEngineReady(true);
      })
      .catch((e) => alive && setEngineError(String(e?.message || e)));
    return () => { alive = false; };
  }, []);
  useEffect(() => { // menu bar "View Jobs"
    let un: any;
    if (typeof window !== "undefined" && (window as any).__TAURI_INTERNALS__) {
      import("@tauri-apps/api/event").then(({ listen }) => listen<string>("navigate", (ev) => router.push(ev.payload)).then((f) => { un = f; }));
    }
    return () => { if (un) un(); };
  }, [router]);
  useEffect(() => {
    let t: "dark" | "light" = "light";
    try { t = (localStorage.getItem("yta-theme") as any) || "light"; } catch {}
    setTheme(t);
  }, []);
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try { localStorage.setItem("yta-theme", theme); } catch {}
  }, [theme]);
  useEffect(() => {
    if (ov && !ov.setup_complete && !inSetup) router.replace("/setup/");
  }, [ov, inSetup, router]);
  useEffect(() => {
    // Tell the desktop shell whether closing the window should keep the app running in the tray.
    if (!engineReady) return;
    api("/settings").then((s) => syncTray(s?.extra?.close_to_tray !== false)).catch(() => {});
  }, [engineReady]);

  if (engineError) return <Starting error={engineError} />;
  if (!engineReady || !ov) return <Starting />;
  if (inSetup) return <ShellCtx.Provider value={{ overview: ov, reload, lastEvent, live, toast: show }}><div className="pt-7" data-tauri-drag-region>{children}</div>{node}</ShellCtx.Provider>;

  const control = async (action: "start" | "pause" | "stop") => {
    try {
      const r = await api(`/agents/${action}`, { method: "POST" });
      show(`Automation ${r.state.toLowerCase()}`);
      reload();
    } catch (e: any) { show(e.message, true); }
  };
  const st = ov.automation.state;
  const agentTone = ov.agent_status === "Running" ? "good" : ov.agent_status === "Error" ? "bad" : "warn";
  const prof = ov.channel.profile || {};

  return (
    <ShellCtx.Provider value={{ overview: ov, reload, lastEvent, live, toast: show }}>
     <UpdateProvider enabled>
      <a href="#main" className="sr-only focus:not-sr-only focus:fixed focus:top-2 focus:left-2 focus:z-50 focus:rounded focus:bg-[var(--panel)] focus:px-3 focus:py-2">Skip to content</a>
      <div className="grid min-h-screen grid-cols-[232px_1fr]">
        <aside className="sticky top-0 flex h-screen flex-col overflow-y-auto border-r border-[var(--line)] bg-[var(--panel)]">
          <div className="flex items-center gap-3 px-5 pt-11 pb-4" data-tauri-drag-region>
            <img src="/icon.png" alt="" className="h-9 w-9 rounded-lg" />
            <div className="text-[16px] leading-tight font-semibold">YouTube AI Agent Pro</div>
          </div>
          <nav aria-label="Sections" className="px-3 pb-4">
            {NAV.map(([href, label]) => {
              const active = href === "/" ? path === "/" : path.startsWith(href);
              return (
                <Link key={href} href={href} aria-current={active ? "page" : undefined}
                  className={`mb-0.5 block rounded-lg px-3 py-2 text-[15px] ${active ? "bg-[var(--panel-2)] font-medium text-[var(--text)] shadow-[inset_3px_0_0_var(--marigold)]" : "text-[var(--muted)] hover:text-[var(--text)]"}`}>
                  {label}
                </Link>
              );
            })}
          </nav>
          <div className="mt-auto border-t border-[var(--line)] px-5 py-4">
            {ov.channel.connected ? (
              <div className="flex items-center gap-2.5">
                {prof.thumbnail ? <img src={prof.thumbnail} alt="" className="h-8 w-8 rounded-full" referrerPolicy="no-referrer" /> : <div className="h-8 w-8 rounded-full bg-[var(--panel-2)]" />}
                <div className="min-w-0">
                  <div className="truncate text-[14px] font-medium">{ov.channel.name}</div>
                  <div className="text-[12px] text-[var(--muted)]">{prof.subscriber_count != null ? `${prof.subscriber_count.toLocaleString("en-IN")} subscribers` : "YouTube connected"}</div>
                </div>
              </div>
            ) : <Link href="/connections/" className="text-[14px] text-[var(--kumkum)] underline">Connect your YouTube channel</Link>}
            <div className="mt-3 text-[12px] text-[var(--muted)]">Runs on this Mac with free local AI while the app is open (also from the menu bar).</div>
          </div>
        </aside>
        <div className="min-w-0">
          <header className="sticky top-0 z-30 border-b border-[var(--line)] bg-[color-mix(in_oklab,var(--bg)_88%,transparent)] backdrop-blur">
            <div className="mx-auto flex max-w-[1240px] flex-wrap items-center gap-x-5 gap-y-3 px-6 py-3">
              <div className="flex flex-wrap items-center gap-2 text-[13px]" aria-label="System status" data-tauri-drag-region>
                <UpdateBadge />
                <Pill tone={agentTone}>AI agent: {ov.agent_status}</Pill>
                <Pill tone={ov.channel.connected ? "good" : "bad"}>YouTube: {ov.channel.connected ? "Connected" : "Disconnected"}</Pill>
                {ov.production?.free_mode ? <Pill tone="good">FREE MODE ACTIVE</Pill> : <Pill tone="warn">{ov.production?.mode} mode</Pill>}
                <Pill tone={ov.automation.mode === "FULL_AUTO" ? "accent" : "neutral"}>{ov.automation.mode === "FULL_AUTO" ? "Full auto" : "Approval mode"}</Pill>
                <Pill>Active jobs {ov.active_jobs}</Pill>
                {!ov.production?.free_mode && <Pill tone={ov.budget.daily_remaining_usd <= 0 ? "bad" : "neutral"}>Paid budget left today {usd(ov.budget.daily_remaining_usd)}</Pill>}
                <span className={`inline-block h-2 w-2 rounded-full ${live ? "bg-[var(--teal)]" : "bg-[var(--muted)]"}`} title={live ? "Live updates on" : "Reconnecting"} />
              </div>
              <div className="ml-auto flex flex-wrap items-center gap-2">
                <Button kind="primary" disabled={st === "RUNNING"} onClick={() => control("start")}>Start automation</Button>
                <Button disabled={st === "PAUSED"} onClick={() => control("pause")}>Pause automation</Button>
                <Button kind="danger" disabled={st === "STOPPED"} onClick={() => control("stop")}>Stop automation</Button>
                <CreateNow disabled={st === "STOPPED"} onDone={(id) => router.push(`/videos/detail/?id=${id}`)} toast={show} />
                <Button kind="ghost" onClick={() => setTheme(theme === "dark" ? "light" : "dark")} title="Switch theme">{theme === "dark" ? "Light" : "Dark"}</Button>
              </div>
            </div>
          </header>
          <main id="main" className="mx-auto max-w-[1240px] px-6 py-8">{children}</main>
        </div>
      </div>
      {node}
     </UpdateProvider>
    </ShellCtx.Provider>
  );
}

function UpdateBadge() {
  const { available, open } = useUpdates();
  if (!available) return null;
  return <button onClick={open} className="rounded-full bg-[var(--marigold)] px-2.5 py-0.5 text-[13px] font-medium text-[var(--marigold-ink)]">Update {available.version} available</button>;
}

function Starting({ error }: { error?: string }) {
  return (
    <main className="grid min-h-screen place-items-center px-6 text-center">
      <div>
        <img src="/icon.png" alt="" className="mx-auto mb-5 h-16 w-16 rounded-2xl" />
        <div className="text-[22px] font-semibold">YouTube AI Agent Pro</div>
        {error ? (
          <div role="alert" className="mt-3 max-w-[60ch] text-left text-[var(--kumkum)]"><p>The app could not start: {error}</p>
          <p className="mt-2 text-[var(--muted)]">Your data has not been changed. Quit from the menu bar (YouTube AI Agent Pro → Quit Application) and open the app again. Details are in ~/Library/Logs/com.youtubeaiagentpro.app.</p>
          <Recovery /></div>
        ) : <p className="mt-2 text-[var(--muted)]">Starting the local engine…</p>}
      </div>
    </main>
  );
}

/** Shown when the engine refused to open the database: lets the owner pick a backup to restore at next start. */
function Recovery() {
  const [list, setList] = useState<any[] | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  useEffect(() => { api("/backups").then((b) => setList(b.backups)).catch(() => setList(null)); }, []);
  if (!list || !list.length) return null;
  return (
    <div className="mt-4 rounded-xl border border-[var(--line)] p-4 text-[var(--text)]">
      <div className="font-medium">Restore a backup</div>
      <ul className="mt-2 space-y-1 text-[14px]">{list.slice(0, 6).map((b) => (
        <li key={b.name} className="flex items-center justify-between gap-3"><span>{b.name}</span>
          <Button kind="ghost" onClick={async () => { try { setMsg((await api(`/backups/${b.name}/restore`, { method: "POST" })).message); } catch (e: any) { setMsg(e.message); } }}>Restore</Button></li>))}</ul>
      {msg && <p className="mt-2 text-[14px]">{msg} Quit and reopen the app.</p>}
    </div>
  );
}

function CreateNow({ onDone, disabled, toast }: { onDone: (id: string) => void; disabled: boolean; toast: (m: string, bad?: boolean) => void }) {
  const [open, setOpen] = useState(false);
  const create = async (format: "SHORT" | "LONG") => {
    setOpen(false);
    try {
      const p = await api("/videos/create", { method: "POST", body: { format } });
      toast(`${format === "SHORT" ? "Short" : "Long video"} started`);
      onDone(p.id);
    } catch (e: any) { toast(e.message, true); }
  };
  return (
    <div className="relative">
      <Button disabled={disabled} onClick={() => setOpen(!open)} title={disabled ? "Start or pause automation first" : undefined}>Create video now</Button>
      {open && (
        <div className="absolute right-0 z-40 mt-2 w-56 rounded-xl border border-[var(--line)] bg-[var(--panel)] p-1.5 shadow-xl" role="menu">
          <button role="menuitem" className="block w-full rounded-lg px-3 py-2 text-left hover:bg-[var(--panel-2)]" onClick={() => create("SHORT")}>
            Short<span className="block text-[13px] text-[var(--muted)]">30–60 s, vertical 9:16</span>
          </button>
          <button role="menuitem" className="block w-full rounded-lg px-3 py-2 text-left hover:bg-[var(--panel-2)]" onClick={() => create("LONG")}>
            Long video<span className="block text-[13px] text-[var(--muted)]">5–15 min, landscape 16:9</span>
          </button>
        </div>
      )}
    </div>
  );
}
