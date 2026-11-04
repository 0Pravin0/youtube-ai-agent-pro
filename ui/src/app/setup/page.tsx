"use client";
import { useRouter } from "next/navigation";
import { ReactNode, useEffect, useState } from "react";
import { ComputerCheck, HfToken, ToolsList, VoiceSample } from "@/components/FreeTools";
import { useShell } from "@/components/Shell";
import { Button, Field, Pill, Toggle, inputCls } from "@/components/ui";
import { api, useApi } from "@/lib/api";
import { openExternal } from "@/lib/engine";

const STEPS = ["Connect YouTube", "Set up this Mac", "Video preferences", "Automation"];

export default function Setup() {
  const { data: s, reload } = useApi<any>("/setup");
  const { lastEvent, toast, reload: reloadShell } = useShell();
  const [step, setStep] = useState(0);
  const router = useRouter();
  useEffect(() => { if (lastEvent?.type === "channel" || lastEvent?.type === "project") reload(); }, [lastEvent]); // eslint-disable-line
  if (!s) return null;
  const go = (n: number) => { setStep(n); window.scrollTo(0, 0); };

  return (
    <main className="mx-auto max-w-[860px] px-6 py-10">
      <div className="mb-8 flex items-center gap-3">
        <img src="/icon.png" alt="" className="h-11 w-11 rounded-xl" />
        <div>
          <h1 className="text-[26px] leading-tight font-semibold">Welcome to YouTube AI Agent Pro</h1>
          <p className="text-[var(--muted)]">Original Tamil animated stories for your channel, made with free tools on this Mac.</p>
        </div>
      </div>
      <ol className="mb-8 grid grid-cols-4 gap-2" aria-label="Setup progress">
        {STEPS.map((t, i) => (
          <li key={t}>
            <button onClick={() => go(i)} aria-current={i === step ? "step" : undefined}
              className={`w-full rounded-lg border px-3 py-2 text-left text-[14px] ${i === step ? "border-[var(--marigold)] bg-[var(--panel)]" : "border-[var(--line)] text-[var(--muted)]"}`}>
              <span className="block text-[12px]">Step {i + 1}</span>{t}
              {Object.values(s.steps)[i] ? <span className="ml-1 text-[var(--teal)]">✓</span> : null}
            </button>
          </li>
        ))}
      </ol>
      {step === 0 && <YouTubeStep s={s} reload={reload} toast={toast} next={() => go(1)} />}
      {step === 1 && <FreeToolsStep s={s} reload={reload} toast={toast} next={() => go(2)} back={() => go(0)} />}
      {step === 2 && <PreferencesStep s={s} reload={reload} toast={toast} next={() => go(3)} back={() => go(1)} />}
      {step === 3 && <AutomationStep s={s} toast={toast} back={() => go(2)} done={async () => { await reloadShell(); router.replace("/"); }} />}
    </main>
  );
}

function Card({ title, children, footer }: { title: string; children: ReactNode; footer?: ReactNode }) {
  return (
    <section className="rounded-2xl border border-[var(--line)] bg-[var(--panel)] p-6">
      <h2 className="mb-4 text-[20px] font-semibold">{title}</h2>
      <div className="space-y-5">{children}</div>
      {footer && <div className="mt-6 flex flex-wrap items-center justify-between gap-3 border-t border-[var(--line)] pt-5">{footer}</div>}
    </section>
  );
}

function Help({ children }: { children: ReactNode }) {
  return <details className="rounded-lg bg-[var(--panel-2)] px-4 py-3 text-[14px]"><summary className="cursor-pointer font-medium">How do I get this?</summary><div className="mt-2 space-y-1.5 text-[var(--muted)]">{children}</div></details>;
}

function Ext({ href, children }: { href: string; children: ReactNode }) {
  return <button type="button" className="text-[var(--text)] underline" onClick={() => openExternal(href)}>{children}</button>;
}

