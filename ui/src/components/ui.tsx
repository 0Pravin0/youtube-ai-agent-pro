"use client";
import { ReactNode, useState } from "react";

export function PageTitle({ title, sub, actions }: { title: string; sub?: string; actions?: ReactNode }) {
  return (
    <div className="mb-6 flex flex-wrap items-end justify-between gap-4">
      <div>
        <h1 className="text-[28px] leading-tight font-semibold tracking-[-0.01em]">{title}</h1>
        {sub && <p className="mt-1 max-w-[70ch] text-[var(--muted)]">{sub}</p>}
      </div>
      {actions && <div className="flex flex-wrap gap-2">{actions}</div>}
    </div>
  );
}

export function Panel({ title, children, aside, className = "", flush = false }:
  { title?: ReactNode; children: ReactNode; aside?: ReactNode; className?: string; flush?: boolean }) {
  return (
    <section className={`rounded-xl border border-[var(--line)] bg-[var(--panel)] ${className}`}>
      {title && (
        <header className="flex items-center justify-between gap-3 px-5 pt-4 pb-3">
          <h2 className="text-[17px] font-semibold">{title}</h2>
          {aside}
        </header>
      )}
      <div className={flush ? "" : "px-5 pb-5 " + (title ? "" : "pt-5")}>{children}</div>
    </section>
  );
}

type Tone = "neutral" | "good" | "warn" | "bad" | "accent";
const TONES: Record<Tone, string> = {
  neutral: "bg-[var(--panel-2)] text-[var(--muted)]",
  good: "bg-[color-mix(in_oklab,var(--teal)_20%,transparent)] text-[var(--teal)]",
  warn: "bg-[color-mix(in_oklab,var(--marigold)_20%,transparent)] text-[var(--marigold)]",
  bad: "bg-[color-mix(in_oklab,var(--kumkum)_20%,transparent)] text-[var(--kumkum)]",
  accent: "bg-[color-mix(in_oklab,var(--violet)_22%,transparent)] text-[var(--violet)]",
};

export function Pill({ tone = "neutral", children }: { tone?: Tone; children: ReactNode }) {
  return <span className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-[13px] font-medium whitespace-nowrap ${TONES[tone]}`}>{children}</span>;
}

export function stageTone(stage: string): Tone {
  if (["FAILED", "BLOCKED_BY_PROVIDER"].includes(stage)) return "bad";
  if (["PAUSED", "RETRYING", "READY_TO_UPLOAD"].includes(stage)) return "warn";
  if (["PUBLISHED", "ANALYTICS_MONITORING", "COMPLETED", "SCHEDULED"].includes(stage)) return "good";
  if (stage === "CANCELLED") return "neutral";
  return "accent";
}

export function Button({ children, onClick, kind = "secondary", disabled, type = "button", title }:
  { children: ReactNode; onClick?: () => void | Promise<void>; kind?: "primary" | "secondary" | "danger" | "ghost"; disabled?: boolean; type?: "button" | "submit"; title?: string }) {
  const [busy, setBusy] = useState(false);
  const styles = {
    primary: "bg-[var(--marigold)] text-[var(--marigold-ink)] hover:brightness-110",
    secondary: "bg-[var(--panel-2)] text-[var(--text)] border border-[var(--line)] hover:border-[var(--muted)]",
    danger: "bg-transparent text-[var(--kumkum)] border border-[color-mix(in_oklab,var(--kumkum)_50%,transparent)] hover:bg-[color-mix(in_oklab,var(--kumkum)_12%,transparent)]",
    ghost: "bg-transparent text-[var(--muted)] hover:text-[var(--text)]",
  }[kind];
  return (
    <button type={type} title={title} disabled={disabled || busy}
      onClick={onClick ? async () => { setBusy(true); try { await onClick(); } finally { setBusy(false); } } : undefined}
      className={`rounded-lg px-3.5 py-2 text-[14px] font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${styles}`}>
      {busy ? "Working…" : children}
    </button>
  );
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="rounded-xl border border-dashed border-[var(--line)] px-6 py-10 text-center">
      <p className="font-medium">{title}</p>
      {children && <div className="mx-auto mt-1 max-w-[60ch] text-[var(--muted)]">{children}</div>}
    </div>
  );
}

export function ErrorNote({ error }: { error: string | null }) {
  if (!error) return null;
  return <p role="alert" className="mb-4 rounded-lg border border-[var(--kumkum)] px-4 py-3 text-[var(--kumkum)]">{error}</p>;
}

export function Stat({ label, value, tone }: { label: string; value: ReactNode; tone?: Tone }) {
  return (
    <div className="min-w-0">
      <div className="text-[13px] text-[var(--muted)]">{label}</div>
      <div className={`mt-0.5 text-[22px] font-semibold ${tone === "bad" ? "text-[var(--kumkum)]" : tone === "good" ? "text-[var(--teal)]" : ""}`}>{value}</div>
    </div>
  );
}

export function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <label className="block">
      <span className="text-[14px] font-medium">{label}</span>
      {hint && <span className="block text-[13px] text-[var(--muted)]">{hint}</span>}
      <div className="mt-1.5">{children}</div>
    </label>
  );
}

export const inputCls = "w-full rounded-lg border border-[var(--line)] bg-[var(--bg)] px-3 py-2 text-[15px] focus:border-[var(--marigold)] focus:outline-none";

export function useToast() {
  const [msg, setMsg] = useState<{ text: string; bad?: boolean } | null>(null);
  const show = (text: string, bad = false) => { setMsg({ text, bad }); setTimeout(() => setMsg(null), 4500); };
  const node = msg ? (
    <div role="status" className={`fixed right-5 bottom-5 z-50 max-w-[420px] rounded-lg border px-4 py-3 shadow-lg ${msg.bad ? "border-[var(--kumkum)] bg-[var(--panel)] text-[var(--kumkum)]" : "border-[var(--teal)] bg-[var(--panel)]"}`}>{msg.text}</div>
  ) : null;
  return { show, node };
}

export function Toggle({ on, onChange, label }: { on: boolean; onChange: (v: boolean) => void; label: string }) {
  return (
    <button type="button" role="switch" aria-checked={on} aria-label={label} onClick={() => onChange(!on)}
      className={`relative h-8 w-14 shrink-0 rounded-full transition-colors ${on ? "bg-[var(--marigold)]" : "bg-[var(--line)]"}`}>
      <span className={`absolute top-1 h-6 w-6 rounded-full bg-white shadow transition-all ${on ? "left-7" : "left-1"}`} />
    </button>
  );
}
