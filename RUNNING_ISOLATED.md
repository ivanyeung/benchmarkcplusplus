# Runbook — Isolated-Mode Fairness Benchmark (Linux)

Step-by-step guide for running `run_isolated_fairness.py` + `analyze_isolated_fairness.py`
on a **Linux host with cgroup v2**, as **root**.

This is the **control condition** for the shared-pool study. Each client runs in
its **own** top-level cgroup with a dedicated, non-reclaimable memory cap, so
client2 (the bursty scanner) **cannot** evict client1's pages. Run it and compare
against `run_shared_fairness.py`: the shared run shows client1's memory collapse +
p99 spike when client2 bursts; the isolated run should **not**. That contrast is
the fairness result.

```
/sys/fs/cgroup/client1_iso     <- steady tenant,  memory.max = memory.min = --c1-mem
/sys/fs/cgroup/client2_iso     <- bursty tenant,  memory.max = memory.min = --c2-mem
   (two INDEPENDENT top-level cgroups — no shared parent)
```

---

## Step 1 — Prerequisites

```bash
# Debian / Ubuntu
sudo apt-get update && sudo apt-get install -y fio sysstat g++ make python3
# RHEL / Fedora
sudo dnf install -y fio sysstat gcc-c++ make python3
```

- `fio` — the I/O workload driver
- `sysstat` — provides `iostat`
- `python3` — the two scripts (standard library only, no pip installs)

## Step 2 — Confirm cgroup v2 is active

```bash
stat -fc %T /sys/fs/cgroup                 # must print: cgroup2fs
cat /sys/fs/cgroup/cgroup.controllers      # must include: memory io cpu
```

If it prints `tmpfs` (cgroup v1), boot with `systemd.unified_cgroup_hierarchy=1`
on the kernel cmdline and reboot.

> **`--pin` only:** if you want per-client CPU pinning, `cpuset` must also be
> available. `cat /sys/fs/cgroup/cgroup.controllers` should list `cpuset`. Pinning
> is **off by default** — skip this unless you pass `--pin`.

## Step 3 — Get the code & make it executable

```bash
cd <this-repo>
chmod +x run_isolated_fairness.py analyze_isolated_fairness.py
```

## Step 4 — Choose parameters

Defaults come from `fairness_configs.ini` (client1 = 1G steady reader,
client2 = 32G bursty reader, three 30 s phases).

| Flag | Meaning | Example |
|------|---------|---------|
| `--client-mem` | memory cap (== floor) applied to **each** client | `--client-mem 256M` |
| `--c1-mem` / `--c2-mem` | override the cap per client | `--c1-mem 256M --c2-mem 256M` |
| `--pin` | give each client its own dedicated CPU cores (auto-split) | `--pin` |
| `--c1-size` / `--c2-size` | override client file sizes | `--c1-size 2G --c2-size 32G` |
| `--runtime` | per-phase seconds (overrides the ini) | `--runtime 60` |
| `--data-dir` | directory for the test files (created if absent) | `--data-dir /srv/pcf_data` |
| `--cold-drop` | drop caches once before phase 1 (never between) | `--cold-drop` |
| `--skip-build` | skip the automatic `make` step | `--skip-build` |
| `-o` | output directory | `-o isolated_results` |

**Sizing rule of thumb — keep it comparable to the shared run.** In the shared run
one parent pool (`--parent-mem`) is shared by both. Here you split that same total
across two isolated caps, so use **half each**:

- shared: `--parent-mem 512M`  ⇔  isolated: `--client-mem 256M` (256M + 256M = 512M)
- shared: `--parent-mem 4G`     ⇔  isolated: `--c1-mem 2G --c2-mem 2G`

Then, so the isolation is actually *visible*:
- `--c1-size` should be **≤ client1's cap** (client1 fits in its own cache).
- `--c2-size` should be **much larger** than client2's cap (client2 thrashes its
  own cache — but, unlike the shared run, it can only evict *itself*, never client1).

> ⚠️ **Disk space**: files are created with `dd` under `--data-dir`; a 32G file
> needs 32G free. Put `--data-dir` on a **real local disk** (not tmpfs/overlay/
> virtiofs) or the hit/miss numbers are meaningless.

## Step 5 — Run

