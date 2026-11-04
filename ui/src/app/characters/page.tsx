"use client";
import { mediaUrl } from "@/lib/engine";
import { Empty, ErrorNote, PageTitle, Panel } from "@/components/ui";
import { useApi } from "@/lib/api";

export default function Characters() {
  const { data, error } = useApi<any[]>("/characters");
  return (
    <>
      <PageTitle title="Character library" sub="Recurring characters keep their look and voice across episodes. Reference images are sent to the image model to improve consistency; exact identity across scenes is not guaranteed by any current provider." />
      <ErrorNote error={error} />
      {data && !data.length && <Empty title="No characters yet">Characters are registered the first time a script introduces them.</Empty>}
      <div className="grid gap-5 md:grid-cols-2">
        {(data || []).map((c) => (
          <Panel key={c.id}>
            <div className="flex gap-4">
              {c.reference_asset_id ? <img src={mediaUrl(`/api/media/${c.reference_asset_id}`)} alt={`Reference image of ${c.name_en || c.name}`} className="h-36 w-36 shrink-0 rounded-lg object-cover" />
                : <div className="grid h-36 w-36 shrink-0 place-items-center rounded-lg bg-[var(--panel-2)] text-[13px] text-[var(--muted)]">No reference yet</div>}
              <div className="min-w-0">
                <h2 className="text-[20px] font-semibold">{c.name}</h2>
                <div className="text-[14px] text-[var(--muted)]">{c.name_en}{c.age && `, ${c.age}`}</div>
                <p className="mt-2 text-[14px]">{c.description}</p>
              </div>
            </div>
            <dl className="mt-4 grid gap-x-4 gap-y-1 text-[14px] md:grid-cols-[110px_1fr]">
              <dt className="text-[var(--muted)]">Hair</dt><dd>{c.hairstyle || "—"}</dd>
              <dt className="text-[var(--muted)]">Face</dt><dd>{c.facial_attributes || "—"}</dd>
              <dt className="text-[var(--muted)]">Clothing</dt><dd>{c.clothing || "—"}</dd>
              <dt className="text-[var(--muted)]">Personality</dt><dd>{c.personality || "—"}</dd>
              <dt className="text-[var(--muted)]">Appears in</dt><dd>{c.story_history.length ? c.story_history.map((h: any) => h.title).join(", ") : "—"}</dd>
            </dl>
          </Panel>
        ))}
      </div>
    </>
  );
}
