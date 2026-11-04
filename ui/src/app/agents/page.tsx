"use client";
import Link from "next/link";
import { useState } from "react";
import { ErrorNote, PageTitle, Panel, Pill } from "@/components/ui";
import { ago, usd, useApi } from "@/lib/api";

export default function Agents() {
  const { data, error } = useApi<any[]>("/agents", 15000);
  const [agent, setAgent] = useState<string>("");
  const runs = useApi<any[]>(`/agents/runs?limit=150${agent ? `&agent=${agent}` : ""}`, 10000);
  return (
    <>
      <PageTitle title="AI Agent Control" sub="What each agent is responsible for, the tools it may use, and every run with tokens and cost. Agents marked “code” use no LLM." />
      <ErrorNote error={error} />
      <div className="mb-6 grid gap-3 md:grid-cols-2 xl:grid-cols-3">
        {(data || []).map((a) => (
          <button key={a.key} onClick={() => setAgent(agent === a.key ? "" : a.key)} aria-pressed={agent === a.key}
            className={`rounded-xl border p-4 text-left ${agent === a.key ? "border-[var(--marigold)]" : "border-[var(--line)] hover:border-[var(--muted)]"} bg-[var(--panel)]`}>
            <div className="flex items-start justify-between gap-2">
              <span className="font-semibold">{a.name}</span>
              <span className="flex gap-1.5">{a.running && <Pill tone="accent">working</Pill>}<Pill>{a.uses_llm ? "Claude" : "code"}</Pill></span>
            </div>
            <p className="mt-1 line-clamp-3 text-[13px] text-[var(--muted)]">{a.responsibilities.split("\n\n")[0]}</p>
            <div className="mt-2 text-[13px]">{a.runs_7d} runs in 7 days{a.failed_7d > 0 && <span className="text-[var(--kumkum)]">, {a.failed_7d} failed or blocked</span>}, {usd(a.cost_7d)}</div>
            <div className="mt-1 text-[12px] text-[var(--muted)]">Tools: {a.tools.join(", ")}</div>
          </button>
        ))}
      </div>
      <Panel title={agent ? `Runs: ${agent}` : "Recent runs"} flush>
        <table className="tbl text-[14px]">
          <thead><tr><th>Agent</th><th>Stage</th><th>Status</th><th className="text-right">Tokens in / out</th><th className="text-right">Cost</th><th>When</th></tr></thead>
          <tbody>{(runs.data || []).map((r) => (
            <tr key={r.id}>
              <td>{r.agent}</td>
              <td>{r.stage}{r.attempt > 1 && ` (attempt ${r.attempt})`}{r.project_id && <> <Link className="text-[var(--muted)] underline" href={`/videos/detail/?id=${r.project_id}`}>video</Link></>}
                {r.error && <div className="max-w-[420px] truncate text-[12px] text-[var(--kumkum)]" title={r.error}>{r.error}</div>}</td>
              <td><Pill tone={r.status === "SUCCEEDED" ? "good" : r.status === "RUNNING" ? "accent" : "bad"}>{r.status.toLowerCase()}</Pill></td>
              <td className="text-right">{r.input_tokens.toLocaleString()} / {r.output_tokens.toLocaleString()}</td>
              <td className="text-right">{usd(r.cost_usd, 4)}</td>
              <td className="whitespace-nowrap">{ago(r.started_at)}</td>
            </tr>))}</tbody>
        </table>
      </Panel>
    </>
  );
}
