"use client";
import Link from "next/link";
import { useEffect } from "react";
import { ActivityFeed } from "@/components/ActivityFeed";
import { PipelineThread } from "@/components/PipelineThread";
import { useShell } from "@/components/Shell";
import { Empty, Panel, Pill, Stat, stageTone } from "@/components/ui";
import { STAGE_LABEL, ago, ist, usd, useApi } from "@/lib/api";

export default function Overview() {
  const { overview: ov, lastEvent } = useShell();
  const vids = useApi<any[]>("/videos?stage=active&limit=8");
  const notes = useApi<any[]>("/notifications");
  const sched = useApi<any>("/schedule");
  const uploads = useApi<any>("/analytics");
  const res = useApi<any>("/system/resources", 5000);
  useEffect(() => { if (lastEvent?.type === "project") vids.reload(); if (lastEvent?.type === "notification") notes.reload(); }, [lastEvent]); // eslint-disable-line
  const next = (sched.data?.slots || []).filter((s: any) => new Date(s.slot_at) > new Date()).slice(0, 4);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-[28px] font-semibold leading-tight">Dashboard</h1>
        <p className="mt-1 text-[var(--muted)]">
          {ov ? <>Automation is <b className="text-[var(--text)]">{ov.automation.state.toLowerCase()}</b> in {ov.automation.mode.replace("_", " ").toLowerCase()} mode.
            {!ov.automation.publishing_authorized && <> Autonomous publishing is not authorized yet — <Link className="underline" href="/settings">authorize it in Settings</Link>.</>}</> : "Loading…"}
        </p>
      </div>

      {ov && (
        <Panel>
          <div className="grid grid-cols-2 gap-5 md:grid-cols-5">
            <div className="col-span-2 flex items-center gap-3 md:col-span-1">
              {ov.channel.profile?.thumbnail ? <img src={ov.channel.profile.thumbnail} alt="" className="h-12 w-12 rounded-full" referrerPolicy="no-referrer" /> : <div className="h-12 w-12 rounded-full bg-[var(--panel-2)]" />}
              <div className="min-w-0"><div className="truncate font-semibold">{ov.channel.name}</div>
                <div className="text-[13px] text-[var(--muted)]">{ov.channel.connected ? "Connected" : <Link className="underline" href="/connections/">Not connected</Link>}</div></div>
            </div>
            <Stat label="Subscribers" value={ov.channel.profile?.subscriber_count != null ? ov.channel.profile.subscriber_count.toLocaleString("en-IN") : "—"} />
            <Stat label="Published by the agent" value={ov.published_videos} />
            <Stat label="Views on those videos" value={ov.tracked_views ? ov.tracked_views.toLocaleString("en-IN") : "—"} />
            <Stat label="AI agent" value={ov.agent_status} tone={ov.agent_status === "Running" ? "good" : ov.agent_status === "Error" ? "bad" : undefined} />
          </div>
          <p className="mt-3 text-[12px] text-[var(--muted)]">Subscriber count as reported by YouTube when the channel was connected; view totals come from YouTube Analytics after its 1–3 day delay.</p>
        </Panel>
      )}

      {ov?.production && <FreeStatus pr={ov.production} res={res.data} />}

      <Panel title="Videos in production" aside={<Link href="/videos" className="text-[14px] text-[var(--muted)] hover:text-[var(--text)]">All videos</Link>}>
        {vids.data && vids.data.length === 0 && <Empty title="Nothing in production">Use “Create video now”, or start automation and the scheduler will begin the next slot’s video.</Empty>}
        <ul className="divide-y divide-[var(--line)]">
          {(vids.data || []).map((p) => (
            <li key={p.id} className="py-4 first:pt-0">
              <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
                <Link href={`/videos/detail/?id=${p.id}`} className="text-[17px] font-medium hover:underline">{p.title || (p.format === "SHORT" ? "Untitled Short" : "Untitled episode")}</Link>
                <div className="flex items-center gap-2">
                  <Pill>{p.format === "SHORT" ? "Short" : "Long-form"}</Pill>
                  <Pill tone={stageTone(p.stage)}>{STAGE_LABEL[p.stage]}</Pill>
                </div>
              </div>
              <PipelineThread stage={p.stage} resumeStage={p.resume_stage} />
              {(p.blocker || p.error) && <p className="mt-1 text-[14px] text-[var(--kumkum)]">{p.blocker || p.error}</p>}
              <div className="mt-1 text-[13px] text-[var(--muted)]">{Number(p.cost_usd) > 0 ? `Paid APIs: ${usd(p.cost_usd)}` : "Cost: ₹0 (free local tools)"} · updated {ago(p.updated_at)}</div>
              {(p.blocker || p.error) && <Link className="text-[13px] underline" href={`/videos/detail/?id=${p.id}`}>Open to see how to fix it and resume</Link>}
            </li>
          ))}
        </ul>
      </Panel>

      <div className="grid gap-6 lg:grid-cols-[1.4fr_1fr]">
        <Panel title="Live agent activity" aside={<Link href="/activity" className="text-[14px] text-[var(--muted)] hover:text-[var(--text)]">Full log</Link>}>
          <ActivityFeed limit={12} />
        </Panel>
        <div className="space-y-6">
          <Panel title="Production cost" aside={<Link href="/costs/" className="text-[14px] text-[var(--muted)] hover:text-[var(--text)]">Details</Link>}>
            {ov && (ov.production?.free_mode ? (
              <div className="grid grid-cols-2 gap-4">
                <Stat label="Estimated cost per video" value="₹0" tone="good" />
                <Stat label="Paid APIs this month" value={`₹${ov.production.paid_spend_month_inr}`} />
                <p className="col-span-2 text-[13px] text-[var(--muted)]">Free local tools only. Your computer's electricity is the only cost.</p>
              </div>
            ) : (
              <div className="grid grid-cols-2 gap-4">
                <Stat label="Today" value={usd(ov.budget.today_usd)} />
                <Stat label="Daily limit" value={usd(ov.budget.daily_budget_usd)} />
                <Stat label="This month" value={`₹${ov.production?.paid_spend_month_inr}`} />
                <Stat label="Monthly limit" value={usd(ov.budget.monthly_budget_usd)} />
              </div>
            ))}
          </Panel>
          <Panel title="Errors and recovery actions">
            {(notes.data || []).length === 0 ? <p className="text-[14px] text-[var(--muted)]">No problems reported.</p> : (
              <ul className="space-y-3">
                {(notes.data || []).slice(0, 5).map((n: any) => (
                  <li key={n.id} className={`border-l-2 pl-3 ${n.level === "ERROR" || n.level === "ACTION_REQUIRED" ? "border-[var(--kumkum)]" : n.level === "WARNING" ? "border-[var(--marigold)]" : "border-[var(--line)]"}`}>
                    <div className="text-[14px] font-medium">{n.title}</div>
                    <div className="line-clamp-2 text-[13px] text-[var(--muted)]">{n.body}</div>
                    {n.project_id && <Link className="text-[13px] underline" href={`/videos/detail/?id=${n.project_id}`}>Open video</Link>}
                  </li>
                ))}
              </ul>
            )}
          </Panel>
          <Panel title="Recent uploads" aside={<Link href="/library/" className="text-[14px] text-[var(--muted)] hover:text-[var(--text)]">Library</Link>}>
            {(uploads.data?.videos || []).length === 0 ? <p className="text-[14px] text-[var(--muted)]">Nothing uploaded yet.</p> : (
              <ul className="space-y-2">
                {uploads.data.videos.slice(0, 5).map((v: any) => (
                  <li key={v.youtube_video_id} className="flex justify-between gap-3 text-[14px]">
                    <span className="truncate">{v.title}</span><span className="whitespace-nowrap text-[var(--muted)]">{v.privacy}{v.snapshots?.["7d"] ? ` · ${v.snapshots["7d"].metrics.views ?? 0} views` : ""}</span>
                  </li>
                ))}
              </ul>
            )}
          </Panel>
          <Panel title="Upcoming videos" aside={<Link href="/calendar" className="text-[14px] text-[var(--muted)] hover:text-[var(--text)]">Calendar</Link>}>
            <ul className="space-y-2">
              {next.map((s: any) => (
                <li key={s.id} className="flex justify-between gap-3 text-[14px]">
                  <span>{ist(s.slot_at, sched.data.timezone)} · {s.format === "SHORT" ? "Short" : "Long-form"}</span>
                  <span className="text-[var(--muted)]">{s.project ? STAGE_LABEL[s.project.stage] : s.status.toLowerCase()}</span>
                </li>
              ))}
            </ul>
          </Panel>
        </div>
      </div>
    </div>
  );
}


