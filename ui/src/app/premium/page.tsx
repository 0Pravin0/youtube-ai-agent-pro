"use client";
import Link from "next/link";
import { useEffect, useState } from "react";
import { useShell } from "@/components/Shell";
import { Button, Field, PageTitle, Panel, Pill, inputCls } from "@/components/ui";
import { api, useApi } from "@/lib/api";

const KINDS: [string, string, string[]][] = [["IMAGE", "Images", ["comfyui_image", "gemini_image"]], ["TTS", "Tamil voice", ["local_tts", "google_tts"]],
  ["VIDEO", "AI video clips", ["", "gemini_omni_video", "runway_video"]]];
const NAMES: Record<string, string> = { comfyui_image: "ComfyUI (free)", local_tts: "Indic Parler-TTS (free)", gemini_image: "Google Gemini images",
  google_tts: "Google Cloud TTS", gemini_omni_video: "Gemini Omni video", runway_video: "Runway video", "": "None (free animation)" };

export default function Premium() {
  const { toast, reload: reloadShell } = useShell();
  const { data, reload } = useApi<any>("/mode");
  const [f, setF] = useState<any>(null);
  useEffect(() => { if (data) setF({ mode: data.mode, enabled_paid: data.enabled_paid, llm_choice: data.llm_choice, provider_choice: data.provider_choice,
    daily_budget_inr: data.daily_budget_inr, monthly_budget_inr: data.monthly_budget_inr, usd_inr: data.usd_inr, authorize: false }); }, [data]);
  if (!data || !f) return null;
  const set = (k: string, v: any) => setF({ ...f, [k]: v });
  const toggle = (k: string) => set("enabled_paid", f.enabled_paid.includes(k) ? f.enabled_paid.filter((x: string) => x !== k) : [...f.enabled_paid, k]);
  const save = async () => {
    try {
      const body = { ...f, daily_budget_inr: Number(f.daily_budget_inr), monthly_budget_inr: Number(f.monthly_budget_inr), usd_inr: Number(f.usd_inr),
        provider_choice: Object.fromEntries(Object.entries(f.provider_choice).filter(([, v]) => v)) };
      await api("/mode", { method: "PUT", body }); toast(f.mode === "FREE_ONLY" ? "FREE ONLY mode — no paid APIs" : `${f.mode} mode authorized`); reload(); reloadShell();
    } catch (e: any) { toast(e.message, true); }
  };
  const paid = f.mode !== "FREE_ONLY";
  return (
    <div className="space-y-6">
      <PageTitle title="Future premium upgrade" sub="Grow with free tools now. Paid AI services stay off until you choose them here, authorize them, and set rupee limits." />
      <Panel title="AI production mode" aside={<Pill tone={data.mode === "FREE_ONLY" ? "good" : "warn"}>{data.mode === "FREE_ONLY" ? "FREE MODE ACTIVE" : `${data.mode} — authorized`}</Pill>}>
        <div className="grid gap-5 sm:grid-cols-2">
          <Field label="AI Production Mode">
            <select className={inputCls} value={f.mode} onChange={(e) => set("mode", e.target.value)}>
              <option value="FREE_ONLY">FREE ONLY — local tools, ₹0 (default)</option>
              <option value="HYBRID">HYBRID — free tools plus the paid services you pick</option>
              <option value="PREMIUM">PREMIUM — paid services you pick for best quality</option>
            </select>
          </Field>
        </div>
        <ul className="mt-3 list-disc pl-5 text-[14px] text-[var(--muted)]">
          <li>The app never buys subscriptions. Paid services are pay-per-use API keys you add in <Link className="underline" href="/connections/">API connections</Link>.</li>
          <li>If a free tool fails, the job pauses and tells you why — it never switches to a paid service by itself.</li>
        </ul>
      </Panel>
      {paid && (
        <>
          <Panel title="Paid services and their costs">
            <ul className="space-y-2">
              {data.paid_providers.map((p: any) => (
                <li key={p.key} className="flex flex-wrap items-start justify-between gap-3 rounded-xl border border-[var(--line)] p-4">
                  <div className="min-w-0 flex-1"><div className="font-medium">{p.name} <Pill>{p.status === "UNCONFIGURED" ? "no key yet" : p.status.toLowerCase()}</Pill></div>
                    <div className="mt-1 text-[14px] text-[var(--muted)]">{p.cost}</div></div>
                  <label className="flex items-center gap-2"><input type="checkbox" className="h-4 w-4 accent-[var(--marigold)]" checked={f.enabled_paid.includes(p.key)} onChange={() => toggle(p.key)} />Enable</label>
                </li>))}
            </ul>
          </Panel>
          <Panel title="Which service does what">
            <div className="grid gap-5 sm:grid-cols-2">
              <Field label="Story writer"><select className={inputCls} value={f.llm_choice} onChange={(e) => set("llm_choice", e.target.value)}>
                <option value="ollama">Ollama local model (free)</option><option value="anthropic">Claude API (paid)</option></select></Field>
              {KINDS.map(([k, label, opts]) => (
                <Field key={k} label={label}><select className={inputCls} value={f.provider_choice[k] || ""} onChange={(e) => set("provider_choice", { ...f.provider_choice, [k]: e.target.value })}>
                  {opts.map((o) => <option key={o} value={o}>{NAMES[o]}</option>)}</select></Field>))}
            </div>
          </Panel>
          <Panel title="Spending limits">
            <div className="grid gap-5 sm:grid-cols-3">
              <Field label="Daily limit (₹)"><input type="number" min={0} className={inputCls} value={f.daily_budget_inr} onChange={(e) => set("daily_budget_inr", e.target.value)} /></Field>
              <Field label="Monthly limit (₹)"><input type="number" min={0} className={inputCls} value={f.monthly_budget_inr} onChange={(e) => set("monthly_budget_inr", e.target.value)} /></Field>
              <Field label="₹ per US$ (for provider prices)" hint="Providers bill in dollars"><input type="number" min={10} step={0.5} className={inputCls} value={f.usd_inr} onChange={(e) => set("usd_inr", e.target.value)} /></Field>
            </div>
            <label className="mt-4 flex items-start gap-3 rounded-xl bg-[var(--panel-2)] p-4">
              <input type="checkbox" className="mt-1 h-4 w-4 accent-[var(--marigold)]" checked={f.authorize} onChange={(e) => set("authorize", e.target.checked)} />
              <span>I authorize YouTube AI Agent Pro to use the paid services I enabled above, billed to my own API accounts, within these daily and monthly limits.</span>
            </label>
          </Panel>
        </>
      )}
      <PaidKeys mode={data.mode} toast={toast} />
      <div className="flex justify-end"><Button kind="primary" disabled={paid && !f.authorize} onClick={save}>{paid ? `Authorize ${f.mode}` : "Use FREE ONLY"}</Button></div>
    </div>
  );
}

