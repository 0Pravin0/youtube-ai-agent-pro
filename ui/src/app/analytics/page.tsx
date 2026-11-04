"use client";
import Link from "next/link";
import { Bar, BarChart, CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { Empty, ErrorNote, PageTitle, Panel, Pill } from "@/components/ui";
import { ist, useApi } from "@/lib/api";

const WINDOWS = ["24h", "7d", "30d"];
const fmt = (v: any, d = 0) => (v == null ? "—" : typeof v === "number" ? v.toLocaleString("en-IN", { maximumFractionDigits: d }) : v);

export default function Analytics() {
  const { data, error } = useApi<any>("/analytics");
  if (!data) return <ErrorNote error={error} />;
  const axis = { stroke: "var(--muted)", fontSize: 12 };
  const compare = data.videos.filter((v: any) => v.snapshots["7d"]).slice(0, 12).reverse()
    .map((v: any) => ({ name: v.title.slice(0, 18), views: v.snapshots["7d"].metrics.views, avp: v.snapshots["7d"].metrics.averageViewPercentage }));
  return (
    <>
      <PageTitle title="YouTube Analytics" sub={data.notes} />
      {!data.connected && <Empty title="Connect YouTube to see analytics"><Link className="underline" href="/connections/">Connect the channel</Link> with Google OAuth. Nothing is shown until real data arrives from the YouTube Analytics API.</Empty>}
      {data.connected && (
        <Panel title="Channel, last 28 days" className="mb-6">
          {data.channel_daily_error && <p className="text-[var(--kumkum)]">{data.channel_daily_error}</p>}
          {data.channel_daily.length === 0 && !data.channel_daily_error ? <p className="text-[var(--muted)]">The API has returned no daily rows yet. New channels and videos usually take 1–3 days to show data.</p> : (
            <div className="h-[260px]">
              <ResponsiveContainer>
                <LineChart data={data.channel_daily} margin={{ left: 0, right: 12, top: 8 }}>
                  <CartesianGrid stroke="var(--line)" vertical={false} />
                  <XAxis dataKey="day" tick={axis} tickFormatter={(d) => d.slice(5)} />
                  <YAxis tick={axis} width={48} />
                  <Tooltip contentStyle={{ background: "var(--panel)", border: "1px solid var(--line)", borderRadius: 8 }} />
                  <Line type="monotone" dataKey="views" name="Views" stroke="var(--marigold)" strokeWidth={2} dot={false} />
                  <Line type="monotone" dataKey="subscribersGained" name="Subscribers gained" stroke="var(--teal)" strokeWidth={2} dot={false} />
                </LineChart>
              </ResponsiveContainer>
            </div>
          )}
        </Panel>
      )}
      {compare.length > 1 && (
        <Panel title="First 7 days, by video" className="mb-6">
          <div className="h-[260px]">
            <ResponsiveContainer>
              <BarChart data={compare} margin={{ left: 0, right: 12, top: 8 }}>
                <CartesianGrid stroke="var(--line)" vertical={false} />
                <XAxis dataKey="name" tick={axis} interval={0} angle={-15} height={50} />
                <YAxis tick={axis} width={48} />
                <Tooltip contentStyle={{ background: "var(--panel)", border: "1px solid var(--line)", borderRadius: 8 }} />
                <Bar dataKey="views" name="Views (7d)" fill="var(--marigold)" radius={[4, 4, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </Panel>
      )}
      <Panel title="Published videos" className="mb-6" flush>
        {data.videos.length === 0 ? <div className="px-5 pb-5"><p className="text-[var(--muted)]">No videos published yet.</p></div> : (
          <table className="tbl text-[14px]">
            <thead><tr><th>Video</th>{WINDOWS.map((w) => <th key={w} className="text-right">Views {w}</th>)}<th className="text-right">Avg viewed (7d)</th><th className="text-right">Subs gained (7d)</th></tr></thead>
            <tbody>{data.videos.map((v: any) => (
              <tr key={v.youtube_video_id}>
                <td><a className="font-medium underline-offset-2 hover:underline" href={`https://youtu.be/${v.youtube_video_id}`} target="_blank" rel="noreferrer">{v.title}</a>
                  <div className="text-[13px] text-[var(--muted)]">{v.privacy}, published {ist(v.published_at)}</div></td>
                {WINDOWS.map((w) => <td key={w} className="text-right">{v.snapshots[w] ? fmt(v.snapshots[w].metrics.views) : <span className="text-[var(--muted)]">pending</span>}</td>)}
                <td className="text-right">{v.snapshots["7d"]?.metrics.averageViewPercentage != null ? `${fmt(v.snapshots["7d"].metrics.averageViewPercentage, 1)}%` : "—"}</td>
                <td className="text-right">{fmt(v.snapshots["7d"]?.metrics.subscribersGained)}</td>
              </tr>))}</tbody>
          </table>
        )}
      </Panel>
      <Panel title="Strategy memory">
        {data.strategy.length === 0 ? <p className="text-[var(--muted)]">The growth agent reviews real results every Monday. With fewer than three videos that have 7-day data it records “insufficient data” instead of changing strategy.</p> : (
          <ul className="space-y-3">{data.strategy.map((e: any, i: number) => (
            <li key={i} className="border-l-2 border-[var(--line)] pl-3">
              <div className="flex flex-wrap items-center gap-2"><Pill tone={e.kind === "experiment" ? "accent" : "neutral"}>{e.kind}</Pill>
                <Pill tone={e.confidence === "high" || e.confidence === "medium" ? "good" : "neutral"}>confidence: {e.confidence}</Pill>
                {e.kind === "experiment" && <span className="text-[13px] text-[var(--muted)]">n={e.sample_size}, {e.status.toLowerCase()}</span>}</div>
              <p className="mt-1">{e.hypothesis}</p>
              {e.evidence?.p_value != null && <p className="text-[13px] text-[var(--muted)]">A {e.evidence.mean_A}% vs B {e.evidence.mean_B}% average viewed, permutation p = {e.evidence.p_value}</p>}
              {Object.keys(e.adjustment || {}).length > 0 && <p className="text-[13px] text-[var(--teal)]">Applied: {JSON.stringify(e.adjustment)}</p>}
            </li>))}</ul>
        )}
      </Panel>
    </>
  );
}
