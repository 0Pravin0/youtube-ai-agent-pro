"use client";
import { useEffect, useState } from "react";
import Link from "next/link";
import { MusicLibrary } from "@/components/MusicLibrary";
import { useShell } from "@/components/Shell";
import { Button, ErrorNote, Field, PageTitle, Panel, Pill, Toggle, inputCls } from "@/components/ui";
import { api, ist, useApi } from "@/lib/api";
import { syncTray } from "@/lib/engine";

export default function Settings() {
  const { data, error, reload } = useApi<any>("/settings");
  const { toast, reload: reloadShell } = useShell();
  const [f, setF] = useState<any>(null);
  useEffect(() => { if (data) setF(data); }, [data]);
  if (!f) return <ErrorNote error={error} />;
  const set = (k: string, v: any) => setF({ ...f, [k]: v });
  const num = (k: string) => (e: any) => set(k, e.target.value === "" ? "" : Number(e.target.value));
  const save = async (patch?: any) => {
    const keys = ["mode", "timezone", "daily_budget_usd", "monthly_budget_usd", "per_video_ceiling_short_usd", "per_video_ceiling_long_usd",
      "max_concurrent_jobs", "default_privacy", "contains_synthetic_media", "prefer_generated_video", "max_generated_video_seconds_per_video",
      "long_form_minutes", "youtube_daily_quota_units", "quality_min_score", "missed_slot_policy"];
    const body = patch || Object.fromEntries(keys.map((k) => [k, f[k]]));
    try { await api("/settings", { method: "PUT", body }); toast("Settings saved"); reload(); reloadShell(); }
    catch (e: any) { toast(e.message, true); }
  };
  const chk = (k: string, label: string, hint?: string) => (
    <label className="flex items-start gap-3"><input type="checkbox" className="mt-1 h-4 w-4 accent-[var(--marigold)]" checked={!!f[k]} onChange={(e) => set(k, e.target.checked)} />
      <span><span className="font-medium">{label}</span>{hint && <span className="block text-[13px] text-[var(--muted)]">{hint}</span>}</span></label>
  );

  return (
    <>
      <PageTitle title="Automation Settings" sub="Saved on this computer. Automation keeps running while the app is open or in the system tray." actions={<Button kind="primary" onClick={() => save()}>Save settings</Button>} />
      <div className="grid gap-6 lg:grid-cols-2">
        <Panel title="Publishing authorization">
          <p className="mb-4 max-w-[60ch] text-[14px] text-[var(--muted)]">In full-auto mode, videos that pass every quality and policy check are uploaded without asking you each time. This needs your one-time authorization, which you can withdraw at any moment.</p>
          {f.publishing_authorized ? (
            <div className="flex flex-wrap items-center justify-between gap-3">
              <span>Authorized {ist(f.publishing_authorized_at)}</span>
              <Button kind="danger" onClick={() => save({ publishing_authorized: false })}>Withdraw authorization</Button>
            </div>
          ) : <Button kind="primary" onClick={() => save({ publishing_authorized: true })}>Authorize autonomous publishing</Button>}
          <div className="mt-5 grid gap-4 sm:grid-cols-2">
            <Field label="Automation mode">
              <select className={inputCls} value={f.mode} onChange={(e) => set("mode", e.target.value)}>
                <option value="FULL_AUTO">Full auto</option><option value="APPROVAL_REQUIRED">Ask me before each upload</option><option value="MANUAL">Manual</option>
              </select>
            </Field>
            <Field label="Upload visibility" hint="Change it in Public publishing below">
              <input className={inputCls} value={f.default_privacy} readOnly />
            </Field>
          </div>
          <div className="mt-4 space-y-3">
            {chk("contains_synthetic_media", "Declare altered or synthetic content", "Sets YouTube’s status.containsSyntheticMedia on every upload")}
          </div>
        </Panel>

        <PublicPublishing toast={toast} />

        <Panel title="Paid API spending">
          {(f.extra?.production_mode || "FREE_ONLY") === "FREE_ONLY" ? (
            <p className="text-[14px]"><b>FREE MODE ACTIVE</b> — the paid budget is ₹0 and no paid service can be called.
              To use paid services later, go to <Link className="underline" href="/premium/">Future premium upgrade</Link>.</p>
          ) : (
            <>
              <p className="mb-3 text-[14px] text-[var(--muted)]">Daily and monthly limits are set in rupees on <Link className="underline" href="/premium/">Future premium upgrade</Link>. Per-video ceilings (US$, as providers bill):</p>
              <div className="grid gap-4 sm:grid-cols-2">
                <Field label="Per Short ceiling (USD)"><input type="number" min={0} step="0.5" className={inputCls} value={f.per_video_ceiling_short_usd} onChange={num("per_video_ceiling_short_usd")} /></Field>
                <Field label="Per long-form ceiling (USD)"><input type="number" min={0} step="1" className={inputCls} value={f.per_video_ceiling_long_usd} onChange={num("per_video_ceiling_long_usd")} /></Field>
              </div>
            </>
          )}
        </Panel>

        <Panel title="Production">
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="Videos produced at once"><input type="number" min={1} max={20} className={inputCls} value={f.max_concurrent_jobs} onChange={num("max_concurrent_jobs")} /></Field>
            <Field label="Long-form length (minutes)"><input type="number" min={5} max={15} step="0.5" className={inputCls} value={f.long_form_minutes} onChange={num("long_form_minutes")} /></Field>
            <Field label="Minimum quality score" hint="Critical checks must pass regardless"><input type="number" min={50} max={100} className={inputCls} value={f.quality_min_score} onChange={num("quality_min_score")} /></Field>
          </div>
          <p className="mt-4 text-[13px] text-[var(--muted)]">Animation style and local models: <Link className="underline" href="/tools/">Tools and models</Link>. On low-spec computers keep “videos produced at once” at 1.</p>
        </Panel>

        <Panel title="Schedule and limits">
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="Timezone"><input className={inputCls} value={f.timezone} onChange={(e) => set("timezone", e.target.value)} /></Field>
            <Field label="When a slot is missed">
              <select className={inputCls} value={f.missed_slot_policy} onChange={(e) => set("missed_slot_policy", e.target.value)}>
                <option value="next_free_slot">Move to the next free slot</option><option value="publish_asap">Publish when ready</option><option value="skip">Skip the slot</option>
              </select>
            </Field>
            <Field label="YouTube daily quota (units)" hint="Default project quota is 10,000"><input type="number" min={100} className={inputCls} value={f.youtube_daily_quota_units} onChange={num("youtube_daily_quota_units")} /></Field>
          </div>
          <p className="mt-4 text-[14px]"><Link className="underline" href="/calendar/">Change the publishing schedule</Link></p>
        </Panel>

        <Panel title="Running in the background">
          <p className="text-[14px]">Closing the window keeps approved jobs running; use the menu bar icon to show the dashboard, pause, resume or stop the agent, or quit the app.</p>
          <p className="mt-2 text-[13px] text-[var(--muted)]">Nothing runs while the Mac is asleep or shut down. Performance mode and keep-awake are in System Health.</p>
        </Panel>

        <Panel title="Music and sound effects library" className="lg:col-span-2"><MusicLibrary toast={toast} /></Panel>

        <Panel title="Setup" className="lg:col-span-2">
          <p className="text-[14px] text-[var(--muted)]">Reconnect YouTube, re-check this computer, or change video preferences.</p>
          <div className="mt-3 flex flex-wrap gap-2">
            {[["/setup/", "Open the setup wizard"], ["/premium/", "Premium providers"], ["/connections/", "API connections"], ["/ideas/", "Content ideas"], ["/activity/", "Activity logs"], ["/costs/", "Usage and costs"]].map(([h, l]) => (
              <Link key={h} href={h} className="inline-block rounded-lg border border-[var(--line)] px-3.5 py-2 text-[14px]">{l}</Link>))}
          </div>
        </Panel>
      </div>
    </>
  );
}


