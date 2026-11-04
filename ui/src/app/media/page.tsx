"use client";
import { mediaUrl } from "@/lib/engine";
import Link from "next/link";
import { useState } from "react";
import { AssetGrid } from "@/components/AssetGrid";
import { ErrorNote, PageTitle, Panel } from "@/components/ui";
import { ago, useApi } from "@/lib/api";

const KINDS: [string, string][] = [["SCENE_IMAGE", "Scene images"], ["CHARACTER_REFERENCE", "Character references"], ["THUMBNAIL", "Thumbnails"],
  ["SCENE_VIDEO", "Generated video clips"], ["VOICE_LINE", "Voice lines"], ["MIXED_AUDIO", "Audio mixes"], ["RENDER", "Final renders"]];

export default function Media() {
  const [kind, setKind] = useState("SCENE_IMAGE");
  const { data, error } = useApi<any[]>(`/assets?kind=${kind}&limit=200`);
  const visual = ["SCENE_IMAGE", "CHARACTER_REFERENCE", "THUMBNAIL"].includes(kind);
  return (
    <>
      <PageTitle title="Media assets" sub="Every generated or rendered file, with the provider that produced it and its licence notes." />
      <div className="mb-5 flex flex-wrap gap-2" role="group" aria-label="Asset type">
        {KINDS.map(([k, l]) => (
          <button key={k} onClick={() => setKind(k)} aria-pressed={kind === k}
            className={`rounded-full border px-3 py-1 text-[14px] ${kind === k ? "border-[var(--marigold)] text-[var(--text)]" : "border-[var(--line)] text-[var(--muted)]"}`}>{l}</button>
        ))}
      </div>
      <ErrorNote error={error} />
      <Panel>
        {visual ? <AssetGrid assets={data || []} /> : (
          (data || []).length === 0 ? <p className="text-[var(--muted)]">No files of this type yet.</p> :
          <table className="tbl text-[14px]">
            <thead><tr><th>File</th><th>Details</th><th>Licence / provider</th><th>Created</th></tr></thead>
            <tbody>{(data || []).map((a) => (
              <tr key={a.id}>
                <td>{a.mime.startsWith("audio") ? <audio controls preload="none" src={mediaUrl(`/api/media/${a.id}`)} className="h-8 w-56" /> :
                  <a className="underline" href={mediaUrl(`/api/media/${a.id}`)} target="_blank" rel="noreferrer">Open {a.mime.split("/")[1]}</a>}</td>
                <td>{a.duration_s ? `${a.duration_s.toFixed(1)} s` : ""}{a.width ? ` ${a.width}×${a.height}` : ""}{a.motion_kind ? ` ${a.motion_kind === "GENERATED_VIDEO" ? "AI video" : "still + camera motion"}` : ""}{a.extra?.speaker ? ` ${a.extra.speaker}` : ""}</td>
                <td>{a.license || a.provider || "—"}</td>
                <td>{a.project_id ? <Link className="underline" href={`/videos/detail/?id=${a.project_id}`}>{ago(a.created_at)}</Link> : ago(a.created_at)}</td>
              </tr>))}</tbody>
          </table>
        )}
      </Panel>
    </>
  );
}
