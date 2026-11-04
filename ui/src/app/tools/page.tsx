"use client";
import { ComputerCheck, EngineControls, HfToken, ToolsList, VoiceSample } from "@/components/FreeTools";
import { useShell } from "@/components/Shell";
import { Field, PageTitle, Panel, Toggle, inputCls } from "@/components/ui";
import { api, useApi } from "@/lib/api";

export default function ToolsPage() {
  const { toast } = useShell();
  const { data, reload } = useApi<any>("/tools");
  const setOpt = async (body: any) => { try { await api("/tools/select", { method: "POST", body }); toast("Saved"); reload(); } catch (e: any) { toast(e.message, true); } };
  return (
    <div className="space-y-6">
      <PageTitle title="Free Tools Manager" sub="Free, open tools that run on this Mac. Every download shows its size and licence, waits for your approval and is checked against this release's verified checksums." />
      <Panel title="This Mac"><ComputerCheck /></Panel>
      <Panel title="Install free tools"><ToolsList toast={toast} groups={["engine", "voice"]} /></Panel>
      <p className="text-[14px] text-[var(--muted)]">Writer and image models are in <a className="underline" href="/models/">Model Manager</a>.</p>
      <Panel title="Tamil voice"><div className="space-y-4"><HfToken toast={toast} /><VoiceSample toast={toast} /></div></Panel>
      {data && (
        <Panel title="Animation and sound">
          <div className="grid gap-5 sm:grid-cols-2">
            <Field label="Animation" hint="Layered = characters cut out and animated over a moving background (motion-comic). Camera = pan/zoom over full illustrations.">
              <select className={inputCls} value={data.active.animation_mode} onChange={(e) => setOpt({ animation_mode: e.target.value })}>
                <option value="auto">Automatic — layered when characters talk, camera moves otherwise</option>
                <option value="layered">Layered animation wherever possible</option>
                <option value="camera">Camera moves over illustrations only</option>
              </select>
            </Field>
            <div className="flex items-end"><Toggle on={data.active.generated_music} onChange={(v) => setOpt({ generated_music: v })}
              label="Use an original generated background pad when no licensed music fits" /></div>
          </div>
          <p className="mt-3 text-[13px] text-[var(--muted)]">Honest labelling: videos are described as “motion-comic / layered 2D animation” or “animated-image storytelling”. Characters move as whole cut-outs (no lip-sync); true AI video needs a premium video provider.</p>
        </Panel>
      )}
    </div>
  );
}
