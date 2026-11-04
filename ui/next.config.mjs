/** Static export: the desktop shell (Tauri) loads these files directly; the UI talks to the local
 * engine on 127.0.0.1 using the address and per-launch token the shell provides at runtime. */
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));

export default {
  output: "export",
  distDir: "out-build",
  trailingSlash: true,
  images: { unoptimized: true },
  poweredByHeader: false,
  webpack(config) {
    config.resolve.alias["@"] = path.join(here, "src");
    return config;
  },
};
