"use client";
import { useState } from "react";
import { ComputerCheck } from "@/components/FreeTools";
import { useShell } from "@/components/Shell";
import { Button, Field, PageTitle, Panel, Pill, Stat, Toggle, inputCls } from "@/components/ui";
import { api, useApi } from "@/lib/api";

const MODES: [string, string, string][] = [
  ["ECO", "ECO", "Coolest and quietest: one video at a time, only on the power adapter, faster/lighter rendering."],
  ["BALANCED", "BALANCED", "Recommended: pauses below 30% battery, when the Mac gets hot, or under heavy memory pressure."],
  ["HIGH_QUALITY", "HIGH QUALITY", "Slowest, best-looking rendering; still pauses at critical memory or very low battery."],
];

function Bar({ pct }: { pct: number }) {
  return <div className="mt-1 h-2 overflow-hidden rounded bg-[var(--panel-2)]"><div className={`h-full ${pct > 90 ? "bg-[var(--kumkum)]" : pct > 70 ? "bg-[var(--marigold)]" : "bg-[var(--teal)]"}`} style={{ width: `${Math.min(100, pct)}%` }} /></div>;
}

export default function Health() {
  const { toast } = useShell();
  const { data: u } = useApi<any>("/system/resources", 5000);
  const { data: s, reload } = useApi<any>("/settings");
  const { data: sys } = useApi<any>("/system", 10000);
  const { data: storage, reload: reloadStorage } = useApi<any>("/storage", 5000);
  const [dir, setDir] = useState("");
  const extra = s?.extra || {};
  const save = async (e: any) => { try { await api("/settings", { method: "PUT", body: { extra: e } }); reload(); toast("Saved"); } catch (x: any) { toast(x.message, true); } };
  const mac = u?.mac;
  return (
    <div className="space-y-6">
      <PageTitle title="System Health" sub="How your Mac is coping, and how hard the agent is allowed to work." />
      {u?.governor_pause && <p role="status" className="rounded-xl border border-[var(--marigold)] bg-[var(--panel)] p-4"><b>Paused for your Mac's sake:</b> {u.governor_pause}. Work continues automatically when this clears.</p>}
      <Panel title="Quality / performance">
        <div className="grid gap-3 sm:grid-cols-3">
          {MODES.map(([k, label, d]) => (
            <button key={k} onClick={() => save({ performance_mode: k })} aria-pressed={(extra.performance_mode || "BALANCED") === k}
              className={`rounded-xl border p-4 text-left ${(extra.performance_mode || "BALANCED") === k ? "border-[var(--marigold)] bg-[var(--panel-2)]" : "border-[var(--line)]"}`}>
              <div className="font-semibold">{label}</div><div className="mt-1 text-[13px] text-[var(--muted)]">{d}</div></button>))}
        </div>
        <div className="mt-4 flex items-center justify-between gap-4">
          <div><div className="font-medium">Keep the Mac awake while a video is being made (power adapter only)</div>
            <div className="text-[13px] text-[var(--muted)]">Stops idle sleep only while work is running. Closing the lid or choosing Sleep still sleeps the Mac; nothing runs while it is asleep or off.</div></div>
          <Toggle label="Keep awake while working" on={extra.keep_awake_when_plugged !== false} onChange={(v) => save({ keep_awake_when_plugged: v })} />
        </div>
      </Panel>
      <Panel title="Right now">
        {!u ? <p className="text-[var(--muted)]">Measuring…</p> : (
          <div className="grid gap-6 sm:grid-cols-2 lg:grid-cols-4">
            <div><Stat label="Processor" value={`${Math.round(u.cpu_pct)}%`} /><Bar pct={u.cpu_pct} /></div>
            <div><Stat label="Memory" value={`${u.ram_used_gb} / ${u.ram_total_gb} GB`} /><Bar pct={u.ram_pct} /></div>
            <Stat label="Memory pressure" value={mac?.memory?.pressure ?? "—"} tone={mac?.memory?.pressure === "critical" ? "bad" : mac?.memory?.pressure === "warning" ? "warn" : undefined} />
            <Stat label="Temperature" value={mac ? (mac.thermal.thermal_level || mac.thermal.performance_level ? `warm (level ${Math.max(mac.thermal.thermal_level, mac.thermal.performance_level)})` : "normal") : "—"} />
            <Stat label="Power" value={mac ? (mac.battery.has_battery ? `${mac.battery.percent}% · ${mac.battery.on_ac ? (mac.battery.charging ? "charging" : "on adapter") : "on battery"}` : "power adapter") : "—"} />
            <Stat label="Videos in progress" value={(sys?.queue?.running || []).length} />
            <Stat label="Paused because" value={Object.values(sys?.queue?.holds || {}).join("; ") || "not paused"} />
          </div>)}
      </Panel>
      <Panel title="This Mac"><ComputerCheck /></Panel>
      <Panel title="Storage">
        {storage && <>
          <p className="text-[14px]">Videos and images: <b className="break-all">{storage.media_dir}</b> · {storage.free_gb} GB free</p>
          <p className="text-[13px] text-[var(--muted)]">Settings, history and the database stay in <span className="break-all">{storage.data_dir}</span>. Nothing is stored inside the app itself, so updates never touch your data.</p>
          <div className="mt-3 flex flex-wrap items-end gap-2">
            <Field label="Move videos and images to another folder (full path)"><input className={`${inputCls} min-w-[360px]`} value={dir} onChange={(e) => setDir(e.target.value)} placeholder="/Volumes/External/YouTube AI Agent Pro" /></Field>
            <Button disabled={!dir.trim() || storage.move?.state === "moving"} onClick={async () => { try { await api("/storage/media-dir", { method: "POST", body: { path: dir.trim() } }); toast("Moving media…"); reloadStorage(); } catch (e: any) { toast(e.message, true); } }}>Move</Button>
          </div>
          {storage.move?.state === "moving" && <p className="mt-2 text-[14px]">Moving files — production is paused until this finishes.</p>}
          {storage.move?.state === "done" && <p className="mt-2 text-[14px]">Moved. Quit and reopen the app to finish.</p>}
          {storage.move?.state === "failed" && <p className="mt-2 text-[14px] text-[var(--kumkum)]">Move failed: {storage.move.error}</p>}
        </>}
      </Panel>
      <Pill>Engine data folder: {sys?.data_dir}</Pill>
    </div>
  );
}