const KEYS: [string, string, string, string][] = [
  ["anthropic_api_key", "anthropic", "Anthropic Claude API key", "https://console.anthropic.com/settings/keys"],
  ["gemini_api_key", "gemini_image", "Google Gemini API key (images, video)", "https://aistudio.google.com/apikey"],
  ["google_tts_api_key", "google_tts", "Google Cloud Text-to-Speech API key", "https://console.cloud.google.com/apis/library/texttospeech.googleapis.com"],
  ["runway_api_key", "runway_video", "Runway API key", "https://dev.runwayml.com/"],
];

function PaidKeys({ mode, toast }: { mode: string; toast: (m: string, bad?: boolean) => void }) {
  const { data, reload } = useApi<any>("/setup");
  const [vals, setVals] = useState<Record<string, string>>({});
  if (!data) return null;
  const save = async (k: string, test: string) => {
    try {
      await api("/setup/keys", { method: "PUT", body: { [k]: (vals[k] || "").trim() } });
      setVals({ ...vals, [k]: "" });
      const r = await api("/setup/test", { method: "POST", body: { key: test } });
      toast(r.ok ? "Key works" : r.result, !r.ok); reload();
    } catch (e: any) { toast(e.message, true); }
  };
  return (
    <Panel title="Paid API keys (optional)">
      <p className="mb-3 text-[14px] text-[var(--muted)]">Stored in Windows Credential Manager. {mode === "FREE_ONLY" ? "In FREE ONLY mode keys are kept but never used or tested (testing a key costs money)." : "A key is tested with one tiny paid request."}</p>
      <ul className="space-y-3">
        {KEYS.map(([k, test, label, url]) => (
          <li key={k} className="flex flex-wrap items-center gap-2">
            <span className="min-w-[260px] flex-1 text-[14px]">{label} {data.keys[k]?.set && <Pill tone="good">saved</Pill>}
              <button className="ml-2 text-[13px] underline" onClick={() => import("@/lib/engine").then((m) => m.openExternal(url))}>get a key</button></span>
            <input className={`${inputCls} max-w-[320px]`} type="password" autoComplete="off" value={vals[k] || ""} onChange={(e) => setVals({ ...vals, [k]: e.target.value })}
              placeholder={data.keys[k]?.set ? `Saved (${data.keys[k].masked})` : "Paste key"} aria-label={label} />
            <Button disabled={!(vals[k] || "").trim()} onClick={() => save(k, test)}>Save</Button>
          </li>))}
      </ul>
    </Panel>
  );
}
