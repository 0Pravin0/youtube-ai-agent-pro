"use client";
import { useRouter } from "next/navigation";
import { useShell } from "@/components/Shell";
import { Button, Empty, ErrorNote, PageTitle, Panel, Pill } from "@/components/ui";
import { ago, api, useApi } from "@/lib/api";

const CRITERIA: [string, string][] = [["topic_relevance", "Relevance"], ["audience_fit", "Audience fit"], ["originality", "Originality"],
  ["historical_performance", "Past performance"], ["production_feasibility", "Feasibility"], ["cost_efficiency", "Cost"], ["duplication_risk", "Duplication risk"]];

export default function Ideas() {
  const { data, error, reload } = useApi<any[]>("/ideas?limit=200");
  const { toast } = useShell();
  const router = useRouter();
  const make = async (i: any) => {
    try {
      const p = await api("/videos/create", { method: "POST", body: { format: i.format, idea_id: i.id } });
      toast("Video queued from this idea");
      router.push(`/videos/detail/?id=${p.id}`);
    } catch (e: any) { toast(e.message, true); }
  };
  return (
    <>
      <PageTitle title="Content Ideas" sub="Ideas found by the research agent, ranked by a transparent weighted score. Ratings are 0–10 heuristics for ranking, not predictions of views." />
      <ErrorNote error={error} />
      {data && !data.length && <Empty title="No ideas yet">The research agent creates ideas at the start of each video, using web search and your channel’s analytics.</Empty>}
      <div className="space-y-4">
        {(data || []).map((i) => (
          <Panel key={i.id}>
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div className="min-w-0 max-w-[75ch]">
                <h2 className="text-[18px] font-semibold">{i.title}</h2>
                <div className="text-[14px] text-[var(--muted)]">{i.title_en}</div>
                <p className="mt-2">{i.logline}</p>
                {i.rationale && <p className="mt-2 text-[14px] text-[var(--muted)]">{i.rationale}</p>}
              </div>
              <div className="flex flex-col items-end gap-2">
                <div className="flex gap-2">
                  <Pill>{i.format === "SHORT" ? "Short" : "Long-form"}</Pill>
                  <Pill tone={i.status === "NEW" ? "accent" : i.status === "REJECTED" ? "bad" : "neutral"}>{i.status.toLowerCase()}</Pill>
                </div>
                {i.total_score > 0 && <div className="text-[22px] font-semibold">{i.total_score.toFixed(2)}</div>}
              </div>
            </div>
            <div className="mt-4 grid grid-cols-2 gap-x-6 gap-y-2 sm:grid-cols-4 lg:grid-cols-7">
              {CRITERIA.map(([k, l]) => (
                <div key={k}>
                  <div className="text-[13px] text-[var(--muted)]">{l}</div>
                  <div className="mt-1 h-1.5 rounded-full bg-[var(--panel-2)]">
                    <div className="h-1.5 rounded-full" style={{ width: `${(i.scores[k] ?? 0) * 10}%`, background: k === "duplication_risk" ? "var(--kumkum)" : "var(--marigold)" }} />
                  </div>
                  <div className="text-[13px]">{i.scores[k]}</div>
                </div>
              ))}
            </div>
            <div className="mt-4 flex flex-wrap items-center gap-3 text-[13px] text-[var(--muted)]">
              <span>Found {ago(i.created_at)}</span>
              {(i.sources || []).slice(0, 3).map((s: string) => s.startsWith("http") ?
                <a key={s} href={s} target="_blank" rel="noreferrer" className="max-w-[260px] truncate underline">{new URL(s).hostname}</a> : <span key={s}>{s}</span>)}
              {i.status === "NEW" && <span className="ml-auto flex gap-2">
                <Button onClick={() => make(i)}>Make this video</Button>
                <Button kind="ghost" onClick={async () => { await api(`/ideas/${i.id}/reject`, { method: "POST" }); reload(); }}>Reject</Button>
              </span>}
            </div>
          </Panel>
        ))}
      </div>
    </>
  );
}