/* ---------------- Step 1: email + Google ---------------- */
function YouTubeStep({ s, reload, toast, next }: any) {
  const [email, setEmail] = useState(s.channel.email_hint || "");
  const [cid, setCid] = useState("");
  const [csec, setCsec] = useState("");
  const [waiting, setWaiting] = useState(false);
  const [choices, setChoices] = useState<any[]>([]);
  const hasClient = s.keys.google_client_id.set && s.keys.google_client_secret.set;
  const valid = /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email.trim());
  useEffect(() => {
    if (!waiting) return;
    const t = setInterval(async () => {
      reload();
      try { const c = await api("/channels/choices"); if (c.choices.length > 1) setChoices(c.choices); } catch {}
    }, 3000);
    return () => clearInterval(t);
  }, [waiting, reload]);
  useEffect(() => { if (s.channel.connected) setWaiting(false); }, [s.channel.connected]);

  const saveClient = async () => {
    try {
      await api("/setup/keys", { method: "PUT", body: { google_client_id: cid.trim(), google_client_secret: csec.trim() } });
      setCid(""); setCsec(""); toast("Saved in the macOS Keychain"); reload();
    } catch (e: any) { toast(e.message, true); }
  };
  const connect = async () => {
    try {
      const r = await api("/channels/connect", { method: "POST", body: { email: email.trim() } });
      setWaiting(true);
      await openExternal(r.authorization_url);
    } catch (e: any) { toast(e.message, true); }
  };
  const choose = async (id: string) => {
    try { await api("/channels/select", { method: "POST", body: { youtube_channel_id: id } }); setChoices([]); reload(); }
    catch (e: any) { toast(e.message, true); }
  };
  const p = s.channel.profile || {};
  return (
    <Card title="Connect your YouTube channel"
      footer={<><span className="text-[14px] text-[var(--muted)]">{s.channel.connected ? "Channel verified with YouTube." : "Uploading needs a connected channel."}</span>
        <div className="flex gap-2">{!s.channel.connected && <Button kind="ghost" onClick={next}>Set up later</Button>}<Button kind="primary" disabled={!s.channel.connected} onClick={next}>Continue</Button></div></>}>
      {s.channel.connected ? (
        <div className="flex items-center gap-4 rounded-xl border border-[var(--teal)] p-4">
          {p.thumbnail && <img src={p.thumbnail} alt="" className="h-14 w-14 rounded-full" referrerPolicy="no-referrer" />}
          <div>
            <div className="text-[18px] font-semibold">{p.title || s.channel.name}</div>
            <div className="text-[14px] text-[var(--muted)]">{s.channel.youtube_channel_id}
              {p.subscriber_count != null && ` · ${p.subscriber_count.toLocaleString("en-IN")} subscribers`}
              {p.video_count != null && ` · ${p.video_count.toLocaleString("en-IN")} videos`}</div>
          </div>
          <Pill tone="good">Connected</Pill>
        </div>
      ) : (
        <>
          <Field label="Your YouTube channel's email address" hint="Used only to pre-select that account on Google's sign-in page. Typing an email doesn't give the app any access.">
            <input className={inputCls} type="email" autoComplete="email" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="you@gmail.com" />
          </Field>
          {!hasClient ? (
            <div className="space-y-3 rounded-xl border border-[var(--line)] p-4">
              <div className="font-medium">One-time: your own Google sign-in ID</div>
              <p className="text-[14px] text-[var(--muted)]">Google requires every app that uploads videos to have its own sign-in ID. Because this app runs on your Mac and is not a Google-verified service, it uses one you create for free in your Google account (about 5 minutes, once).</p>
              <Help>
                <p>1. Open <Ext href="https://console.cloud.google.com/projectcreate">Google Cloud Console</Ext> and create a project (any name).</p>
                <p>2. Enable <Ext href="https://console.cloud.google.com/apis/library/youtube.googleapis.com">YouTube Data API v3</Ext> and <Ext href="https://console.cloud.google.com/apis/library/youtubeanalytics.googleapis.com">YouTube Analytics API</Ext>.</p>
                <p>3. Open <Ext href="https://console.cloud.google.com/auth/audience">Google Auth Platform → Audience</Ext>, choose <b>External</b>, and under <b>Test users</b> add the email above.</p>
                <p>4. <Ext href="https://console.cloud.google.com/auth/clients">Clients → Create client</Ext>, type <b>Desktop app</b>, then copy the Client ID and Client secret here.</p>
              </Help>
              <div className="grid gap-4 sm:grid-cols-2">
                <Field label="Client ID"><input className={inputCls} value={cid} onChange={(e) => setCid(e.target.value)} placeholder="…apps.googleusercontent.com" /></Field>
                <Field label="Client secret"><input className={inputCls} type="password" value={csec} onChange={(e) => setCsec(e.target.value)} placeholder="GOCSPX-…" /></Field>
              </div>
              <Button disabled={!cid.trim() || !csec.trim()} onClick={saveClient}>Save</Button>
            </div>
          ) : (
            <div className="space-y-3">
              <Button kind="primary" disabled={!valid} onClick={connect}>Continue with Google</Button>
              {waiting && <p className="text-[var(--muted)]">Finish on Google's page in your browser (you sign in there — this app never sees your password), then come back. This screen updates by itself.</p>}
              {s.channel.error && <p role="alert" className="text-[var(--kumkum)]">{s.channel.error}</p>}
              {s.channel.error?.includes("access_denied") || s.channel.error?.includes("verification") ? <p className="text-[14px]">If Google said "Access blocked", add {email || "your email"} under Test users (step 3 above) and try again.</p> : null}
              <button className="block text-[14px] text-[var(--muted)] underline" onClick={async () => { await api("/setup/keys", { method: "PUT", body: { google_client_id: "", google_client_secret: "" } }); reload(); }}>Use a different Google sign-in ID</button>
            </div>
          )}
          {choices.length > 1 && (
            <div className="rounded-xl border border-[var(--marigold)] p-4">
              <div className="mb-2 font-medium">This Google account manages several channels. Which one should the agent run?</div>
              <ul className="space-y-2">{choices.map((c) => (
                <li key={c.id}><button onClick={() => choose(c.id)} className="flex w-full items-center gap-3 rounded-lg border border-[var(--line)] p-3 text-left hover:border-[var(--marigold)]">
                  {c.thumbnail && <img src={c.thumbnail} alt="" className="h-9 w-9 rounded-full" referrerPolicy="no-referrer" />}
                  <span className="flex-1"><b>{c.title}</b><span className="block text-[13px] text-[var(--muted)]">{c.id}{c.subscriber_count != null ? ` · ${c.subscriber_count} subscribers` : ""}</span></span></button></li>))}</ul>
            </div>
          )}
        </>
      )}
    </Card>
  );
}

