"use client";
import { useState } from "react";
import { useShell } from "@/components/Shell";
import { Button, Empty, Field, PageTitle, Panel, Pill, inputCls } from "@/components/ui";
import { ago, api, useApi } from "@/lib/api";

export default function McpPage() {
  const { toast } = useShell();
  const { data: servers, reload } = useApi<any[]>("/mcp/servers", 30000);
  const { data: audit, reload: reloadAudit } = useApi<any[]>("/mcp/audit?limit=30", 30000);
  const { data: settings, reload: reloadSettings } = useApi<any>("/settings");
  const [f, setF] = useState({ name: "", url: "", token: "", description: "" });
  const [sel, setSel] = useState<Record<string, string[]>>({});
  const [test, setTest] = useState<Record<string, any>>({});
  const [arg, setArg] = useState<Record<string, string>>({});

  const act = async (fn: () => Promise<any>, ok: string) => { try { await fn(); toast(ok); reload(); reloadAudit(); } catch (e: any) { toast(e.message, true); } };
  const add = () => act(async () => {
    await api("/mcp/servers", { method: "POST", body: { ...f, token: f.token || null } });
    setF({ name: "", url: "", token: "", description: "" });
  }, "Server added — review its tools below");
  const research = settings?.extra?.mcp_research || {};
  const setResearch = (server: string, tool: string) => act(async () => {
    await api("/settings", { method: "PUT", body: { extra: { mcp_research: server ? { server, tool, query_arg: "query" } : {} } } }); reloadSettings();
  }, server ? `Research agent will use ${server}/${tool}` : "Research tool removed");

  return (
    <div className="space-y-6">
      <PageTitle title="MCP Connections" sub="Model Context Protocol tools the agents may use. Built-in servers are read-only; other servers stay off until you approve each tool." />
      {(servers || []).length === 0 && <Empty title="No servers" />}
      {(servers || []).map((s) => {
        const chosen = sel[s.id] ?? s.allowed_tools.filter((t: string) => t !== "*");
        return (
          <Panel key={s.id} title={<span className="flex flex-wrap items-center gap-2">{s.name}
            <Pill tone={s.status === "connected" ? "good" : s.status === "failed" ? "bad" : "neutral"}>{s.status}</Pill>
            {s.trusted ? <Pill tone="accent">built-in</Pill> : <Pill>{s.transport === "http" ? "remote" : s.transport}</Pill>}
            {s.enabled ? <Pill tone="good">enabled</Pill> : <Pill>off</Pill>}</span>}
            aside={<div className="flex gap-2">
              <Button kind="ghost" onClick={() => act(() => api(`/mcp/servers/${s.id}/discover`, { method: "POST" }), "Checked")}>Check now</Button>
              {!s.trusted && <Button kind="danger" onClick={() => act(() => api(`/mcp/servers/${s.id}`, { method: "DELETE" }), "Removed")}>Remove</Button>}
            </div>}>
            <p className="text-[14px] text-[var(--muted)]">{s.description || s.target}</p>
            <p className="mt-1 text-[13px] text-[var(--muted)]">Last check {s.last_checked_at ? ago(s.last_checked_at) : "never"} · last used {s.last_used_at ? ago(s.last_used_at) : "never"}{s.has_token ? " · access token in Keychain" : ""}</p>
            {s.last_error && <p className="mt-1 text-[14px] text-[var(--kumkum)]">{s.last_error}</p>}
            <ul className="mt-3 divide-y divide-[var(--line)] rounded-lg border border-[var(--line)]">
              {(s.tools || []).map((t: any) => (
                <li key={t.name} className="flex flex-wrap items-start gap-3 px-3 py-2 text-[14px]">
                  {!s.trusted && <input type="checkbox" aria-label={`Allow ${t.name}`} className="mt-1 h-4 w-4 accent-[var(--marigold)]" checked={chosen.includes(t.name)}
                    onChange={(e) => setSel({ ...sel, [s.id]: e.target.checked ? [...chosen, t.name] : chosen.filter((x: string) => x !== t.name) })} />}
                  <div className="min-w-0 flex-1"><b className="font-medium">{t.name}</b> {t.allowed ? <Pill tone="good">allowed</Pill> : <Pill>not allowed</Pill>}
                    <div className="text-[var(--muted)]">{t.description}</div>
                    <div className="text-[12px] text-[var(--muted)]">Needs: {Object.keys(t.input_schema?.properties || {}).join(", ") || "no input"}</div></div>
                  {t.allowed && Object.keys(t.input_schema?.properties || {}).length === 1 && (
                    <input className={`${inputCls} max-w-[200px]`} aria-label={`Value for ${Object.keys(t.input_schema.properties)[0]}`}
                      placeholder={Object.keys(t.input_schema.properties)[0]} value={arg[s.id + t.name] || ""}
                      onChange={(e) => setArg({ ...arg, [s.id + t.name]: e.target.value })} />)}
                  {t.allowed && Object.keys(t.input_schema?.properties || {}).length <= 1 && (
                    <Button kind="ghost" onClick={async () => {
                      const key = Object.keys(t.input_schema?.properties || {})[0];
                      const val = arg[s.id + t.name] || "";
                      try { setTest({ ...test, [s.id + t.name]: await api(`/mcp/servers/${s.id}/call`, { method: "POST", body: { tool: t.name, arguments: key ? { [key]: val } : {} } }) }); reloadAudit(); }
                      catch (e: any) { toast(e.message, true); }
                    }}>Try it</Button>)}
                  {test[s.id + t.name] && <pre className="mt-1 max-h-40 w-full overflow-auto rounded bg-[var(--panel-2)] p-2 text-[12px]">{JSON.stringify(test[s.id + t.name].structured ?? test[s.id + t.name].content, null, 2)}</pre>}
                  {!s.trusted && t.allowed && (
                    <Button kind="ghost" onClick={() => setResearch(research.server === s.name && research.tool === t.name ? "" : s.name, t.name)}>
                      {research.server === s.name && research.tool === t.name ? "Stop using for research" : "Use for research"}</Button>)}
                </li>))}
            </ul>
            {!s.trusted && (
              <div className="mt-3 flex flex-wrap gap-2">
                <Button onClick={() => act(() => api(`/mcp/servers/${s.id}`, { method: "PUT", body: { approve_tools: chosen } }), "Tool permissions saved")}>Save allowed tools</Button>
                <Button kind={s.enabled ? "danger" : "primary"} onClick={() => act(() => api(`/mcp/servers/${s.id}`, { method: "PUT", body: { enabled: !s.enabled } }), s.enabled ? "Disabled" : "Enabled")}>{s.enabled ? "Disable" : "Enable"}</Button>
              </div>)}
          </Panel>);
      })}
      <Panel title="Add a remote MCP server">
        <p className="mb-3 text-[14px] text-[var(--muted)]">Only add servers you trust. They never receive your Google sign-in or API keys; their answers are treated as information, never as instructions. Nothing is installed on your Mac.</p>
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Name (lowercase)"><input className={inputCls} value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} placeholder="research" /></Field>
          <Field label="Server address (https)"><input className={inputCls} value={f.url} onChange={(e) => setF({ ...f, url: e.target.value })} placeholder="https://example.com/mcp" /></Field>
          <Field label="Access token (optional)" hint="Saved in the macOS Keychain"><input className={inputCls} type="password" value={f.token} onChange={(e) => setF({ ...f, token: e.target.value })} /></Field>
          <Field label="What is it for?"><input className={inputCls} value={f.description} onChange={(e) => setF({ ...f, description: e.target.value })} /></Field>
        </div>
        <div className="mt-3"><Button kind="primary" disabled={!f.name || !f.url} onClick={add}>Add and inspect</Button></div>
      </Panel>
      <Panel title="Tool activity (audit log)">
        {(audit || []).length === 0 ? <p className="text-[14px] text-[var(--muted)]">No tool calls yet.</p> : (
          <ul className="space-y-1 text-[13px]">
            {(audit || []).map((a, i) => <li key={i} className="flex flex-wrap gap-2"><span className="text-[var(--muted)]">{ago(a.at)}</span><b>{a.server}/{a.tool}</b><span>by {a.caller}</span>
              <Pill tone={a.ok ? "good" : "bad"}>{a.ok ? "ok" : "failed"}</Pill><span className="text-[var(--muted)]">{a.seconds}s</span></li>)}
          </ul>)}
      </Panel>
    </div>
  );
}
