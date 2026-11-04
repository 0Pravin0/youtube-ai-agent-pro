"use client";
import { ReactNode, useEffect, useState } from "react";
import { Button, Field, Pill, inputCls } from "./ui";
import { api, useApi } from "@/lib/api";
import { mediaUrl, openExternal } from "@/lib/engine";

export const gb = (b: number) => (b >= 1e9 ? `${(b / 1e9).toFixed(1)} GB` : `${Math.max(1, Math.round(b / 1e6))} MB`);
const GROUPS: [string, string, string][] = [
  ["engine", "Engines", "Programs that run the free AI models on this Mac."],
  ["writer_model", "Writer model (Tamil stories, scripts, titles)", "Pick one. The recommended one fits your Mac."],
  ["image_model", "Image model (illustrations, characters, thumbnails)", "Pick one. The recommended one fits your Mac."],
  ["voice", "Tamil voice", "Natural Tamil narration and character voices."],
];

function Ext({ href, children }: { href: string; children: ReactNode }) {
  return <button type="button" className="underline" onClick={() => openExternal(href)}>{children}</button>;
}

export function ComputerCheck({ compact = false }: { compact?: boolean }) {
  const { data: hw } = useApi<any>("/system/hardware");
  if (!hw) return <p className="text-[var(--muted)]">Checking this Mac…</p>;
  const mac = hw.mac;
  const gpu = (hw.gpus || [])[0];
  const tone = hw.profile === "HIGH" ? "good" : hw.profile === "MEDIUM" ? "accent" : "warn";
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <Pill tone={tone as any}>{hw.profile === "LOW" ? "Light setup" : hw.profile === "MEDIUM" ? "Standard setup" : "Large setup"}</Pill>
        <span className="text-[14px] text-[var(--muted)]">
          {hw.profile === "LOW" && "Light models, one video at a time, models unloaded between stages."}
          {hw.profile === "MEDIUM" && "Recommended models with room to spare."}
          {hw.profile === "HIGH" && "Larger models and two videos at once."}
        </span>
      </div>
      <dl className={`grid gap-3 text-[14px] ${compact ? "sm:grid-cols-3" : "sm:grid-cols-3 lg:grid-cols-6"}`}>
        <Spec k="Mac" v={mac ? `${mac.chip} · macOS ${mac.macos}` : hw.os} />
        <Spec k={mac ? "Unified memory" : "Memory (RAM)"} v={`${mac?.memory_gb ?? hw.ram_gb} GB`} />
        <Spec k="Graphics" v={gpu ? `${gpu.name}${gpu.driver ? ` · ${gpu.driver}` : ""}` : "none found"} />
        <Spec k="Free disk" v={`${hw.disk_free_gb} GB`} />
        {!compact && <Spec k="FFmpeg" v={hw.ffmpeg.available ? "bundled ✓" : "missing"} />}
        {!compact && <Spec k="Blender" v={hw.blender.available ? "found (not used yet)" : "not installed (not needed)"} />}
      </dl>
      {(hw.notes || []).map((n: string) => <p key={n} className="rounded-lg bg-[var(--panel-2)] px-3 py-2 text-[14px]">{n}</p>)}
    </div>
  );
}

function Spec({ k, v }: { k: string; v: ReactNode }) {
  return <div className="rounded-lg border border-[var(--line)] px-3 py-2"><dt className="text-[12px] text-[var(--muted)]">{k}</dt><dd className="font-medium break-words">{v}</dd></div>;
}

