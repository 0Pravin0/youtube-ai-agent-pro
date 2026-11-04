"use client";
import { mediaUrl } from "@/lib/engine";

export function AssetGrid({ assets }: { assets: any[] }) {
  if (!assets.length) return <p className="text-[var(--muted)]">No images yet.</p>;
  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-4 lg:grid-cols-6">
      {assets.map((a) => (
        <figure key={a.id} className="overflow-hidden rounded-lg border border-[var(--line)]">
          <img src={mediaUrl(`/api/media/${a.id}`)} alt={`${a.kind.toLowerCase().replaceAll("_", " ")}${a.scene_index != null ? ` scene ${a.scene_index + 1}` : ""}`} loading="lazy"
            className="aspect-square w-full object-cover" />
          <figcaption className="px-2 py-1.5 text-[12px] text-[var(--muted)]">
            {a.kind === "SCENE_IMAGE" ? `Scene ${a.scene_index + 1}` : a.kind === "CHARACTER_REFERENCE" ? `Character: ${a.extra?.character}` : a.kind.toLowerCase()}
            {a.provider && ` (${a.provider})`}
          </figcaption>
        </figure>
      ))}
    </div>
  );
}
