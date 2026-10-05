import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { existsSync, mkdirSync, rmSync, chmodSync } from "fs";
import { tmpdir } from "os";
import { join } from "path";

// Record every temp directory mkdtempSync creates so tests can assert that
// cleanup removes only resources that were actually created.
const createdTempDirs: string[] = [];
// Fault injection switch for writes into the runCrontab temp directory.
let failTempFileWrites = false;

vi.mock("fs", async (importOriginal) => {
  const actual = await importOriginal<typeof import("fs")>();
  return {
    ...actual,
    mkdtempSync: vi.fn((prefix: string, options?: any) => {
      const dir = actual.mkdtempSync(prefix, options);
      createdTempDirs.push(dir);
      return dir;
    }),
    writeFileSync: vi.fn((path: any, data: any, options: any) => {
      if (failTempFileWrites && String(path).includes("ws-ckpt-cron-")) {
        const err: NodeJS.ErrnoException = new Error(
          "EIO: i/o error, open '" + path + "'",
        );
        err.code = "EIO";
        throw err;
      }
      return actual.writeFileSync(path, data, options);
    }),
  };
});

// The child_process mock matches commands.test.ts: runCrontab promisifies
// execFile, so mock through the custom promisify symbol.
vi.mock("child_process", () => {
  const sym = Symbol.for("nodejs.util.promisify.custom");
  const promisifiedFn = vi.fn();
  const fn = vi.fn();
  (fn as any)[sym] = promisifiedFn;
  return { execFile: fn };
});

import { execFile } from "child_process";
import { runCrontab } from "../commands.js";

const promisifiedMock = (execFile as any)[
  Symbol.for("nodejs.util.promisify.custom")
] as ReturnType<typeof vi.fn>;

describe("runCrontab temporary directory failures", () => {
  const originalTmpdir = process.env.TMPDIR;
  let unwritableDir: string | undefined;

  beforeEach(() => {
    vi.clearAllMocks();
    createdTempDirs.length = 0;
    failTempFileWrites = false;
    promisifiedMock.mockResolvedValue({ stdout: "", stderr: "" });
  });

  afterEach(() => {
    if (originalTmpdir === undefined) delete process.env.TMPDIR;
    else process.env.TMPDIR = originalTmpdir;
    if (unwritableDir) {
      try {
        chmodSync(unwritableDir, 0o755);
        rmSync(unwritableDir, { recursive: true, force: true });
      } catch {
        /* best effort */
      }
      unwritableDir = undefined;
    }
  });

  it("resolves with CommandOutput when the temp directory cannot be created (missing TMPDIR)", async () => {
    process.env.TMPDIR = join(tmpdir(), "ws-ckpt-cron-missing-ancestor");
    const result = await runCrontab(["-"], { input: "0 * * * * true\n" });
    expect(result.exitCode).toBe(1);
    expect(result.stderr).toBeTruthy();
    expect(promisifiedMock).not.toHaveBeenCalled();
  });

  it("resolves with CommandOutput when the temp directory is unwritable", async () => {
    unwritableDir = join(tmpdir(), "ws-ckpt-cron-readonly");
    rmSync(unwritableDir, { recursive: true, force: true });
    mkdirSync(unwritableDir, { mode: 0o555 });
    chmodSync(unwritableDir, 0o555);
    process.env.TMPDIR = unwritableDir;
    const result = await runCrontab(["-"], { input: "0 * * * * true\n" });
    expect(result.exitCode).toBe(1);
    expect(result.stderr).toBeTruthy();
    expect(promisifiedMock).not.toHaveBeenCalled();
  });

  it("resolves with CommandOutput and cleans up when writing the temp file fails", async () => {
    failTempFileWrites = true;
    const result = await runCrontab(["-"], { input: "0 * * * * true\n" });
    expect(result.exitCode).toBe(1);
    expect(result.stderr).toContain("EIO");
    expect(promisifiedMock).not.toHaveBeenCalled();
    // The directory that was successfully created must still be removed.
    expect(createdTempDirs.length).toBe(1);
    expect(existsSync(createdTempDirs[0])).toBe(false);
  });

  it("normal execution still succeeds and cleans the temp directory", async () => {
    const result = await runCrontab(["-"], { input: "0 * * * * true\n" });
    expect(result.exitCode).toBe(0);
    expect(result.stdout).toBe("");
    expect(promisifiedMock).toHaveBeenCalledTimes(1);
    expect(createdTempDirs.length).toBe(1);
    expect(existsSync(createdTempDirs[0])).toBe(false);
    const passedFile = promisifiedMock.mock.calls[0][1][0] as string;
    expect(passedFile).toContain("ws-ckpt-cron-");
    expect(passedFile.endsWith("crontab")).toBe(true);
  });

  it("keeps the no-input path contract (list command)", async () => {
    promisifiedMock.mockResolvedValue({ stdout: "line1\n", stderr: "" });
    const result = await runCrontab(["-l"]);
    expect(result.exitCode).toBe(0);
    expect(result.stdout).toBe("line1\n");
    expect(createdTempDirs.length).toBe(0);
  });
});
