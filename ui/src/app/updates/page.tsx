"use client";
import { useShell } from "@/components/Shell";
import { useUpdates } from "@/components/Updater";
import { Button, PageTitle, Panel, Pill } from "@/components/ui";
import { api, useApi } from "@/lib/api";

const when = (t: number) => new Date(t * 1000).toLocaleString("en-IN");

export default function Updates() {
  const { toast } = useShell();
  const upd = useUpdates();
  const { data: v } = useApi<any>("/system/version");
  const { data: hist } = useApi<any[]>("/update/history");
  const { data: b, reload } = useApi<any>("/backups");
  return (
    <div className="space-y-6">
      <PageTitle title="Updates" sub="New versions install over the current app — no uninstalling — and keep your channel, keys, videos and history." />
      <Panel title="Version">
        <div className="flex flex-wrap items-center gap-3">
          <span className="text-[18px] font-semibold">YouTube AI Agent Pro {v?.app_version}</span>
          <Pill>database schema {v?.schema_version}</Pill>
          {upd.available ? <Button kind="primary" onClick={upd.open}>Update {upd.available.version} available — view</Button> :
            <Button disabled={upd.checking} onClick={() => upd.check(true)}>{upd.checking ? "Checking…" : "Check for updates"}</Button>}
        </div>
        <p className="mt-2 text-[13px] text-[var(--muted)]">{upd.lastChecked ? `Last checked ${new Date(upd.lastChecked).toLocaleTimeString("en-IN")}. ` : ""}The app checks at start-up and every 6 hours. Only updates signed with this app's release key are accepted.</p>
        {upd.error && <p className="mt-2 text-[14px] text-[var(--kumkum)]">{upd.error}</p>}
        {v?.startup?.update && <p className="mt-2 text-[14px]">Updated from {v.startup.update.from} to {v.startup.update.to}{v.startup.backup ? ` (backup ${v.startup.backup} made before upgrading the database)` : ""}.</p>}
      </Panel>
      <Panel title="Update history">
        {(hist || []).length === 0 ? <p className="text-[14px] text-[var(--muted)]">No updates yet.</p> : (
          <ul className="space-y-1 text-[14px]">{(hist || []).map((h, i) => <li key={i}><Pill tone={h.status === "installed" ? "good" : h.status === "failed" ? "bad" : "neutral"}>{h.status}</Pill> {h.from} → {h.to} · {when(h.at)}{h.detail ? ` · ${h.detail}` : ""}</li>)}</ul>)}
      </Panel>
      <Panel title="Backups" aside={<Button onClick={async () => { await api("/backups", { method: "POST" }); reload(); toast("Backup made"); }}>Back up now</Button>}>
        <p className="mb-3 text-[13px] text-[var(--muted)]">A backup of the database is made automatically before every update and database upgrade (the last 12 are kept) in <span className="break-all">{b?.dir}</span>.</p>
        <ul className="space-y-1 text-[14px]">{(b?.backups || []).map((x: any) => (
          <li key={x.name} className="flex flex-wrap items-center justify-between gap-2"><span>{x.name} · {(x.bytes / 1e6).toFixed(1)} MB</span>
            <Button kind="ghost" onClick={async () => { try { const r = await api(`/backups/${x.name}/restore`, { method: "POST" }); toast(r.message); } catch (e: any) { toast(e.message, true); } }}>Restore at next start</Button></li>))}</ul>
        <p className="mt-3 text-[13px] text-[var(--muted)]">If an update ever goes wrong: your data is never deleted. Reinstall the previous version from the Releases page and restore the backup made before the update.</p>
      </Panel>
    </div>
  );
}
