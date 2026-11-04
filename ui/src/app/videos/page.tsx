"use client";
import Link from "next/link";
import { useEffect, useState } from "react";
import { PipelineThread } from "@/components/PipelineThread";
import { useShell } from "@/components/Shell";
import { Empty, ErrorNote, PageTitle, Panel, Pill, stageTone } from "@/components/ui";
import { STAGE_LABEL, ago, usd, useApi } from "@/lib/api";

export default function Videos() {
  const [filter, setFilter] = useState<"active" | "">("active");
  const { data, error, reload } = useApi<any[]>(`/videos?limit=200${filter ? "&stage=active" : ""}`);
  const { lastEvent } = useShell();
  useEffect(() => { if (lastEvent?.type === "project") reload(); }, [lastEvent]); // eslint-disable-line
  return (
    <>
      <PageTitle title="Video Generation" sub="Every video job, its current stage and what it has cost so far."
        actions={<div className="flex rounded-lg border border-[var(--line)] p-0.5" role="group" aria-label="Filter">
          {([["active", "In progress"], ["", "All"]] as const).map(([v, l]) => (
            <button key={v} onClick={() => setFilter(v)} aria-pressed={filter === v}
              className={`rounded-md px-3 py-1.5 text-[14px] ${filter === v ? "bg-[var(--panel-2)] font-medium" : "text-[var(--muted)]"}`}>{l}</button>))}
        </div>} />
      <ErrorNote error={error} />
      {data && data.length === 0 ? <Empty title={filter ? "No videos in progress" : "No videos yet"}>Create one with “Create video now” in the header.</Empty> : (
        <Panel flush>
          <table className="tbl">
            <thead><tr><th>Video</th><th className="hidden md:table-cell">Pipeline</th><th>Stage</th><th className="text-right">Cost</th></tr></thead>
            <tbody>
              {(data || []).map((p) => (
                <tr key={p.id}>
                  <td className="max-w-[320px]">
                    <Link href={`/videos/detail/?id=${p.id}`} className="font-medium hover:underline">{p.title || "Untitled"}</Link>
                    <div className="text-[13px] text-[var(--muted)]">{p.format === "SHORT" ? "Short" : "Long-form"} · {p.created_by === "owner" ? "created by you" : "scheduled"} · {ago(p.created_at)}</div>
                    {(p.blocker || p.error) && <div className="mt-1 text-[13px] text-[var(--kumkum)]">{(p.blocker || p.error).slice(0, 180)}</div>}
                  </td>
                  <td className="hidden w-[360px] md:table-cell"><PipelineThread stage={p.stage} resumeStage={p.resume_stage} compact /></td>
                  <td><Pill tone={stageTone(p.stage)}>{STAGE_LABEL[p.stage]}</Pill></td>
                  <td className="text-right">{usd(p.cost_usd)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>
      )}
    </>
  );
}