export function ToolsList({ toast, onlyRecommended = false, groups }: { toast: (m: string, bad?: boolean) => void; onlyRecommended?: boolean; groups?: string[] }) {
  const { data, reload } = useApi<any>("/tools");
  const [confirm, setConfirm] = useState<any>(null);
  const running = (data?.items || []).some((i: any) => i.job && ["queued", "running"].includes(i.job.state));
  useEffect(() => {
    const t = setInterval(reload, running ? 2000 : 15000);
    return () => clearInterval(t);
  }, [running, reload]);
  if (!data) return <p className="text-[var(--muted)]">Loading tools…</p>;
  const rec = data.recommended;
  const isRec = (id: string) => [rec.writer_model, rec.image_model, rec.voice_model, ...rec.engines].includes(id);
  const install = async (it: any) => {
    try {
      await api(`/tools/${it.id}/install`, { method: "POST", body: { approved: true, size_bytes: it.size_bytes } });
      toast(`Installing ${it.title}`); setConfirm(null); reload();
    } catch (e: any) { toast(e.message, true); setConfirm(null); reload(); }
  };
  const select = async (it: any) => {
    try {
      await api("/tools/select", { method: "POST", body: it.group === "writer_model" ? { writer_model: it.id } : { image_model: it.id } });
      toast(`${it.title} is now in use`); reload();
    } catch (e: any) { toast(e.message, true); }
  };
  const inUse = (it: any) => (it.group === "writer_model" && data.active.writer_model === it.extra.ollama_tag) ||
    (it.group === "image_model" && data.active.image_model === it.id);
  return (
    <div className="space-y-6">
      {GROUPS.filter(([g]) => !groups || groups.includes(g)).map(([g, title, sub]) => {
        const items = data.items.filter((i: any) => i.group === g && (!onlyRecommended || isRec(i.id) || i.installed));
        if (!items.length) return null;
        return (
          <section key={g}>
            <h3 className="font-semibold">{title}</h3>
            <p className="mb-2 text-[13px] text-[var(--muted)]">{sub}</p>
            <ul className="space-y-2">
              {items.map((it: any) => {
                const j = it.job;
                const busy = j && ["queued", "running"].includes(j.state);
                const pct = j && j.total_bytes ? Math.min(100, Math.round((100 * j.done_bytes) / j.total_bytes)) : null;
                return (
                  <li key={it.id} className="rounded-xl border border-[var(--line)] p-4">
                    <div className="flex flex-wrap items-start justify-between gap-3">
                      <div className="min-w-0 flex-1">
                        <div className="flex flex-wrap items-center gap-2 font-medium">{it.title}
                          {isRec(it.id) && <Pill tone="accent">Recommended</Pill>}
                          {it.installed ? <Pill tone="good">Installed</Pill> : <Pill>{gb(it.size_bytes)} download</Pill>}
                          {inUse(it) && <Pill tone="good">In use</Pill>}
                        </div>
                        <div className="mt-1 text-[14px] text-[var(--muted)]">{it.purpose}</div>
                        <div className="mt-1 text-[13px] text-[var(--muted)]">Licence: <Ext href={it.license_url}>{it.license}</Ext> · Commercial use: {it.commercial}{it.pinned ? ` · Version ${it.pinned}` : ""}</div>
                      </div>
                      <div className="flex gap-2">
                        {!it.installed && !busy && <Button kind={isRec(it.id) ? "primary" : "secondary"} onClick={() => setConfirm(it)}>Install…</Button>}
                        {it.installed && (it.group === "writer_model" || it.group === "image_model") && !inUse(it) && <Button onClick={() => select(it)}>Use this</Button>}
                      </div>
                    </div>
                    {j && (busy || j.state === "failed") && (
                      <div className="mt-3">
                        {busy && <div className="h-2 overflow-hidden rounded bg-[var(--panel-2)]"><div className="h-full bg-[var(--marigold)] transition-all" style={{ width: `${pct ?? 5}%` }} /></div>}
                        <p className={`mt-1 text-[13px] ${j.state === "failed" ? "text-[var(--kumkum)]" : "text-[var(--muted)]"}`}>
                          {j.state === "failed" ? `Failed: ${j.error}` : `${j.phase}${pct != null ? ` — ${pct}% (${gb(j.done_bytes)} of ${gb(j.total_bytes)})` : ""}`}</p>
                        {j.state === "failed" && <Button onClick={() => setConfirm(it)}>Try again</Button>}
                      </div>
                    )}
                  </li>
                );
              })}
            </ul>
          </section>
        );
      })}
      {confirm && (
        <div role="dialog" aria-modal="true" className="fixed inset-0 z-50 grid place-items-center bg-black/40 p-6" onClick={() => setConfirm(null)}>
          <div className="w-full max-w-[520px] rounded-2xl bg-[var(--panel)] p-6 shadow-xl" onClick={(e) => e.stopPropagation()}>
            <h3 className="text-[19px] font-semibold">Download {confirm.title}?</h3>
            <dl className="mt-3 space-y-1.5 text-[15px]">
              <div><b>Download size:</b> {gb(confirm.size_bytes)}</div>
              <div><b>Licence:</b> {confirm.license} — commercial use: {confirm.commercial}</div>
              <div><b>Saved to:</b> <span className="break-all text-[14px]">{data.tools_dir}</span></div>
              <div><b>Free disk space:</b> {data.disk_free_gb} GB</div>
            </dl>
            <p className="mt-3 text-[14px] text-[var(--muted)]">It's free. Nothing is bought and no account is charged. Every file is checked against the verified checksum recorded in this release before it is used; a file that doesn't match is deleted.</p>
            <div className="mt-5 flex justify-end gap-2"><Button kind="ghost" onClick={() => setConfirm(null)}>Cancel</Button>
              <Button kind="primary" onClick={() => install(confirm)}>Approve download ({gb(confirm.size_bytes)})</Button></div>
          </div>
        </div>
      )}
    </div>
  );
}

