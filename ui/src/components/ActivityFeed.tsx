"use client";
import Link from "next/link";
import { useEffect, useState } from "react";
import { ago, useApi } from "@/lib/api";
import { useShell } from "./Shell";
import { Empty } from "./ui";

const AGENT_NAMES: Record<string, string> = {
  executive: "Executive", research: "Research", strategy: "Strategy", script: "Script writer", continuity: "Continuity",
  character: "Characters", image: "Images", video: "Video", voice: "Voice & audio", editor: "Editor", render: "Renderer",
  qa: "Quality", metadata: "Thumbnail & metadata", publisher: "Publisher", analytics: "Analytics", growth: "Growth",
  recovery: "Recovery", orchestrator: "Orchestrator",
};

export function ActivityFeed({ limit = 40, projectId }: { limit?: number; projectId?: string }) {
  const q = `/activity?limit=${limit}${projectId ? `&project_id=${projectId}` : ""}`;
  const { data } = useApi<any[]>(q);
  const [items, setItems] = useState<any[]>([]);
  const { lastEvent } = useShell();
  useEffect(() => { if (data) setItems(data); }, [data]);
  useEffect(() => {
    if (lastEvent?.type === "activity" && (!projectId || lastEvent.project_id === projectId)) {
      setItems((prev) => [{ ...lastEvent, id: lastEvent.id ?? Math.random() }, ...prev].slice(0, limit));
    }
  }, [lastEvent, projectId, limit]);
  if (data && !items.length) return <Empty title="No agent activity yet">Start automation or create a video, and each agent's work appears here as it happens.</Empty>;
  return (
    <ol className="divide-y divide-[var(--line)]" aria-live="polite">
      {items.map((a) => (
        <li key={a.id} className="grid grid-cols-[110px_1fr_auto] gap-3 py-2.5 text-[14px]">
          <span className={`font-medium ${a.level === "error" ? "text-[var(--kumkum)]" : a.level === "warning" ? "text-[var(--marigold)]" : "text-[var(--violet)]"}`}>
            {AGENT_NAMES[a.agent] || a.agent || "System"}
          </span>
          <span className="min-w-0 break-words">
            {a.message}
            {a.project_id && !projectId && <> · <Link className="text-[var(--muted)] underline-offset-2 hover:underline" href={`/videos/detail/?id=${a.project_id}`}>open video</Link></>}
          </span>
          <time className="whitespace-nowrap text-[13px] text-[var(--muted)]" dateTime={a.at}>{ago(a.at)}</time>
        </li>
      ))}
    </ol>
  );
}
