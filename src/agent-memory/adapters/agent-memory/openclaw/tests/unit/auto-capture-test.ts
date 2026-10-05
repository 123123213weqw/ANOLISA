/**
 * agent_end auto-capture Unicode-boundary regressions.
 *
 * The observation bound is `slice(0, 2000)` on UTF-16 code units, so a
 * supplementary character (emoji, ext-B CJK) whose surrogate pair
 * straddles unit 1999/2000 is split: the captured `memory_observe`
 * content ends with an unpaired high surrogate. That is not a complete
 * Unicode prefix and strict string consumers — the Rust JSON string
 * decoder among them — reject it, silently dropping the observation.
 *
 * These tests fire the actual registered `agent_end` hook for string and
 * text-block messages against a receiver that only accepts complete
 * Unicode scalar sequences. No subprocess is spawned: `callTool` is
 * intercepted on the client prototype. Boundary cases assert the pair
 * is never split; controls pin short text, a pair fully inside the
 * bound, and BMP CJK at the bound.
 */

import { describe, it, before, after } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { McpStdioClient } from "../../src/mcp-client.js";

const plugin = (await import("../../src/index.js")).default;

// resolveConfig requires an existing, executable binaryPath. It is never
// spawned — callTool is intercepted below — so a stub file is enough.
const binDir = fs.mkdtempSync(path.join(os.tmpdir(), "agent-memory-capture-"));
const binaryPath = path.join(binDir, "agent-memory");
fs.writeFileSync(binaryPath, "#!/bin/sh\nexit 0\n");
fs.chmodSync(binaryPath, 0o755);

type Observation = { tool: string; content: string; hint: string };

/** True when every code unit is a complete Unicode scalar value: BMP
 *  non-surrogates, or surrogate pairs pairing up exactly. A lone high
 *  or low surrogate makes the string invalid — this is the "private
 *  Unicode-scalar receiver" standing in for the store's strict decoder. */
function isCompleteUnicodeScalars(value: string): boolean {
  for (let i = 0; i < value.length; i++) {
    const unit = value.charCodeAt(i);
    if (unit >= 0xd800 && unit <= 0xdbff) {
      const next = value.charCodeAt(i + 1);
      if (!(next >= 0xdc00 && next <= 0xdfff)) return false;
      i++;
    } else if (unit >= 0xdc00 && unit <= 0xdfff) {
      return false;
    }
  }
  return true;
}

const observations: Observation[] = [];
const rejections: string[] = [];

before(() => {
  McpStdioClient.prototype.callTool = async function (
    this: McpStdioClient,
    contractName: string,
    args: Record<string, unknown>,
  ) {
    const content = String(args.content ?? "");
    if (!isCompleteUnicodeScalars(content)) {
      rejections.push(content);
      throw new Error(
        "receiver rejected content with unpaired surrogate code units",
      );
    }
    observations.push({
      tool: contractName,
      content,
      hint: String(args.hint ?? ""),
    });
    return "{}";
  };
});

after(() => {
  fs.rmSync(binDir, { recursive: true, force: true });
});

/** Build an assistant message body whose supplementary `marker` starts
 *  exactly at UTF-16 unit `startAt`, followed by trailing filler so the
 *  2000-unit bound actually truncates. The head carries a trigger phrase
 *  so the capture path proceeds. `tailLen` tunes the total length (0
 *  gives a message that ends right after the marker's pair). */
function contentWithMarkerAt(marker: string, startAt: number, tailLen = 16): string {
  const head = "I decided: ";
  assert.ok(head.length <= startAt, "head must fit before the marker");
  const pad = "x".repeat(startAt - head.length);
  return head + pad + marker + "y".repeat(tailLen);
}

/** Marks for the per-test deltas: observations and rejections both
 *  accumulate across tests because the hook is module-scoped. */
function marks() {
  return { observations: observations.length, rejections: rejections.length };
}

function newObservations(mark: { observations: number }): Observation[] {
  return observations.slice(mark.observations);
}

function newRejections(mark: { rejections: number }): string[] {
  return rejections.slice(mark.rejections);
}

function mockHost() {
  const handlers = new Map<string, (event: unknown, ctx: unknown) => unknown>();
  const api = {
    pluginConfig: { binaryPath, userId: "1000", sessionId: "ses_capture", profile: "advanced" },
    resolvePath: (p: string) => p,
    logger: {
      info: () => {},
      debug: () => {},
      error: () => {},
      warn: () => {},
    },
    on: (event: string, handler: (event: unknown, ctx: unknown) => unknown) => {
      handlers.set(event, handler);
    },
    registerTool: () => {},
    registerMemoryCapability: () => {},
    registerMemoryCorpusSupplement: () => {},
  };
  plugin.register(api as never);
  return handlers;
}