export function EngineControls({ toast }: { toast: (m: string, bad?: boolean) => void }) {
  const { data, reload } = useApi<any>("/tools", 10000);
  const [check, setCheck] = useState<any>(null);
  if (!data) return null;
  const sv = data.services;
  const start = async (e: string) => { try { await api(`/tools/${e}/start`, { method: "POST" }); toast(`${e} started`); reload(); } catch (x: any) { toast(x.message, true); } };
  const tamil = async () => { try { setCheck(await api("/tools/writer/check", { method: "POST" })); } catch (x: any) { toast(x.message, true); } };
  return (
    <div className="grid gap-3 sm:grid-cols-2">
      <div className="rounded-xl border border-[var(--line)] p-4">
        <div className="flex items-center justify-between"><b>Writer (Ollama)</b><Pill tone={sv.ollama.running ? "good" : "warn"}>{sv.ollama.running ? "Running" : "Stopped"}</Pill></div>
        <p className="mt-1 text-[14px] text-[var(--muted)]">Model: {data.active.writer_model || "none chosen"}</p>
        <div className="mt-2 flex gap-2">{!sv.ollama.running && <Button onClick={() => start("ollama")}>Start</Button>}
          {sv.ollama.running && data.active.writer_model && <Button onClick={tamil}>Test Tamil writing</Button>}</div>
        {check && <p className={`mt-2 text-[14px] ${check.ok ? "" : "text-[var(--kumkum)]"}`}>{check.ok ? "✓ " : "✗ "}{check.sample} <span className="text-[var(--muted)]">({check.seconds}s, {check.tokens_per_second} tokens/s)</span></p>}
      </div>
      <div className="rounded-xl border border-[var(--line)] p-4">
        <div className="flex items-center justify-between"><b>Images (ComfyUI)</b><Pill tone={sv.comfyui.running ? "good" : "warn"}>{sv.comfyui.running ? "Running" : "Stopped"}</Pill></div>
        <p className="mt-1 text-[14px] text-[var(--muted)]">Model: {data.active.image_checkpoint || "none chosen"}{sv.comfyui.devices?.[0] ? ` · on ${sv.comfyui.devices[0].name}` : ""}</p>
        <div className="mt-2 flex gap-2">{!sv.comfyui.running ? <Button onClick={() => start("comfyui")}>Start</Button> :
          <Button kind="ghost" onClick={async () => { await api("/tools/comfyui/stop", { method: "POST" }); reload(); }}>Stop</Button>}</div>
      </div>
    </div>
  );
}

