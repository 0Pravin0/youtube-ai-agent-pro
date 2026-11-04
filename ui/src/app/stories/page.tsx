"use client";
import Link from "next/link";
import { useState } from "react";
import { Empty, ErrorNote, PageTitle, Panel, Pill } from "@/components/ui";
import { ago, useApi } from "@/lib/api";

export default function Stories() {
  const { data, error } = useApi<any[]>("/stories?limit=100");
  const [sel, setSel] = useState<string | null>(null);
  const story = useApi<any>(sel ? `/stories/${sel}` : null);
  const s = story.data?.script;
  return (
    <>
      <PageTitle title="Story Studio" actions={<Link href="/characters/" className="rounded-lg border border-[var(--line)] px-3.5 py-2 text-[14px]">Character library</Link>} sub="Every script the writing agent produced: scenes, Tamil dialogue, camera directions and the prompts sent to image and video models." />
      <ErrorNote error={error} />
      {data && !data.length ? <Empty title="No stories yet">Stories are written automatically once a video job reaches the script stage.</Empty> : (
        <div className="grid gap-6 lg:grid-cols-[320px_minmax(0,1fr)]">
          <Panel flush>
            <ul className="divide-y divide-[var(--line)]">
              {(data || []).map((st) => (
                <li key={st.id}>
                  <button onClick={() => setSel(st.id)} aria-pressed={sel === st.id}
                    className={`w-full px-4 py-3 text-left ${sel === st.id ? "bg-[var(--panel-2)]" : "hover:bg-[var(--panel-2)]"}`}>
                    <div className="font-medium">{st.title}</div>
                    <div className="text-[13px] text-[var(--muted)]">{st.format === "SHORT" ? "Short" : "Long-form"}, {st.scenes} scenes, v{st.version}, {ago(st.created_at)}</div>
                  </button>
                </li>
              ))}
            </ul>
          </Panel>
          <div className="min-w-0">
            {!s ? <Empty title="Choose a story">Pick a story on the left to read the full script.</Empty> : (
              <div className="space-y-4">
                <Panel>
                  <h2 className="text-[24px] font-semibold">{s.title_ta}</h2>
                  <div className="text-[var(--muted)]">{s.title_en}</div>
                  <p className="mt-3 max-w-[75ch]">{s.synopsis_ta}</p>
                  <div className="mt-3 flex flex-wrap gap-2">
                    <Pill>{s.ending_type}</Pill>{s.moral_or_theme && <Pill tone="accent">{s.moral_or_theme}</Pill>}
                    {story.data.language_report?.est_seconds && <Pill>~{Math.round(story.data.language_report.est_seconds)} s narration (estimate)</Pill>}
                  </div>
                  <p className="mt-3 text-[14px] text-[var(--muted)]">Art style: {s.art_style}</p>
                </Panel>
                {s.scenes.map((sc: any) => (
                  <Panel key={sc.index} title={`Scene ${sc.index + 1}: ${sc.title}`} aside={<span className="text-[14px] text-[var(--muted)]">{sc.duration_s}s planned</span>}>
                    <div className="space-y-2">
                      {sc.lines.map((l: any, i: number) => (
                        <p key={i} className="grid grid-cols-[100px_1fr] gap-3">
                          <span className="text-[14px] text-[var(--violet)]">{l.speaker === "narrator" ? "Narrator" : l.speaker}</span>
                          <span className="text-[17px] leading-relaxed">{l.text_ta}</span>
                        </p>
                      ))}
                    </div>
                    <details className="mt-3 text-[14px]">
                      <summary className="cursor-pointer text-[var(--muted)]">Direction and prompts</summary>
                      <dl className="mt-2 grid gap-x-4 gap-y-1.5 md:grid-cols-[150px_1fr]">
                        <dt className="text-[var(--muted)]">Camera</dt><dd>{sc.camera_direction}</dd>
                        <dt className="text-[var(--muted)]">Image prompt</dt><dd>{sc.visual_prompt}</dd>
                        <dt className="text-[var(--muted)]">Video prompt</dt><dd>{sc.video_prompt}</dd>
                        <dt className="text-[var(--muted)]">Avoid</dt><dd>{sc.negative_prompt || "—"}</dd>
                        <dt className="text-[var(--muted)]">Sound</dt><dd>{sc.background_audio}</dd>
                      </dl>
                    </details>
                  </Panel>
                ))}
              </div>
            )}
          </div>
        </div>
      )}
    </>
  );
}