/* ---------------- Step 2 ---------------- */
const STYLES = ["Colourful 2D storybook animation", "3D Pixar-like cartoon", "Anime style", "Watercolour illustration", "Paper-cut art", "Realistic cinematic"];

function FreeToolsStep({ s, reload, toast, next, back }: any) {
  useEffect(() => { const t = setInterval(reload, 5000); return () => clearInterval(t); }, [reload]);
  return (
    <Card title="Set up this Mac"
      footer={<><Button kind="ghost" onClick={back}>Back</Button><div className="flex gap-2">{!s.steps.providers && <Button kind="ghost" onClick={next}>Finish this later</Button>}<Button kind="primary" disabled={!s.steps.providers} onClick={next}>Continue</Button></div></>}>
      <ComputerCheck compact />
      <p className="max-w-[68ch] text-[14px] text-[var(--muted)]">Install the <b>recommended</b> items below (each shows its size and licence and waits for your approval). Downloads continue in the background — you can carry on with the next steps and come back.</p>
      <ToolsList toast={toast} onlyRecommended />
      <HfToken toast={toast} />
      <VoiceSample toast={toast} />
    </Card>
  );
}

/* ---------------- Step 3 ---------------- */
function PreferencesStep({ s, reload, toast, next, back }: any) {
  const [p, setP] = useState<any>(s.preferences);
  const set = (k: string, v: any) => setP({ ...p, [k]: v });
  const toggleFormat = (f: string) => set("formats", p.formats.includes(f) ? p.formats.filter((x: string) => x !== f) : [...p.formats, f]);
  const save = async () => {
    try {
      const { daily_budget_usd, monthly_budget_usd, ...rest } = p;
      await api("/setup/preferences", { method: "PUT", body: rest });
      toast("Content settings saved"); reload(); next();
    } catch (e: any) { toast(e.message, true); }
  };
  return (
    <Card title="Video preferences"
      footer={<><Button kind="ghost" onClick={back}>Back</Button><Button kind="primary" disabled={!p.formats.length} onClick={save}>Save and continue</Button></>}>
      <div className="grid gap-5 sm:grid-cols-2">
        <Field label="Channel language">
          <select className={inputCls} value={p.language} onChange={(e) => set("language", e.target.value)}>
            <option value="ta">Tamil (தமிழ்)</option><option value="en">English</option>
          </select>
        </Field>
        <Field label="Content category" hint="Sets the YouTube category of every upload">
          <select className={inputCls} value={p.content_category} onChange={(e) => set("content_category", e.target.value)}>
            <option value="1">Film &amp; Animation</option><option value="24">Entertainment</option><option value="27">Education</option>
            <option value="22">People &amp; Blogs</option><option value="23">Comedy</option><option value="19">Travel &amp; Events</option><option value="26">Howto &amp; Style</option>
          </select>
        </Field>
        <Field label="Topic of the channel"><input className={inputCls} value={p.niche} onChange={(e) => set("niche", e.target.value)} placeholder="e.g. Animated fantasy and adventure stories" /></Field>
        <Field label="Target audience"><input className={inputCls} value={p.audience} onChange={(e) => set("audience", e.target.value)} placeholder="e.g. Tamil-speaking families" /></Field>
        <Field label="Animation style">
          <select className={inputCls} value={STYLES.includes(p.video_style) ? p.video_style : "custom"} onChange={(e) => set("video_style", e.target.value === "custom" ? "" : e.target.value)}>
            {STYLES.map((x) => <option key={x} value={x}>{x}</option>)}<option value="custom">Other (describe)…</option>
          </select>
          {!STYLES.includes(p.video_style) && <input className={`${inputCls} mt-2`} value={p.video_style} onChange={(e) => set("video_style", e.target.value)} placeholder="Describe the look" />}
        </Field>
      </div>
      <fieldset>
        <legend className="text-[14px] font-medium">Formats</legend>
        <div className="mt-2 flex flex-wrap gap-3">
          {[["SHORT", "Shorts (vertical, under a minute)"], ["LONG", "Long videos (landscape)"]].map(([f, l]) => (
            <label key={f} className="flex items-center gap-2 rounded-lg border border-[var(--line)] px-3 py-2">
              <input type="checkbox" className="h-4 w-4 accent-[var(--marigold)]" checked={p.formats.includes(f)} onChange={() => toggleFormat(f)} />{l}
            </label>))}
        </div>
      </fieldset>
      <div className="grid gap-5 sm:grid-cols-3">
        {p.formats.includes("SHORT") && <Field label="Short length (seconds)"><input type="number" min={30} max={58} className={inputCls} value={p.short_seconds} onChange={(e) => set("short_seconds", Number(e.target.value))} /></Field>}
        {p.formats.includes("SHORT") && <Field label="Shorts per day"><input type="number" min={0} max={4} className={inputCls} value={p.shorts_per_day} onChange={(e) => set("shorts_per_day", Number(e.target.value))} /></Field>}
        {p.formats.includes("LONG") && <Field label="Long video length (minutes)"><input type="number" min={5} max={15} step={0.5} className={inputCls} value={p.long_form_minutes} onChange={(e) => set("long_form_minutes", Number(e.target.value))} /></Field>}
        {p.formats.includes("LONG") && <Field label="Long videos per week"><input type="number" min={0} max={7} className={inputCls} value={p.longs_per_week} onChange={(e) => set("longs_per_week", Number(e.target.value))} /></Field>}
        <Field label="Publishing time"><input type="time" className={inputCls} value={p.publish_time} onChange={(e) => set("publish_time", e.target.value)} /></Field>
        <Field label="Time zone"><input className={inputCls} value={p.timezone} onChange={(e) => set("timezone", e.target.value)} /></Field>
      </div>
      <p className="rounded-lg bg-[var(--panel-2)] px-4 py-3 text-[14px]"><b>Paid AI budget: ₹0.</b> Free mode uses only local tools. You can enable paid services later in Future Premium Upgrade, with your own rupee limits.</p>
      <label className="flex items-start gap-3"><input type="checkbox" className="mt-1 h-4 w-4 accent-[var(--marigold)]" checked={p.made_for_kids} onChange={(e) => set("made_for_kids", e.target.checked)} />
        <span><span className="font-medium">These videos are made for kids</span><span className="block text-[13px] text-[var(--muted)]">YouTube legally requires this answer (COPPA). It turns off comments and personalised ads on uploads.</span></span></label>
      <p className="text-[13px] text-[var(--muted)]">On a computer without a graphics card, a Short takes roughly 20–60 minutes to make and a 6-minute video several hours; the schedule should leave time for that.</p>
    </Card>
  );
}