export function HfToken({ toast }: { toast: (m: string, bad?: boolean) => void }) {
  const { data, reload } = useApi<any>("/tools");
  const [v, setV] = useState("");
  if (!data) return null;
  const save = async () => { try { await api("/setup/keys", { method: "PUT", body: { hf_token: v.trim() } }); setV(""); toast("Token saved in Windows Credential Manager"); reload(); } catch (e: any) { toast(e.message, true); } };
  return (
    <div className="rounded-xl border border-[var(--line)] p-4">
      <div className="flex items-center justify-between gap-2"><b>Free Hugging Face access (for the Tamil voice model)</b>{data.hf_token_set ? <Pill tone="good">Saved</Pill> : <Pill tone="warn">Needed</Pill>}</div>
      <ol className="mt-2 list-decimal space-y-1 pl-5 text-[14px] text-[var(--muted)]">
        <li>Create a free account at <Ext href="https://huggingface.co/join">huggingface.co</Ext> (if you don't have one).</li>
        <li>Open the <Ext href="https://huggingface.co/ai4bharat/indic-parler-tts">Indic Parler-TTS page</Ext> and click <b>Agree and access repository</b>.</li>
        <li>Create a token at <Ext href="https://huggingface.co/settings/tokens">Settings → Access Tokens</Ext> (type: <b>Read</b>) and paste it here.</li>
      </ol>
      <div className="mt-3 flex flex-wrap gap-2">
        <input className={`${inputCls} min-w-[260px] flex-1`} type="password" autoComplete="off" value={v} onChange={(e) => setV(e.target.value)} placeholder={data.hf_token_set ? "Saved — paste a new token to replace" : "hf_…"} aria-label="Hugging Face token" />
        <Button kind="primary" disabled={!v.trim()} onClick={save}>Save</Button>
      </div>
    </div>
  );
}

export function VoiceSample({ toast }: { toast: (m: string, bad?: boolean) => void }) {
  const { data, reload } = useApi<any>("/tools");
  const [res, setRes] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  const [voice, setVoice] = useState("narrator");
  if (!data) return null;
  const ready = data.items.find((i: any) => i.id === "voice-model")?.installed && data.items.find((i: any) => i.id === "voice-engine")?.installed;
  const make = async () => {
    setBusy(true); setRes(null);
    try { setRes(await api("/voice/sample", { method: "POST", body: { voice } })); } catch (e: any) { toast(e.message, true); }
    setBusy(false);
  };
  const approve = async (ok: boolean) => { await api("/voice/approve", { method: "POST", body: { approved: ok } }); toast(ok ? "Tamil voice approved" : "Voice not approved — videos will wait"); reload(); };
  return (
    <div className="rounded-xl border border-[var(--line)] p-4">
      <div className="flex items-center justify-between gap-2"><b>Listen and approve the Tamil voice</b>
        {data.active.voice_sample_approved ? <Pill tone="good">Approved</Pill> : <Pill tone="warn">Not approved</Pill>}</div>
      <p className="mt-1 text-[14px] text-[var(--muted)]">Videos are only made after you have heard the voice and confirmed it sounds natural. If it doesn't, the app pauses instead of publishing unusable audio.</p>
      {!ready ? <p className="mt-2 text-[14px]">Install the Tamil voice engine and model first.</p> : (
        <div className="mt-3 space-y-3">
          <div className="flex flex-wrap items-end gap-2">
            <Field label="Voice"><select className={inputCls} value={voice} onChange={(e) => setVoice(e.target.value)}>
              <option value="narrator">Narrator (Jaya)</option><option value="child_female">Girl (Kavitha)</option><option value="adult_female">Woman (Kavitha)</option>
              <option value="adult_male">Man (described voice)</option><option value="elder_male">Old man (described voice)</option></select></Field>
            <Button kind="primary" disabled={busy} onClick={make}>{busy ? "Generating… (first time loads the model, can take minutes)" : "Play a sample"}</Button>
          </div>
          {res && (
            <div className="space-y-2">
              <audio controls autoPlay src={mediaUrl(`/api/voice/sample/${res.file}`)} className="w-full" />
              <p className="text-[13px] text-[var(--muted)]">{res.seconds}s of speech made in {res.wall_seconds}s on {res.device}. A 45-second Short needs about {res.estimate_short_minutes} min of voice generation on this Mac.</p>
              <div className="flex gap-2"><Button kind="primary" onClick={() => approve(true)}>It sounds natural — approve</Button><Button kind="danger" onClick={() => approve(false)}>Not good enough</Button></div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