```bash
sudo ./run_isolated_fairness.py \
    --c1-mem 256M --c2-mem 256M \
    --c1-size 1G --c2-size 32G \
    --runtime 60 \
    --data-dir /srv/pcf_data \
    -o isolated_results
```

Add `--pin` if you also want dedicated cores per client.

What happens:
1. Best-effort `make` (skip with `--skip-build`; a build failure is non-fatal —
   the Python path uses `fio` directly).
2. Creates the two **independent** cgroups `client1_iso` / `client2_iso` and applies
   `memory.max` == `memory.min` (the dedicated, non-reclaimable cap) to each.
3. Creates the two test files if absent (reused on later runs).
4. Warms client1's file into cache.
5. Runs each phase with both clients concurrent; snapshots `memory.stat`,
   `io.stat`, `memory.current`/`memory.max`/`memory.min` before/after each phase;
   saves per-client fio JSON. **No `drop_caches` between phases.**
6. Runs the memory-vs-IO micro-benchmark.
7. Tears down the cgroups.

Runtime ≈ `nphases × --runtime` + file creation + ~25 s micro-bench.

## Step 6 — Analyze

```bash
./analyze_isolated_fairness.py isolated_results/
```

Prints, per phase per client: (i) p50/p99/p99.9 latency, (ii) refaults +
pgscan/pgsteal/pgmajfault, (iii) cache hit/miss (from `io.stat` rbytes), (iv)
memory.current per client + total, (v) memory.max/min allocation, (vi) iostat +
per-client bandwidth, (vii) memory-vs-disk page-read speed.

## Step 7 — Read the results (isolated vs shared)

The signature you're confirming in **isolated** mode, phase by phase:

- **Every phase (including client2's burst phase):** client1's `p99` stays **low
  and flat**, its `hit_ratio` stays **high**, its refaults/pgsteal stay **near
  zero**, and its `memory.current` **holds steady** at its cap.
- **client2** thrashes its own capped pool (high refaults/misses on client2) — but
  that damage is contained to client2.

Compare with `analyze_shared_fairness.py` on a shared run of the same sizes: there,
client1's phase-1 numbers **degrade** (p99 spikes, memory collapses, refaults jump)
because client2 evicts it. **Isolated = flat client1; shared = client1 spikes.**
That delta is the noisy-neighbor / fairness failure the project targets.

Run both back-to-back for the side-by-side:

```bash
sudo ./run_shared_fairness.py   --parent-mem 512M --c1-size 1G --c2-size 32G --runtime 60 -o shared_results
sudo ./run_isolated_fairness.py --c1-mem 256M --c2-mem 256M --c1-size 1G --c2-size 32G --runtime 60 -o isolated_results
./analyze_shared_fairness.py   shared_results/
./analyze_isolated_fairness.py isolated_results/
```

---

## Output files

```
isolated_results/
├── run_meta.json            # mode, cgroup names, per-client caps, sizes, phase params
├── phaseN_client1.json      # fio JSON (latency percentiles, bw, iops)
├── phaseN_client2.json
├── phaseN_stats.json        # before/after memory.stat + io.stat + memory.current/max/min
├── micro_mem.json           # tmpfs (memory) read speed
├── micro_io.json            # O_DIRECT (disk) read speed
└── iostat.log               # iostat -x 1 for the whole run
```

## Troubleshooting

| Symptom | Fix |
|--------|-----|
| `ERROR: must run as root` | run with `sudo`. |
| `ERROR: cgroup v2 not found` | see Step 2 (enable unified hierarchy). |
| `WARN: could not write ... cpuset.cpus` | `cpuset` controller not enabled, or too few cores — drop `--pin` or enable cpuset. |
| `WARN: only N core(s) online ... skipping cpuset` | box has <2 cores; pinning skipped automatically (run continues unpinned). |
| client1 **also** degrades (looks like the shared run) | your caps are too small for client1's working set, so client1 evicts *itself*. Raise `--c1-mem` to ≥ `--c1-size`, or shrink `--c1-size`. |
| `io.stat` rbytes ≈ total bytes (hit_ratio ~0%) even when cached | you're on overlay/virtiofs (e.g. Docker Desktop). Use a real local disk for `--data-dir`. |
| cgroup won't delete on cleanup | a stray process is still in it: `cat /sys/fs/cgroup/client1_iso/cgroup.procs`, kill it, rerun. |
