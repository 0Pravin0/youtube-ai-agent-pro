"use client";
/** Finds the local engine. Inside the desktop app the Tauri shell tells us its address and the
 * per-launch token. In a plain browser (development) they come from ?engine=&token= or env vars. */

export type Engine = { url: string; token: string; desktop: boolean };

let cached: Engine | null = null;
let pending: Promise<Engine> | null = null;

export function isDesktop() {
  return typeof window !== "undefined" && !!(window as any).__TAURI_INTERNALS__;
}

export async function getEngine(): Promise<Engine> {
  if (cached) return cached;
  if (pending) return pending;
  pending = (async () => {
    if (isDesktop()) {
      const { invoke } = await import("@tauri-apps/api/core");
      // The shell answers once the engine process is up (it waits for /api/health).
      const info = await invoke<{ url: string; token: string }>("engine_info");
      cached = { ...info, desktop: true };
    } else {
      const q = new URLSearchParams(location.search);
      // Portable Windows edition: the engine serves this UI itself and the launcher passes the
      // per-launch token in the URL fragment (#t=...), which never leaves the browser.
      const frag = new URLSearchParams(location.hash.replace(/^#/, ""));
      const fragToken = frag.get("t");
      if (fragToken) history.replaceState(null, "", location.pathname + location.search);
      const sameOrigin = location.protocol === "http:" && (location.hostname === "127.0.0.1" || location.hostname === "localhost") && location.port !== "3000";
      let url = q.get("engine") || (sameOrigin ? location.origin : "") || sessionStorageGet("yta-engine") || process.env.NEXT_PUBLIC_ENGINE_URL || "http://127.0.0.1:47821";
      let token = fragToken || q.get("token") || sessionStorageGet("yta-token") || process.env.NEXT_PUBLIC_ENGINE_TOKEN || "";
      sessionStorageSet("yta-engine", url);
      sessionStorageSet("yta-token", token);
      cached = { url, token, desktop: false };
    }
    return cached!;
  })();
  try {
    return await pending;
  } finally {
    pending = null;
  }
}

/** Synchronous variant for building <img>/<video> URLs after the engine is known. */
export function engineNow(): Engine | null {
  return cached;
}

export function mediaUrl(path: string): string {
  const e = cached;
  if (!e) return "";
  const sep = path.includes("?") ? "&" : "?";
  return `${e.url}${path}${sep}token=${encodeURIComponent(e.token)}`;
}

export async function openExternal(url: string) {
  if (isDesktop()) {
    const { openUrl } = await import("@tauri-apps/plugin-opener");
    await openUrl(url);
  } else {
    // Portable edition: ask the local engine to open the user's normal browser.
    try {
      const e = await getEngine();
      const r = await fetch(`${e.url}/api/system/open`, {
        method: "POST", headers: { "content-type": "application/json", "x-yta-token": e.token }, body: JSON.stringify({ url }),
      });
      if (r.ok) return;
    } catch {}
    window.open(url, "_blank", "noopener");
  }
}

export async function syncTray(closeToTray: boolean) {
  if (!isDesktop()) return;
  try {
    const { invoke } = await import("@tauri-apps/api/core");
    await invoke("set_close_to_tray", { enabled: closeToTray });
  } catch {}
}

export async function desktopNotify(title: string, body: string) {
  if (!isDesktop()) {
    try {
      if (typeof Notification === "undefined") return;
      let p = Notification.permission;
      if (p === "default") p = await Notification.requestPermission();
      if (p === "granted") new Notification(title, { body, icon: "/icon.png" });
    } catch {}
    return;
  }
  try {
    const n = await import("@tauri-apps/plugin-notification");
    let ok = await n.isPermissionGranted();
    if (!ok) ok = (await n.requestPermission()) === "granted";
    if (ok) n.sendNotification({ title, body });
  } catch {}
}

function sessionStorageGet(k: string) {
  try { return sessionStorage.getItem(k); } catch { return null; }
}
function sessionStorageSet(k: string, v: string) {
  try { sessionStorage.setItem(k, v); } catch {}
}
