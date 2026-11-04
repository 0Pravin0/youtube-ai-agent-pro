"use client";
import Link from "next/link";
import { useState } from "react";
import { AssetGrid } from "@/components/AssetGrid";
import { Empty, PageTitle, Panel, Pill } from "@/components/ui";
import { ago, ist, useApi } from "@/lib/api";
import { mediaUrl, openExternal } from "@/lib/engine";

export default function Library() {
  const [tab, setTab] = useState<"videos" | "images" | "thumbs">("videos");
  const renders = useApi<any[]>("/assets?kind=RENDER&limit=60");
  const analytics = useApi<any>("/analytics");
  const images = useApi<any[]>(tab === "images" ? "/assets?kind=SCENE_IMAGE&limit=200" : null);
  const thumbs = useApi<any[]>(tab === "thumbs" ? "/assets?kind=THUMBNAIL&limit=120" : null);
  return (
    <>
      <PageTitle title="Video Library" sub="Every finished video, exactly as it was (or will be) uploaded, plus the images and thumbnails behind it." />
      <div className="mb-5 flex gap-2" role="group" aria-label="Show">
        {([["videos", "Finished videos"], ["images", "Scene images"], ["thumbs", "Thumbnails"]] as const).map(([k, l]) => (
          <button key={k} onClick={() => setTab(k)} aria-pressed={tab === k}
            className={`rounded-full border px-3 py-1 text-[14px] ${tab === k ? "border-[var(--marigold)]" : "border-[var(--line)] text-[var(--muted)]"}`}>{l}</button>))}
      </div>
      {tab === "videos" && (
        renders.data && !renders.data.length ? <Empty title="No finished videos yet">Videos appear here after rendering. Use “Create video now” to make one.</Empty> :
        <div className="grid gap-6 md:grid-cols-2 xl:grid-cols-3">
          {(renders.data || []).map((a) => (
            <Panel key={a.id}>
              <video controls preload="metadata" src={mediaUrl(`/api/media/${a.id}`)} className={`w-full rounded-lg bg-black ${a.height > a.width ? "mx-auto max-h-[460px] w-auto" : ""}`} />
              <div className="mt-2 text-[14px] text-[var(--muted)]">{a.height > a.width ? "Short" : "Long video"} · {a.duration_s?.toFixed(0)} s · {a.extra?.generated_video_scenes ?? 0} AI-video and {a.extra?.still_motion_scenes ?? 0} illustrated scenes · {ago(a.created_at)}</div>
              {a.project_id && <Link className="text-[14px] underline" href={`/videos/detail/?id=${a.project_id}`}>Details and publishing status</Link>}
            </Panel>
          ))}
        </div>
      )}
      {tab === "images" && <Panel><AssetGrid assets={images.data || []} /></Panel>}
      {tab === "thumbs" && <Panel><AssetGrid assets={thumbs.data || []} /></Panel>}
      {tab === "videos" && (analytics.data?.videos || []).length > 0 && (
        <Panel title="On YouTube" className="mt-6" flush>
          <table className="tbl text-[14px]">
            <thead><tr><th>Title</th><th>Visibility</th><th>Published</th><th></th></tr></thead>
            <tbody>{analytics.data.videos.map((v: any) => (
              <tr key={v.youtube_video_id}><td>{v.title}</td><td><Pill tone={v.privacy === "public" ? "good" : "neutral"}>{v.privacy}</Pill></td>
                <td>{ist(v.published_at || v.publish_at)}</td>
                <td className="text-right"><button className="underline" onClick={() => openExternal(`https://youtu.be/${v.youtube_video_id}`)}>Open on YouTube</button></td></tr>))}</tbody>
          </table>
        </Panel>
      )}
    </>
  );
}