/* ---------------- Step 4 ---------------- */
function AutomationStep({ s, toast, back, done }: any) {
  const [fullAuto, setFullAuto] = useState(true);
  const [authorize, setAuthorize] = useState(false);
  const [start, setStart] = useState(true);
  const [first, setFirst] = useState("SHORT");
  const finish = async () => {
    try {
      const r = await api("/setup/complete", { method: "POST", body: { full_auto: fullAuto, authorize_publishing: fullAuto && authorize, default_privacy: "private", start_now: start, create_first_video: first } });
      toast(r.first_video ? "Your first original Tamil video is being made" : start ? "Automation started" : "Setup complete");
      await done();
    } catch (e: any) { toast(e.message, true); }
  };
  return (
    <Card title="Automation setup"
      footer={<><Button kind="ghost" onClick={back}>Back</Button><Button kind="primary" disabled={fullAuto && !authorize} onClick={finish}>Finish setup</Button></>}>
      <div className="flex items-center justify-between gap-4 rounded-xl border border-[var(--line)] p-4">
        <div>
          <div className="text-[18px] font-semibold">ENABLE AUTONOMOUS PUBLISHING</div>
          <div className="text-[14px] text-[var(--muted)]">{fullAuto ? "On: after this authorization, videos that pass every quality, originality and policy check are uploaded on schedule without asking you each time (daily limits, duplicate protection and emergency stop apply)." : "Off: the AI makes the videos and you approve each one before it goes to YouTube."}</div>
        </div>
        <Toggle on={fullAuto} onChange={setFullAuto} label="Full auto mode" />
      </div>
      {fullAuto && (
        <label className="flex items-start gap-3 rounded-xl bg-[var(--panel-2)] p-4">
          <input type="checkbox" className="mt-1 h-4 w-4 accent-[var(--marigold)]" checked={authorize} onChange={(e) => setAuthorize(e.target.checked)} />
          <span>I authorize YouTube AI Agent Pro to publish videos to <b>{s.channel.profile?.title || s.channel.name}</b> automatically. I can pause or withdraw this at any time in Settings.</span>
        </label>
      )}
      <div className="grid gap-5 sm:grid-cols-2">
        <Field label="Make the first video now" hint="Starts right after setup">
          <select className={inputCls} value={first} onChange={(e) => setFirst(e.target.value)}>
            <option value="SHORT">Yes — an original Tamil Short</option><option value="LONG">Yes — an original long video</option><option value="none">No, just follow the schedule</option>
          </select>
        </Field>
        <label className="flex items-end gap-3 pb-2"><input type="checkbox" className="h-4 w-4 accent-[var(--marigold)]" checked={start} onChange={(e) => setStart(e.target.checked)} />Start automation now</label>
      </div>
      <p className="rounded-lg bg-[var(--panel-2)] px-4 py-3 text-[14px]">Uploads start as <b>Private</b>. After the first private test upload works, you can authorize public publishing in Settings → Public publishing.</p>
      <ul className="list-disc space-y-1 pl-5 text-[14px] text-[var(--muted)]">
        <li>Every upload is marked as containing AI-generated (synthetic) content, as YouTube requires.</li>
        <li>Work runs on this computer while the app is open; closing the window keeps it running in the system tray. Nothing runs while the computer is off.</li>
      </ul>
    </Card>
  );
}
