import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import config, { gameAssets } from "./vite.config.js";

const slugs = ["freeze", "spiders", "tracer", "bioshock", "godzilla", "arcadeduel", "dust2"];
const renamed = { bioshock: "deepwater-shock", godzilla: "kaiju-rampage", dust2: "desert-crossfire" };

function source(t, rename) {
  const root = mkdtempSync(path.join(os.tmpdir(), "edenlive-demo-assets-"));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  const games = path.join(root, "games");
  const files = ["LICENSE", "NOTICE", "games/host/input.js", "games/host/demo-runtime.js",
    "games/runtime/prompts.js", ...slugs.flatMap((slug) =>
      ["main.js", "adapter.js", "initial-state.json", "cover.png"].map((name) =>
        `games/${rename ? renamed[slug] || slug : slug}/${name}`))];
  for (const file of files) {
    mkdirSync(path.dirname(path.join(root, file)), { recursive: true });
    writeFileSync(path.join(root, file), file);
  }
  return games;
}

for (const rename of [false, true]) {
  test(`original assets resolve with ${rename ? "renamed" : "legacy"} game directories`, (t) => {
    const root = source(t, rename);
    const assets = gameAssets(root);
    for (const slug of slugs) {
      assert.equal(assets.get(`local-games/${slug}/cover.png`),
        path.join(root, rename ? renamed[slug] || slug : slug, "cover.png"));
      assert.ok(assets.has(`local-games/${slug}/main.js`));
    }
    assert.match(assets.get("local-games/node_modules/three-demo/build/three.module.js"),
      /node_modules\/three\/build\/three.module.js$/);
    assert.ok(assets.has("local-games/node_modules/three-demo/LICENSE"));
    assert.ok(assets.has("local-games/LICENSE"));
    assert.equal(assets.has("local-games/host/standalone.js"), false);
    assert.equal(assets.has("local-games/../package.json"), false);
  });
}

test("dev and build serve the same allowlisted assets", (t) => {
  const root = source(t, true);
  const previous = process.env.EDEN_GAMES_ROOT;
  process.env.EDEN_GAMES_ROOT = root;
  t.after(() => {
    if (previous === undefined) delete process.env.EDEN_GAMES_ROOT;
    else process.env.EDEN_GAMES_ROOT = previous;
  });
  const plugin = config.plugins.find((item) => item.name === "edenlive-upstream-games");
  plugin.buildStart();
  let serve;
  plugin.configureServer({ middlewares: { use: (fn) => { serve = fn; } } });
  const emitted = new Map();
  plugin.generateBundle.call({ emitFile: ({ fileName, source }) => emitted.set(fileName, source) });
  const response = (url) => {
    const res = { statusCode: 200, headers: {}, setHeader(k, v) { this.headers[k] = v; },
      end(body) { this.body = body; } };
    serve({ url }, res, () => { res.next = true; });
    return res;
  };
  const cover = response("/local-games/bioshock/cover.png?t=1");
  assert.equal(cover.headers["Content-Type"], "image/png");
  assert.deepEqual(cover.body, emitted.get("local-games/bioshock/cover.png"));
  assert.equal(response("/local-games/%2e%2e/package.json").statusCode, 404);
  assert.equal(response("/local-games/host/standalone.js").statusCode, 404);
  assert.equal(response("/local-games/%GG").statusCode, 400);
  assert.equal(response("/src/main.jsx").next, true);
});
