"use client";
import Link from "next/link";
import { useEffect, useState } from "react";
import { useShell } from "@/components/Shell";
import { Button, ErrorNote, Field, PageTitle, Panel, Pill, inputCls, stageTone } from "@/components/ui";
import { STAGE_LABEL, api, useApi } from "@/lib/api";

const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

export default function Calendar() {
  const { data, error, reload } = useApi<any>("/schedule");
  const { toast } = useShell();
  const [rules, setRules] = useState<any[]>([]);
  const [policy, setPolicy] = useState("next_free_slot");
  useEffect(() => { if (data) { setRules(data.rules); setPolicy(data.missed_slot_policy); } }, [data]);
  if (!data) return <ErrorNote error={error} />;
  const tz = data.timezone;
  const byDay: Record<string, any[]> = {};
  for (const s of data.slots) {
    const k = new Date(s.slot_at).toLocaleDateString("en-CA", { timeZone: tz });
    (byDay[k] ||= []).push(s);
  }
  const save = async () => {
    try { await api("/schedule", { method: "PUT", body: { rules, missed_slot_policy: policy } }); toast("Schedule saved"); reload(); }
    catch (e: any) { toast(e.message, true); }
  };
  const upd = (i: number, patch: any) => setRules(rules.map((r, j) => (j === i ? { ...r, ...patch } : r)));

  return (
    <>
      <PageTitle title="Publishing Calendar" sub={`The next seven days of slots, in ${tz}. Production starts automatically ahead of each slot (8 h for Shorts, 36 h for long-form).`} />
      <div className="mb-8 grid gap-3 md:grid-cols-4 xl:grid-cols-7">
        {Object.entries(byDay).map(([day, slots]) => (
          <div key={day} className="rounded-xl border border-[var(--line)] p-3">
            <div className="mb-2 font-medium">{new Date(day + "T12:00:00").toLocaleDateString("en-IN", { weekday: "short", day: "numeric", month: "short" })}</div>
            <ul className="space-y-2">
              {slots.map((s) => (
                <li key={s.id} className="rounded-lg bg-[var(--panel)] p-2 text-[13px]">
                  <div className="font-medium">{new Date(s.slot_at).toLocaleTimeString("en-IN", { timeZone: tz, hour: "2-digit", minute: "2-digit", hour12: false })} {s.format === "SHORT" ? "Short" : "Long-form"}</div>
                  {s.project ? <Link href={`/videos/detail/?id=${s.project.id}`} className="mt-1 block"><Pill tone={stageTone(s.project.stage)}>{STAGE_LABEL[s.project.stage]}</Pill></Link>
                    : <span className="text-[var(--muted)]">{s.status === "OPEN" ? "Not started" : s.status.toLowerCase()}</span>}
                </li>
              ))}
            </ul>
          </div>
        ))}
      </div>

      <Panel title="Schedule rules" aside={<Button kind="primary" onClick={save}>Save schedule</Button>}>
        <div className="space-y-4">
          {rules.map((r, i) => (
            <div key={i} className="grid items-end gap-3 rounded-lg border border-[var(--line)] p-3 md:grid-cols-[1fr_140px_120px_auto_auto]">
              <Field label="Rule name"><input className={inputCls} value={r.key} onChange={(e) => upd(i, { key: e.target.value })} /></Field>
              <Field label="Format">
                <select className={inputCls} value={r.format} onChange={(e) => upd(i, { format: e.target.value })}>
                  <option value="SHORT">Short</option><option value="LONG">Long-form</option>
                </select>
              </Field>
              <Field label="Time"><input type="time" className={inputCls} value={r.time} onChange={(e) => upd(i, { time: e.target.value })} /></Field>
              <fieldset><legend className="text-[14px] font-medium">Days</legend>
                <div className="mt-1.5 flex gap-1">{DAYS.map((d, di) => (
                  <button key={d} type="button" aria-pressed={r.weekdays.includes(di)}
                    onClick={() => upd(i, { weekdays: r.weekdays.includes(di) ? r.weekdays.filter((x: number) => x !== di) : [...r.weekdays, di].sort() })}
                    className={`h-9 w-10 rounded-md border text-[13px] ${r.weekdays.includes(di) ? "border-[var(--marigold)] bg-[color-mix(in_oklab,var(--marigold)_18%,transparent)]" : "border-[var(--line)] text-[var(--muted)]"}`}>{d}</button>))}
                </div>
              </fieldset>
              <Button kind="ghost" onClick={() => setRules(rules.filter((_, j) => j !== i))}>Remove</Button>
            </div>
          ))}
          <div className="flex flex-wrap items-end gap-4">
            <Button onClick={() => setRules([...rules, { key: `rule_${rules.length + 1}`, format: "SHORT", weekdays: [0, 1, 2, 3, 4], time: "18:30", enabled: true }])}>Add rule</Button>
            <div className="w-[300px]"><Field label="When a slot is missed" hint="e.g. generation ran late">
              <select className={inputCls} value={policy} onChange={(e) => setPolicy(e.target.value)}>
                <option value="next_free_slot">Move to the next free slot</option>
                <option value="publish_asap">Publish as soon as it’s ready</option>
                <option value="skip">Skip the slot</option>
              </select></Field></div>
          </div>
        </div>
      </Panel>
    </>
  );
}
