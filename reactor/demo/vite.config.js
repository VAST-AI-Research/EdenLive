import { defineConfig } from "vite";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { existsSync, readFileSync, readdirSync } from "node:fs";
import { createRequire } from "node:module";

const here = path.dirname(fileURLToPath(import.meta.url));
const sdk = path.dirname(
  createRequire(import.meta.url).resolve("@reactor-team/js-sdk"),
);

// The API keeps the seven-game identifiers; upstream renamed three directories.
const games = {
  freeze: "freeze", spiders: "spiders", tracer: "tracer",
  bioshock: "deepwater-shock", godzilla: "kaiju-rampage",
  arcadeduel: "arcadeduel", dust2: "desert-crossfire",
};

export function gameAssets(root) {
  const assets = new Map();
  const add = (file, name) => {
    if (!existsSync(file)) throw Error(`Missing EdenLive game asset: ${file}`);
    assets.set(`local-games/${name}`, file);
  };
  const tree = (directory, prefix) => {
    for (const entry of readdirSync(directory, { withFileTypes: true })) {
      const file = path.join(directory, entry.name);
      const name = `${prefix}/${entry.name}`;
      if (entry.isDirectory()) tree(file, name);
      else if (entry.isFile()) add(file, name);
    }
  };
  for (const [slug, renamed] of Object.entries(games)) {
    const directory = path.join(root, existsSync(path.join(root, slug)) ? slug : renamed);
    // main.js, its relative imports, initial state and the original cover.
    tree(directory, slug);
  }
  for (const name of ["input.js", "demo-runtime.js"])
    add(path.join(root, "host", name), `host/${name}`);
  tree(path.join(root, "runtime"), "runtime");
  for (const name of ["LICENSE", "NOTICE"])
    add(path.join(root, "..", name), name);
  const three = path.resolve(path.dirname(createRequire(import.meta.url).resolve("three")), "..");
  for (const name of ["build/three.module.js", "build/three.core.js", "LICENSE"])
    add(path.join(three, name), `node_modules/three-demo/${name}`);
  return assets;
}

function upstreamGames() {
  let assets;
  return {
    name: "edenlive-upstream-games",
    buildStart() {
      // In EdenLive, use the repository's games. In Cookbook, reuse the model bundle.
      const root = process.env.EDEN_GAMES_ROOT || [
        path.resolve(here, "../../realtime/games"),
        process.env.EDEN_BUNDLE && path.join(process.env.EDEN_BUNDLE, "source/realtime/games"),
      ].find((directory) => directory && existsSync(directory));
      if (!root) throw Error(
        "EdenLive games not found. Prepare EDEN_BUNDLE as described in README.md " +
        "or set EDEN_GAMES_ROOT to an existing realtime/games directory.",
      );
      assets = gameAssets(path.resolve(root));
    },
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        let name;
        try { name = decodeURIComponent((req.url || "").split("?")[0]).replace(/^\//, ""); }
        catch { res.statusCode = 400; res.end("Invalid asset path"); return; }
        if (!name.startsWith("local-games/")) return next();
        const file = assets?.get(name);
        if (!file) { res.statusCode = 404; res.end("Game asset not found"); return; }
        const types = { ".js": "text/javascript", ".json": "application/json",
          ".css": "text/css", ".png": "image/png", ".jpg": "image/jpeg",
          ".webp": "image/webp", ".svg": "image/svg+xml", ".html": "text/html" };
        res.setHeader("Content-Type", types[path.extname(file)] || "text/plain");
        res.end(readFileSync(file));
      });
    },
    generateBundle() {
      for (const [fileName, file] of assets)
        this.emitFile({ type: "asset", fileName, source: readFileSync(file) });
    },
  };
}

export default defineConfig({
  // Relative asset URLs: dist/ works from any static host, at the root or under a sub-path.
  base: "./",
  optimizeDeps: {
    exclude: ["@reactor-team/js-sdk"],
    include: [
      "react",
      "react/jsx-runtime",
      "react/jsx-dev-runtime",
      "react-dom/client",
      "awaitqueue",
      "hls.js",
      "mp4box",
    ],
  },
  server: { host: "127.0.0.1", strictPort: true, fs: { allow: [here, sdk] } },
  preview: { host: "127.0.0.1", strictPort: true },
  build: { target: "es2022" },
  plugins: [
    upstreamGames(),
    {
      name: "reactor-sdk-wasm-assets",
      generateBundle() {
        for (const name of ["reactor_wasm.js", "reactor_wasm_bg.wasm"])
          this.emitFile({
            type: "asset",
            fileName: `assets/wasm/${name}`,
            source: readFileSync(path.join(sdk, "wasm", name)),
          });
      },
    },
  ],
});
