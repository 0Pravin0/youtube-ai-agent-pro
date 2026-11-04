"use client";
import { STAGE_LABEL } from "@/lib/api";

const PIPELINE = ["QUEUED", "RESEARCHING", "IDEA_SELECTED", "SCRIPT_GENERATION", "SCRIPT_VALIDATION", "STORYBOARD",
  "CHARACTER_GENERATION", "IMAGE_GENERATION", "VIDEO_GENERATION", "VOICE_GENERATION", "AUDIO_PROCESSING", "VIDEO_EDITING",
  "RENDERING", "QUALITY_VALIDATION", "THUMBNAIL_GENERATION", "SEO_GENERATION", "READY_TO_UPLOAD", "UPLOADING",
  "YOUTUBE_PROCESSING", "SCHEDULED", "PUBLISHED", "ANALYTICS_MONITORING", "COMPLETED"];

/** Kolam-style dotted thread: one dot per pipeline stage, linked by a gently waving line.
 *  Completed dots fill marigold, the current one pulses; control states recolor the active dot. */
export function PipelineThread({ stage, resumeStage, compact = false }: { stage: string; resumeStage?: string | null; compact?: boolean }) {
  const control = !PIPELINE.includes(stage);
  const current = control ? (resumeStage || "QUEUED") : stage;
  const idx = PIPELINE.indexOf(current);
  const gap = compact ? 13 : 30;
  const h = compact ? 18 : 40;
  const w = gap * (PIPELINE.length - 1) + 16;
  const y = (i: number) => h / 2 + (compact ? 0 : Math.sin(i * 1.15) * 7);
  const color = stage === "FAILED" || stage === "BLOCKED_BY_PROVIDER" ? "var(--kumkum)"
    : stage === "PAUSED" || stage === "RETRYING" ? "var(--marigold)" : stage === "CANCELLED" ? "var(--muted)" : "var(--marigold)";
  const path = PIPELINE.map((_, i) => `${i ? "L" : "M"}${8 + i * gap},${y(i)}`).join(" ");
  const label = `${STAGE_LABEL[stage] || stage}: stage ${idx + 1} of ${PIPELINE.length}`;
  return (
    <svg viewBox={`0 0 ${w} ${h}`} width="100%" style={{ maxWidth: w }} role="img" aria-label={label}>
      <path d={path} fill="none" stroke="var(--line)" strokeWidth={compact ? 1.2 : 1.6} strokeDasharray={compact ? "2 3" : "3 4"} />
      {PIPELINE.map((s, i) => {
        const done = i < idx || current === "COMPLETED";
        const isCur = i === idx && current !== "COMPLETED";
        const r = compact ? (isCur ? 3.6 : 2.6) : (isCur ? 6 : 4.2);
        return (
          <g key={s}>
            <circle cx={8 + i * gap} cy={y(i)} r={r}
              fill={done ? "var(--marigold)" : isCur ? color : "var(--panel)"}
              stroke={done || isCur ? color : "var(--muted)"} strokeWidth={compact ? 1 : 1.4}
              className={isCur && !control ? "kolam-current" : undefined}>
              <title>{STAGE_LABEL[s]}</title>
            </circle>
            {isCur && control && !compact && (
              <circle cx={8 + i * gap} cy={y(i)} r={r + 4} fill="none" stroke={color} strokeWidth={1.2} strokeDasharray="2 2" />
            )}
          </g>
        );
      })}
    </svg>
  );
}

export { PIPELINE };