function FreeStatus({ pr, res }: { pr: any; res: any }) {
  const item = (label: string, ok: boolean, detail: string) => (
    <div className="rounded-lg border border-[var(--line)] px-3 py-2">
      <div className="flex items-center justify-between gap-2 text-[13px] text-[var(--muted)]">{label}<Pill tone={ok ? "good" : "warn"}>{ok ? "Ready" : "Needs attention"}</Pill></div>
      <div className="mt-1 truncate text-[14px] font-medium">{detail}</div>
    </div>
  );
  const gpu = res?.gpus?.[0];
  return (
    <Panel title={<span className="flex items-center gap-2">{pr.free_mode ? <Pill tone="good">FREE MODE ACTIVE</Pill> : <Pill tone="warn">{pr.mode} MODE</Pill>} Production engines</span>}
      aside={<Link href="/tools/" className="text-[14px] text-[var(--muted)] hover:text-[var(--text)]">Tools and models</Link>}>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {item("Writer (Ollama)", !!pr.writer.model && pr.writer.running, pr.writer.model ? `${pr.writer.model}${pr.writer.running ? "" : " · stopped"}` : "No model installed")}
        {item("Images (ComfyUI)", !!pr.images.model && pr.images.running, pr.images.model ? `${pr.images.model}${pr.images.running ? "" : " · stopped"}` : "No model installed")}
        {item("Tamil voice", pr.voice.installed && pr.voice.approved, !pr.voice.installed ? "Not installed" : pr.voice.approved ? "Indic Parler-TTS · approved" : "Listen & approve the sample")}
        <div className="rounded-lg border border-[var(--line)] px-3 py-2">
          <div className="text-[13px] text-[var(--muted)]">This computer now</div>
          <div className="mt-1 text-[14px] font-medium">{res ? <>CPU {Math.round(res.cpu_pct)}% · RAM {res.ram_used_gb}/{res.ram_total_gb} GB{gpu ? ` · GPU ${gpu.utilization_pct}%` : ""}</> : "…"}</div>
          <Link href="/resources/" className="text-[12px] underline">Resource monitor</Link>
        </div>
      </div>
    </Panel>
  );
}
