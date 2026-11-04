"use client";
import Link from "next/link";
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { ErrorNote, PageTitle, Panel, Pill, Stat } from "@/components/ui";
import { usd, useApi } from "@/lib/api";

export default function Costs() {
  const { data, error } = useApi<any>("/costs?days=30", 30000);
  if (!data) return <ErrorNote error={error} />;
  const b = data.budget;
  const days: Record<string, any> = {};
  for (const r of data.daily) { (days[r.date] ||= { date: r.date.slice(5) })[r.provider] = r.usd; }
  const providers = Array.from(new Set(data.daily.map((r: any) => r.provider))) as string[];
  const colors = ["var(--marigold)", "var(--teal)", "var(--violet)", "var(--kumkum)", "var(--muted)"];
  const q = data.youtube_quota_today;
  return (
    <>
      <PageTitle title="Cost dashboard" sub="Paid calls reserve their estimated cost before they run and are blocked when a daily, monthly or per-video limit would be crossed." />
      <Panel className="mb-6">
        <div className="grid grid-cols-2 gap-6 md:grid-cols-4">
          <Stat label="Spent today" value={usd(b.today_usd)} tone={b.daily_remaining_usd <= 0 ? "bad" : undefined} />
          <Stat label="Left today" value={usd(b.daily_remaining_usd)} />
          <Stat label="Spent this month" value={usd(b.month_usd)} />
          <Stat label="Left this month" value={usd(b.monthly_remaining_usd)} />
        </div>
        <p className="mt-4 text-[14px] text-[var(--muted)]">Limits: {usd(b.daily_budget_usd)} a day, {usd(b.monthly_budget_usd)} a month. <Link className="underline" href="/settings">Change limits</Link></p>
      </Panel>
      <div className="grid gap-6 lg:grid-cols-[1.5fr_1fr]">
        <Panel title="Daily spend by provider, last 30 days">
          {data.daily.length === 0 ? <p className="text-[var(--muted)]">No paid calls yet.</p> : (
            <div className="h-[280px]"><ResponsiveContainer>
              <BarChart data={Object.values(days)} margin={{ left: 0, right: 8, top: 8 }}>
                <CartesianGrid stroke="var(--line)" vertical={false} />
                <XAxis dataKey="date" tick={{ fill: "var(--muted)", fontSize: 12 }} />
                <YAxis tick={{ fill: "var(--muted)", fontSize: 12 }} width={44} tickFormatter={(v) => `$${v}`} />
                <Tooltip formatter={(v: any) => usd(v, 3)} contentStyle={{ background: "var(--panel)", border: "1px solid var(--line)", borderRadius: 8 }} />
                {providers.map((p, i) => <Bar key={p} dataKey={p} stackId="a" fill={colors[i % colors.length]} />)}
              </BarChart>
            </ResponsiveContainer></div>
          )}
        </Panel>
        <Panel title="By provider">
          <table className="tbl text-[14px]">
            <thead><tr><th>Provider</th><th className="text-right">Calls</th><th className="text-right">Cost</th></tr></thead>
            <tbody>{data.by_provider.map((p: any) => (
              <tr key={p.provider}><td>{p.provider}{p.estimated && <span className="text-[12px] text-[var(--muted)]"> (estimated from list prices)</span>}</td>
                <td className="text-right">{p.calls}</td><td className="text-right">{usd(p.usd, 3)}</td></tr>))}</tbody>
          </table>
          <div className="mt-4 space-y-1 text-[14px]">
            <div>Claude tokens: {data.claude_tokens.input.toLocaleString()} in, {data.claude_tokens.output.toLocaleString()} out</div>
            <div>YouTube quota today: {q.default_bucket} of {q.limit} units, uploads {q.upload_bucket} units</div>
            <div>Storage: {data.storage.free_gb} GB free ({data.storage.used_pct}% used)</div>
          </div>
        </Panel>
      </div>
      <Panel title="Per video" className="mt-6" flush>
        <table className="tbl text-[14px]">
          <thead><tr><th>Video</th><th>Format</th><th className="text-right">Spent</th><th className="text-right">Ceiling</th></tr></thead>
          <tbody>{data.per_video.map((v: any) => (
            <tr key={v.id}><td><Link className="hover:underline" href={`/videos/detail/?id=${v.id}`}>{v.title || "Untitled"}</Link></td><td>{v.format === "SHORT" ? "Short" : "Long-form"}</td>
              <td className="text-right">{usd(v.usd, 3)}</td><td className="text-right">{v.ceiling != null ? <>{usd(v.ceiling)} {v.usd > v.ceiling * 0.9 && <Pill tone="warn">near limit</Pill>}</> : "—"}</td></tr>))}</tbody>
        </table>
      </Panel>
    </>
  );
}
