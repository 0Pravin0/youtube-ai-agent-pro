"use client";
import { Suspense, useState } from "react";
import { useShell } from "@/components/Shell";
import { Button, ErrorNote, PageTitle, Panel, Pill } from "@/components/ui";
import Link from "next/link";
import { ago, api, useApi } from "@/lib/api";
import { openExternal } from "@/lib/engine";

const KIND_LABEL: Record<string, string> = { LLM: "Reasoning and writing", IMAGE: "Images", VIDEO: "Video clips", TTS: "Tamil voices", YOUTUBE: "YouTube", RESEARCH: "Research" };
const STATUS_TONE: Record<string, any> = { AVAILABLE: "good", UNAVAILABLE: "bad", UNCONFIGURED: "neutral", UNVERIFIED: "warn", DISABLED: "neutral" };
const STATUS_TEXT: Record<string, string> = { AVAILABLE: "Working", UNAVAILABLE: "Failing", UNCONFIGURED: "Not set up", UNVERIFIED: "Not tested yet", DISABLED: "Turned off" };

function Connections() {
  const { data, error, reload } = useApi<any[]>("/providers");
  const ov = useShell().overview;
  const { toast } = useShell();
  const [results, setResults] = useState<Record<string, string>>({});
  const test = async (key: string) => {
    const r = await api("/providers/test", { method: "POST", body: { key } });
    setResults({ ...results, [key]: r.result });
    toast(r.ok ? `${key}: working` : `${key}: ${r.result}`, !r.ok);
    reload();
  };
  const connect = async () => {
    try { const r = await api("/channels/connect", { method: "POST" }); await openExternal(r.authorization_url); toast("Finish signing in in your browser"); }
    catch (e: any) { toast(e.message, true); }
  };
  const disconnect = async () => { await api("/channels/disconnect", { method: "POST" }); toast("YouTube disconnected"); reload(); };
  const toggle = async (p: any) => { await api(`/providers/${p.key}`, { method: "PATCH", body: { enabled: !p.enabled } }); reload(); };
  const groups: Record<string, any[]> = {};
  for (const p of data || []) (groups[p.kind] ||= []).push(p);

  return (
    <>
      <PageTitle title="API Connections" sub="A provider is used only after a live test succeeds. Keys are kept in Windows Credential Manager on this computer."
        actions={<Link href="/setup/" className="rounded-lg bg-[var(--marigold)] px-3.5 py-2 text-[14px] font-medium text-[var(--marigold-ink)]">Change keys and accounts</Link>} />
      <ErrorNote error={error} />
      <Panel title="YouTube channel" className="mb-6">
        <div className="flex flex-wrap items-center justify-between gap-4">
          <div>
            <div className="font-medium">{ov?.channel?.name}</div>
            <div className="text-[14px] text-[var(--muted)]">{ov?.channel?.connected ? `Connected as ${ov.channel.youtube_channel_id}` : "Not connected. Connecting opens Google’s consent screen for upload, playlist and analytics access."}</div>
            {ov?.channel?.connection_error && <div className="text-[14px] text-[var(--kumkum)]">{ov.channel.connection_error}</div>}
          </div>
          {ov?.channel?.connected ? <Button kind="danger" onClick={disconnect}>Disconnect YouTube</Button> : <Button kind="primary" onClick={connect}>Connect YouTube</Button>}
        </div>
      </Panel>
      <div className="space-y-6">
        {Object.entries(groups).map(([kind, rows]) => (
          <Panel key={kind} title={KIND_LABEL[kind] || kind} flush>
            <table className="tbl text-[14px]">
              <thead><tr><th>Provider</th><th>Status</th><th>Last test</th><th></th></tr></thead>
              <tbody>{rows.map((p) => (
                <tr key={p.key}>
                  <td className="max-w-[340px]"><div className="font-medium">{p.display_name}</div>
                    <div className="text-[12px] text-[var(--muted)]">{Object.entries(p.capabilities || {}).slice(0, 4).map(([k, v]) => `${k}: ${Array.isArray(v) ? v.join("/") : v}`).join("; ")}</div></td>
                  <td><Pill tone={STATUS_TONE[p.status]}>{STATUS_TEXT[p.status]}</Pill>{p.circuit_state !== "CLOSED" && <div className="mt-1"><Pill tone="bad">circuit {p.circuit_state.toLowerCase()}</Pill></div>}</td>
                  <td className="max-w-[360px] break-words">{results[p.key] || p.last_test_result || "—"}<div className="text-[12px] text-[var(--muted)]">{p.last_test_at && ago(p.last_test_at)}</div></td>
                  <td className="text-right whitespace-nowrap"><Button onClick={() => test(p.key)}>Test</Button>{" "}
                    {p.kind !== "YOUTUBE" && p.kind !== "LLM" && <Button kind="ghost" onClick={() => toggle(p)}>{p.enabled ? "Turn off" : "Turn on"}</Button>}</td>
                </tr>))}</tbody>
            </table>
          </Panel>
        ))}
      </div>
    </>
  );
}

export default function Page() { return <Suspense><Connections /></Suspense>; }