function PublicPublishing({ toast }: { toast: (m: string, bad?: boolean) => void }) {
  const { data, reload } = useApi<any>("/publishing/public");
  const [privacy, setPrivacy] = useState("public");
  if (!data) return null;
  const go = async (authorize: boolean) => {
    try { await api("/publishing/public", { method: "POST", body: { authorize, default_privacy: privacy } }); toast(authorize ? "Public publishing authorized" : "Uploads will stay private"); reload(); }
    catch (e: any) { toast(e.message, true); }
  };
  const tested = data.allowed || data.reason.includes("not authorized");
  return (
    <Panel title="Public publishing">
      <p className="mb-3 max-w-[60ch] text-[14px] text-[var(--muted)]">Every upload stays <b>private</b> until one private test upload has succeeded on your channel and you authorize public publishing here.</p>
      <ol className="mb-4 space-y-1 text-[14px]">
        <li>{tested ? "✓" : "○"} A private test upload succeeded</li>
        <li>{data.authorized ? "✓" : "○"} You authorized public publishing</li>
      </ol>
      {data.authorized ? (
        <div className="flex flex-wrap items-center gap-3"><Pill tone="good">Uploads: {data.default_privacy}</Pill><Button kind="danger" onClick={() => go(false)}>Go back to private uploads</Button></div>
      ) : (
        <div className="flex flex-wrap items-end gap-3">
          <Field label="Visibility after authorization"><select className={inputCls} value={privacy} onChange={(e) => setPrivacy(e.target.value)}>
            <option value="public">Public</option><option value="unlisted">Unlisted</option></select></Field>
          <Button kind="primary" disabled={!tested} onClick={() => go(true)}>Authorize public publishing</Button>
        </div>
      )}
      {!tested && <p className="mt-2 text-[13px] text-[var(--muted)]">{data.reason}</p>}
    </Panel>
  );
}
