"use client";
import { mediaUrl } from "@/lib/engine";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useEffect } from "react";
import { ActivityFeed } from "@/components/ActivityFeed";
import { AssetGrid } from "@/components/AssetGrid";
import { PipelineThread } from "@/components/PipelineThread";
import { useShell } from "@/components/Shell";
import { Button, ErrorNote, PageTitle, Panel, Pill, stageTone } from "@/components/ui";
import { STAGE_LABEL, api, ist, usd, useApi } from "@/lib/api";

export default function Page() {
  return <Suspense><VideoDetail /></Suspense>;
}

function VideoDetail() {
  const id = useSearchParams().get("id") || "";
  const { data: d, error, reload } = useApi<any>(`/videos/${id}`);
  const prog = useApi<any>(`/videos/${id}/progress`);
  const assets = useApi<any[]>(`/videos/${id}/assets`);
  const { lastEvent, toast } = useShell();
  useEffect(() => {
    if (lastEvent?.type === "project" && lastEvent.id === id) { reload(); prog.reload(); assets.reload(); }
  }, [lastEvent]); // eslint-disable-line
  if (!d) return <ErrorNote error={error} />;

  const act = async (path: string, done: string) => {
    try { await api(`/videos/${id}/${path}`, { method: "POST" }); toast(done); reload(); prog.reload(); }
    catch (e: any) { toast(e.message, true); }
  };
  const canResume = ["PAUSED", "BLOCKED_BY_PROVIDER", "FAILED", "RETRYING"].includes(d.stage);
  const hasRender = assets.data?.some((a) => a.kind === "RENDER");
  const meta = d.metadata || {};
  const qa = d.qa;
  const latestQa = qa && (prog.data?.runs || []).filter((r: any) => r.agent === "qa").slice(-1)[0];

  return (
    <>
      <PageTitle title={d.title || "Untitled video"}
        sub={`${d.format === "SHORT" ? "Short, 1080×1920" : "Long-form, 1920×1080"} — spent ${usd(d.cost_usd)}${d.cost_ceiling_usd != null ? ` of a ${usd(d.cost_ceiling_usd)} ceiling` : ""}`}
        actions={<>
          {canResume && <Button kind="primary" onClick={() => act("resume", "Video resumed")}>Resume</Button>}
          {d.stage === "READY_TO_UPLOAD" && !meta.owner_approved_publish && <Button kind="primary" onClick={() => act("publish", "Publishing approved")}>Approve publishing</Button>}
          {!["COMPLETED", "CANCELLED", "UPLOADING", "PUBLISHED", "ANALYTICS_MONITORING", "SCHEDULED", "YOUTUBE_PROCESSING"].includes(d.stage) &&
            <Button kind="danger" onClick={() => act("cancel", "Video cancelled")}>Cancel video</Button>}
        </>} />

      <Panel className="mb-6">
        <div className="mb-3 flex flex-wrap items-center gap-3">
          <Pill tone={stageTone(d.stage)}>{STAGE_LABEL[d.stage]}</Pill>
          {d.resume_stage && d.stage !== d.resume_stage && <span className="text-[14px] text-[var(--muted)]">resumes at {STAGE_LABEL[d.resume_stage]}</span>}
          {d.retry_count > 0 && <span className="text-[14px] text-[var(--muted)]">retry {d.retry_count}</span>}
        </div>
        <PipelineThread stage={d.stage} resumeStage={d.resume_stage} />
        {d.blocker && <p className="mt-3 rounded-lg bg-[color-mix(in_oklab,var(--kumkum)_12%,transparent)] px-4 py-3 text-[var(--kumkum)]"><b>What’s needed:</b> {d.blocker}</p>}
        {d.error && !d.blocker && <p className="mt-3 break-words text-[14px] text-[var(--kumkum)]">{d.error.slice(0, 600)}</p>}
        {d.upload?.youtube_video_id && (
          <p className="mt-3">On YouTube: <a className="underline" href={`https://youtu.be/${d.upload.youtube_video_id}`} target="_blank" rel="noreferrer">youtu.be/{d.upload.youtube_video_id}</a>
            {" "}(privacy: {d.upload.privacy_actual}{d.upload.privacy_actual !== d.upload.privacy_requested ? `, requested ${d.upload.privacy_requested}` : ""})</p>
        )}
      </Panel>

      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
        <Panel title="Video preview">
          {hasRender ? (
            <video controls preload="metadata" className={`w-full rounded-lg bg-black ${d.format === "SHORT" ? "mx-auto max-h-[560px] w-auto" : ""}`} src={mediaUrl(`/api/videos/${id}/preview`)} />
          ) : <p className="text-[var(--muted)]">The preview appears here once rendering finishes.</p>}
          {d.render_manifest && (
            <p className="mt-3 text-[14px] text-[var(--muted)]">
              {d.render_manifest.generated_video_scenes} scenes use AI-generated video; {d.render_manifest.still_motion_scenes} use still illustrations with camera motion (pan/zoom).
            </p>
          )}
        </Panel>

        <Panel title="Quality checks" aside={qa?.score != null && <Pill tone={qa.score >= 80 ? "good" : "bad"}>Score {qa.score}</Pill>}>
          {!latestQa?.id && !qa ? <p className="text-[var(--muted)]">Runs after rendering. A video can only be published after every critical check passes.</p> : <QaChecks projectId={id} />}
        </Panel>
      </div>

      {meta.title && (
        <Panel title="YouTube metadata" className="mt-6">
          <dl className="grid gap-x-6 gap-y-3 md:grid-cols-[160px_1fr]">
            <dt className="text-[var(--muted)]">Title</dt><dd>{meta.title}{meta.title_transliteration && <div className="text-[14px] text-[var(--muted)]">{meta.title_transliteration}</div>}</dd>
            <dt className="text-[var(--muted)]">Description</dt><dd className="whitespace-pre-wrap text-[14px]">{meta.description}</dd>
            <dt className="text-[var(--muted)]">Tags</dt><dd className="flex flex-wrap gap-1.5">{(meta.tags || []).map((t: string) => <Pill key={t}>{t}</Pill>)}</dd>
            <dt className="text-[var(--muted)]">Playlist</dt><dd>{meta.playlist_name || "—"}{meta.episode && ` (episode ${meta.episode.episode_number})`}</dd>
          </dl>
          {d.thumbnails && (
            <div className="mt-5 grid gap-3 sm:grid-cols-3">
              {d.thumbnails.map((t: any, i: number) => {
                const a = assets.data?.find((x) => x.kind === "THUMBNAIL" && x.extra?.text === t.text);
                return (
                  <figure key={i} className={`overflow-hidden rounded-lg border ${a?.extra?.selected ? "border-[var(--marigold)]" : "border-[var(--line)]"}`}>
                    {a && <img src={mediaUrl(`/api/media/${a.id}`)} alt={`Thumbnail option: ${t.text}`} className="aspect-video w-full object-cover" />}
                    <figcaption className="px-3 py-2 text-[13px] text-[var(--muted)]">{a?.extra?.selected ? "Selected — " : ""}readability {t.readability}, sharpness {t.sharpness}, relevance {t.relevance}</figcaption>
                  </figure>
                );
              })}
            </div>
          )}
        </Panel>
      )}

      <div className="mt-6 grid gap-6 lg:grid-cols-2">
        <Panel title="Stage history">
          <ol className="space-y-2 text-[14px]">
            {(prog.data?.transitions || []).slice().reverse().map((t: any, i: number) => (
              <li key={i} className="grid grid-cols-[96px_1fr] gap-3">
                <time className="text-[var(--muted)]">{ist(t.at)}</time>
                <span><b className="font-medium">{STAGE_LABEL[t.to] || t.to}</b>{t.reason && <span className="text-[var(--muted)]"> — {t.reason}</span>}</span>
              </li>
            ))}
          </ol>
        </Panel>
        <Panel title="Agent runs">
          <table className="tbl text-[14px]">
            <thead><tr><th>Agent</th><th>Status</th><th className="text-right">Tokens</th><th className="text-right">Cost</th></tr></thead>
            <tbody>
              {(prog.data?.runs || []).slice().reverse().slice(0, 30).map((r: any) => (
                <tr key={r.id}>
                  <td>{r.agent}<div className="text-[12px] text-[var(--muted)]">{r.stage}{r.attempt > 1 && `, attempt ${r.attempt}`}</div></td>
                  <td><Pill tone={r.status === "SUCCEEDED" ? "good" : r.status === "RUNNING" ? "accent" : "bad"}>{r.status.toLowerCase()}</Pill></td>
                  <td className="text-right">{(r.input_tokens + r.output_tokens).toLocaleString()}</td>
                  <td className="text-right">{usd(r.cost_usd, 3)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>
      </div>

      <Panel title="Media assets" className="mt-6" aside={<Link href="/media" className="text-[14px] text-[var(--muted)]">All assets</Link>}>
        <AssetGrid assets={(assets.data || []).filter((a) => a.mime.startsWith("image/"))} />
      </Panel>

      <Panel title="Activity for this video" className="mt-6"><ActivityFeed projectId={id} limit={60} /></Panel>
    </>
  );
}

function QaChecks({ projectId }: { projectId: string }) {
  const { data } = useApi<any>(`/videos/${projectId}/qa`);
  if (!data) return <p className="text-[var(--muted)]">Loading checks…</p>;
  if (!data.checks?.length) return <p className="text-[var(--muted)]">No checks recorded yet.</p>;
  return (
    <ul className="divide-y divide-[var(--line)] text-[14px]">
      {data.checks.map((c: any) => (
        <li key={c.name} className="flex items-start justify-between gap-3 py-2">
          <span><b className="font-medium">{c.name.replaceAll("_", " ")}</b><span className="block text-[13px] text-[var(--muted)]">{c.detail}</span></span>
          <Pill tone={c.passed ? "good" : c.severity === "CRITICAL" ? "bad" : "warn"}>{c.passed ? "pass" : c.severity.toLowerCase()}</Pill>
        </li>
      ))}
    </ul>
  );
}
