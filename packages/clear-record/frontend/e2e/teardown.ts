// End the seeded run owner after the last test (see e2e/run_owner.py).
//
// `seed.py` starts a process that keeps one run genuinely in flight for the
// whole suite and records its pid beside the data directory. Nothing else can
// end it: it is in its own session, so it outlives the seed deliberately (the
// console has to find a live owner at startup). This runs once, after the last
// test, and also drops the pid file so a later run can never signal a pid that
// has since been reused.
import { readFileSync, rmSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { dirname, join } from "node:path";

import { dataDir } from "./paths";

export default function globalTeardown() {
  const pidFile = join(dirname(dataDir), "run-owner.pid");
  let raw: string | null = null;
  try {
    raw = readFileSync(pidFile, "utf8");
  } catch {
    raw = null; // no owner was started: nothing to end
  }
  rmSync(pidFile, { force: true });
  const pid = raw ? Number(raw.trim()) : Number.NaN;
  if (!Number.isInteger(pid) || pid <= 0) return;
  try {
    // Signal only a process that is still the run owner: a pid alone is a
    // promise about the past, and this teardown must not hit an unrelated
    // process that inherited it.
    const command = execFileSync("ps", ["-o", "command=", "-p", String(pid)], {
      encoding: "utf8",
    });
    if (!command.includes("run_owner.py")) return;
    process.kill(pid, "SIGTERM");
  } catch {
    // Already gone, or `ps` is unavailable: the owner also stops on its own
    // when its run stops being its own.
  }
}
