"use client";
import Link from "next/link";
import { useShell } from "@/components/Shell";
import { Empty, PageTitle, Panel, Pill, Stat } from "@/components/ui";
import { useApi } from "@/lib/api";
import { openExternal } from "@/lib/engine";

export default function MyChannel() {
  const { data: o, error } = useApi<any>("/channels/overview", 60000);
  const { toast } = useShell();
  if (error) return <p className="text-[var(--kumkum)]">{error}</p>;
  if (!o) return <p className="text-[var(--muted)]">Loading your channel…</p>;
  const p = o.profile || {};
  return (
    <div className="space-y-6">
      <PageTitle title="My Channel" sub="Live information from YouTube's official APIs." />
      <Panel>
        <div className="flex flex-wrap items-center gap-4">
          {p.thumbnail ? <img src={p.thumbnail} alt="" className="h-16 w-16 rounded-full" referrerPolicy="no-referrer" /> : <div className="h-16 w-16 rounded-full bg-[var(--panel-2)]" />}
          <div className="min-w-0 flex-1">
            <div className="text-[20px] font-semibold">{p.title || "Not connected"}</div>
            <div className="text-[14px] text-[var(--muted)]">{o.channel_id || "—"}{p.custom_url ? ` · ${p.custom_url}` : ""}{o.email_hint ? ` · signed in as ${o.email_hint}` : ""}</div>
          </div>
          <Pill tone={o.connected ? "good" : "bad"}>{o.connected ? "Connected" : "Not connected"}</Pill>
          {!o.connected && <Link className="underline" href="/setup/">Connect</Link>}
        </div>
        {o.error && <p className="mt-3 text-[var(--kumkum)]">{o.error}</p>}
        <div className="mt-5 grid grid-cols-2 gap-4 sm:grid-cols-4">
          <Stat label="Subscribers" value={p.subscriber_count != null ? p.subscriber_count.toLocaleString("en-IN") : "hidden"} />
          <Stat label="Videos" value={p.video_count?.toLocaleString("en-IN") ?? "—"} />
          <Stat label="Total views" value={p.view_count?.toLocaleString("en-IN") ?? "—"} />
          <Stat label="Views (28 days)" value={o.analytics ? Number(o.analytics.views).toLocaleString("en-IN") : "—"} />
        </div>
      </Panel>
      {o.analytics && (
        <Panel title="Last 28 days (YouTube Analytics)">
          <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
            <Stat label="Minutes watched" value={Number(o.analytics.minutes_watched).toLocaleString("en-IN")} />
            <Stat label="Avg view duration" value={`${Math.round(o.analytics.avg_view_duration_s)} s`} />
            <Stat label="Subscribers gained" value={o.analytics.subscribers_gained} />
            <Stat label="Subscribers lost" value={o.analytics.subscribers_lost} />
          </div>
        </Panel>
      )}
      {o.analytics_note && <p className="text-[13px] text-[var(--muted)]">{o.analytics_note}</p>}
      <Panel title="Recent videos">
        {o.videos.length === 0 ? <Empty title="No videos yet" /> : (
          <ul className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {o.videos.map((v: any) => (
              <li key={v.id}>
                <button className="block w-full text-left" onClick={() => openExternal(`https://youtu.be/${v.id}`).catch((e) => toast(String(e), true))}>
                  {v.thumbnail && <img src={v.thumbnail} alt="" className="aspect-video w-full rounded-lg object-cover" referrerPolicy="no-referrer" />}
                  <div className="mt-2 line-clamp-2 font-medium">{v.title}</div>
                  <div className="text-[13px] text-[var(--muted)]">{v.published_at ? new Date(v.published_at).toLocaleDateString("en-IN") : ""}</div>
                </button>
              </li>))}
          </ul>
        )}
      </Panel>
    </div>
  );
}