async function fireAgentEnd(
  handlers: Map<string, (event: unknown, ctx: unknown) => unknown>,
  content: unknown,
): Promise<void> {
  const hook = handlers.get("agent_end");
  assert.ok(hook, "agent_end hook must be registered");
  await hook({ messages: [{ role: "assistant", content }] }, {});
}

describe("agent_end auto-capture Unicode boundary", () => {
  it("keeps a complete prefix when an emoji is split by the bound (string message)", async () => {
    const mark = marks();
    const full = contentWithMarkerAt("😀", 1999);
    await fireAgentEnd(mockHost(), full);

    const savedObservations = newObservations(mark);
    assert.equal(savedObservations.length, 1, "observation must be saved");
    const saved = savedObservations[0].content;
    assert.ok(
      isCompleteUnicodeScalars(saved),
      "captured content must not end with an unpaired surrogate",
    );
    assert.equal(saved.length, 1999, "the orphaned high surrogate is dropped, not replaced");
    assert.equal(saved, full.slice(0, 1999));
    assert.match(savedObservations[0].hint, /^auto-capture-[0-9a-f]{16}$/);
    assert.deepEqual(newRejections(mark), [], "the receiver must never see a half character");
  });

  it("keeps a complete prefix when the message ends right after the split pair", async () => {
    // Minimal shape: units 0..2000, the pair at 1999/2000 is the very
    // end of the message — truncation by exactly one unit.
    const mark = marks();
    const full = contentWithMarkerAt("😀", 1999, 0);
    assert.equal(full.length, 2001);
    await fireAgentEnd(mockHost(), full);

    const savedObservations = newObservations(mark);
    assert.equal(savedObservations.length, 1, "observation must be saved");
    const saved = savedObservations[0].content;
    assert.ok(isCompleteUnicodeScalars(saved));
    assert.equal(saved.length, 1999);
    assert.equal(saved, full.slice(0, 1999));
    assert.deepEqual(newRejections(mark), []);
  });

  it("keeps a complete prefix when a supplementary CJK character is split by the bound", async () => {
    const mark = marks();
    const full = contentWithMarkerAt("𠀀", 1999);
    await fireAgentEnd(mockHost(), full);

    const savedObservations = newObservations(mark);
    assert.equal(savedObservations.length, 1, "observation must be saved");
    const saved = savedObservations[0].content;
    assert.ok(isCompleteUnicodeScalars(saved));
    assert.equal(saved.length, 1999);
    assert.equal(saved, full.slice(0, 1999));
    assert.deepEqual(newRejections(mark), []);
  });

  it("keeps a complete prefix for text-block messages split at the bound", async () => {
    const mark = marks();
    const full = contentWithMarkerAt("😀", 1999);
    await fireAgentEnd(
      mockHost(),
      [{ type: "text", text: full }],
    );

    const savedObservations = newObservations(mark);
    assert.equal(savedObservations.length, 1, "observation must be saved");
    const saved = savedObservations[0].content;
    assert.ok(isCompleteUnicodeScalars(saved));
    assert.equal(saved.length, 1999);
    assert.equal(saved, full.slice(0, 1999));
    assert.deepEqual(newRejections(mark), []);
  });

  it("does not shorten the capture when the bound cuts inside trailing BMP filler (control)", async () => {
    // The 2000th unit is plain filler: nothing is split, so the full
    // 2000-unit bound is used unchanged.
    const mark = marks();
    const full = contentWithMarkerAt("汉", 1999);
    await fireAgentEnd(mockHost(), full);

    const savedObservations = newObservations(mark);
    assert.equal(savedObservations.length, 1, "observation must be saved");
    const saved = savedObservations[0].content;
    assert.equal(saved.length, 2000, "BMP character at the bound is not dropped");
    assert.equal(saved, full.slice(0, 2000));
    assert.ok(isCompleteUnicodeScalars(saved));
    assert.deepEqual(newRejections(mark), []);
  });

  it("keeps a surrogate pair that ends exactly at the bound (control)", async () => {
    const mark = marks();
    const full = contentWithMarkerAt("😀", 1998);
    await fireAgentEnd(mockHost(), full);

    const savedObservations = newObservations(mark);
    assert.equal(savedObservations.length, 1, "observation must be saved");
    const saved = savedObservations[0].content;
    assert.equal(saved.length, 2000, "a complete pair inside the bound is kept whole");
    assert.ok(saved.endsWith("😀"));
    assert.ok(isCompleteUnicodeScalars(saved));
    assert.deepEqual(newRejections(mark), []);
  });

  it("leaves short triggered content byte-for-byte unchanged (control)", async () => {
    const mark = marks();
    const short = "I decided: 👍 important note 通知";
    await fireAgentEnd(mockHost(), short);

    const savedObservations = newObservations(mark);
    assert.equal(savedObservations.length, 1, "observation must be saved");
    assert.equal(savedObservations[0].content, short);
    assert.deepEqual(newRejections(mark), []);
  });
});
