import { test } from "node:test";
import assert from "node:assert/strict";
import { setTimeout as wait } from "node:timers/promises";
import { createControls } from "./controls.js";

test("burst collapses to latest keys with only one command in flight", async () => {
  const calls = [], replies = [];
  const controller = createControls((keys) => {
    calls.push(keys);
    return new Promise((resolve) => replies.push(resolve));
  }, assert.fail, 1);
  controller.set(["w"]);
  controller.start();
  await wait(10);
  for (let i = 0; i < 1000; i++) controller.set(i % 2 ? ["a"] : ["d"]);
  controller.set([]);
  await wait(10);
  assert.deepEqual(calls, [["w"]]);
  replies.shift()();
  await wait(10);
  assert.deepEqual(calls, [["w"], []]);
  replies.shift()();
  await wait(10);
  assert.equal(calls.length, 2);
  await controller.stop();
});

test("disconnect drops pending state without reopening the connection", async () => {
  const calls = [];
  let reply;
  const controller = createControls((keys) => {
    calls.push(keys);
    return new Promise((resolve) => { reply = resolve; });
  }, assert.fail, 1);
  controller.start();
  await wait(10);
  controller.set(["f"]);
  const stopped = controller.stop();
  reply();
  await stopped;
  await wait(10);
  assert.deepEqual(calls, [[]]);
});

test("a failed command stops polling instead of filling a retry queue", async () => {
  const errors = [];
  let calls = 0;
  const controller = createControls(async () => {
    calls++;
    throw Error("refused");
  }, (error) => errors.push(error.message), 1);
  controller.start();
  await wait(10);
  controller.set(["f"]);
  await wait(10);
  await controller.stop();
  assert.equal(calls, 1);
  assert.deepEqual(errors, ["refused"]);
});
