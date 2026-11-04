"use client";
import { ComputerCheck } from "@/components/FreeTools";
import { PageTitle, Panel, Pill, Stat } from "@/components/ui";
import { useApi } from "@/lib/api";

function Bar({ pct }: { pct: number }) {
  return <div className="h-2 overflow-hidden rounded bg-[var(--panel-2)]"><div className={`h-full ${pct > 90 ? "bg-[var(--kumkum)]" : pct > 70 ? "bg-[var(--marigold)]" : "bg-[var(--teal)]"}`} style={{ width: `${Math.min(100, pct)}%` }} /></div>;
}

export default function Resources() {
  const { data: u } = useApi<any>("/system/resources", 4000);
  const { data: t } = useApi<any>("/tools", 15000);
  const { data: q } = useApi<any>("/system", 10000);
  return (
    <div className="space-y-6">
      <PageTitle title="Resource monitor" sub="Live use of this computer while the agent works. Updates every few seconds." />
      <Panel title="Right now">
        {!u ? <p className="text-[var(--muted)]">Measuring…</p> : (
          <div className="grid gap-6 sm:grid-cols-2 lg:grid-cols-3">
            <div><Stat label="Processor" value={`${Math.round(u.cpu_pct)}%`} /><Bar pct={u.cpu_pct} /></div>
            <div><Stat label="Memory" value={`${u.ram_used_gb} / ${u.ram_total_gb} GB`} /><Bar pct={u.ram_pct} /></div>
            {u.gpus.length ? u.gpus.map((g: any) => (
              <div key={g.name}><Stat label={`GPU — ${g.name}`} value={`${g.utilization_pct}% · ${g.vram_used_gb}/${g.vram_total_gb} GB`} /><Bar pct={(100 * g.vram_used_gb) / (g.vram_total_gb || 1)} /></div>
            )) : <Stat label="GPU" value="No NVIDIA GPU (work runs on the processor)" />}
          </div>
        )}
      </Panel>
      {t && (
        <Panel title="Local engines">
          <div className="flex flex-wrap gap-2">
            <Pill tone={t.services.ollama.running ? "good" : "neutral"}>Writer: {t.services.ollama.running ? "running" : "stopped"}</Pill>
            <Pill tone={t.services.comfyui.running ? "good" : "neutral"}>Images: {t.services.comfyui.running ? "running" : "stopped"}</Pill>
            <Pill tone={t.active.voice_python ? "good" : "neutral"}>Tamil voice: {t.active.voice_python ? "installed" : "not installed"}</Pill>
            {q && <Pill>Jobs running: {(q.queue?.running || []).length}</Pill>}
          </div>
          <p className="mt-3 text-[13px] text-[var(--muted)]">Engines start automatically while automation runs and stop when you quit the app from the tray.</p>
        </Panel>
      )}
      <Panel title="Processing profile"><ComputerCheck /></Panel>
    </div>
  );
}
