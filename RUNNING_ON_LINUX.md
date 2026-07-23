# Running the Shared-Pool Fairness Benchmark on Linux

Step-by-step guide for running `run_shared_fairness.py` + `analyze_shared_fairness.py`
on a **bare-metal (or VM) Linux host with cgroup v2**. This is the recommended
environment: unlike Docker Desktop on macOS, a real Linux host has a page cache
that serves cache **hits** in sub-microseconds and cache **misses** at true disk
latency — so the hit/miss and p99 numbers are meaningful.

---

## What these scripts do

`run_shared_fairness.py` puts **client1** and **client2** under one shared parent
cgroup (a shared page-cache pool) and runs the phases from `fairness_configs.ini`
concurrently, capturing per-phase cgroup + fio stats. No `drop_caches` between
phases, so eviction/warming carries across them.

```
/sys/fs/cgroup/clients          <- shared parent, memory.max = --parent-mem
  ├── client1                    <- steady tenant
  └── client2                    <- bursty tenant (evicts client1)
```

`analyze_shared_fairness.py` parses the output and reports, per phase per client:

| # | Metric | Source |
|---|--------|--------|
| i   | p99 / p99.9 latency          | fio `clat_ns.percentile` |
| ii  | refaults (+ pgscan/pgsteal/pgmajfault) | per-cgroup `memory.stat` delta |
| iii | cache hits / misses          | bytes read from disk (`io.stat` rbytes) vs bytes read by fio |
| iv  | memory per client + total    | per-cgroup + parent `memory.current` |
| v   | memory allocated             | `memory.max` of parent and each child |
| vi  | iostat device activity + per-client bandwidth | `iostat -x` + fio `bw` |
| vii | page-read speed: memory vs IO | tmpfs randread vs O_DIRECT device read |

---

## Step 1 — Prerequisites

You need a Linux host with **cgroup v2**, run as **root**, plus a few packages.

```bash
# Debian / Ubuntu
sudo apt-get update && sudo apt-get install -y fio sysstat g++ make python3

# RHEL / Fedora
sudo dnf install -y fio sysstat gcc-c++ make python3
```

- `fio` — the I/O workload driver
- `sysstat` — provides `iostat`
- `python3` — the two scripts (standard library only, no pip installs)
- `g++`, `make` — only if you also want to build the original C++ suite

## Step 2 — Confirm cgroup v2 is active

```bash
stat -fc %T /sys/fs/cgroup        # must print: cgroup2fs
cat /sys/fs/cgroup/cgroup.controllers   # must include: memory io cpu
```

If it prints `tmpfs` (cgroup v1) or the controllers are missing, boot with
`systemd.unified_cgroup_hierarchy=1` (add to the kernel cmdline) and reboot.

## Step 3 — Get the code

```bash
git clone <this-repo>
cd PageCache-Fairness
chmod +x run_shared_fairness.py analyze_shared_fairness.py
# also make the isolated-mode (control condition) scripts executable:
chmod +x run_isolated_fairness.py analyze_isolated_fairness.py
```

## Step 4 — Choose your parameters

The defaults come from `fairness_configs.ini` (client1 = 1G steady reader,
client2 = 32G bursty reader, three 30 s phases). Key overrides:

| Flag | Meaning | Example |
|------|---------|---------|
| `--parent-mem` | shared pool cap (`memory.max` on the parent) | `--parent-mem 2G` |
| `--c1-size` / `--c2-size` | override client file sizes | `--c1-size 2G --c2-size 32G` |
| `--runtime` | per-phase seconds (overrides the ini) | `--runtime 60` |
| `--data-dir` | directory for the test files (created if absent) | `--data-dir /srv/pcf_data` |
| `--cold-drop` | drop caches once before phase 1 (never between) | `--cold-drop` |
| `--skip-build` | skip the automatic `make` step | `--skip-build` |
| `-o` | output directory | `-o shared_results` |

### About `--data-dir` (important)

`--data-dir` is **just a directory**, not a mount. The script does
`os.makedirs()` on it and writes the test files there — it does **not** mount any
device. The default is `./pcf_data` (relative to the current directory), which
works on any box.

To measure a specific disk's page-cache behavior, first mount that disk yourself,
then point `--data-dir` at a path on it. `/mnt/nvme/pcf` in older examples was
**only illustrative** — it assumes you already have an NVMe mounted at `/mnt/nvme`,
which most machines do not. Pick whatever path lives on the filesystem you care
about, e.g.:

```bash
lsblk                        # see your disks/mounts
df -h .                      # what filesystem is the current dir on?
# then, e.g.:
--data-dir /srv/pcf_data     # or ./pcf_data, or /var/tmp/pcf — any writable path
```

Avoid a `tmpfs`/`ramfs` path for `--data-dir` (that's RAM — there'd be no disk
misses to observe).

**Sizing rule of thumb** (this is what makes the interference visible):
- `--c1-size` should be **smaller** than `--parent-mem` so client1 fits in cache
  when the neighbor is quiet.
- `--c2-size` should be **much larger** than `--parent-mem` so client2's scan
  can't be cached and must continuously evict client1.
- Example: `--parent-mem 4G --c1-size 2G --c2-size 32G`.

> ⚠️ **Disk space**: the client files are created with `dd` under `--data-dir`.
> A 32G file needs 32G free. Put `--data-dir` on the disk whose page-cache
> behavior you want to measure (ideally a dedicated device).

## Step 5 — Run the benchmark

```bash
sudo ./run_shared_fairness.py \
    --parent-mem 4G \
    --c1-size 2G --c2-size 32G \
    --runtime 60 \
    --data-dir /srv/pcf_data \
    -o shared_results
```

What happens:
1. **Builds the C++ suite** with `make` (skip with `--skip-build`; a build
   failure is non-fatal since the Python path uses `fio` directly).
2. Creates the `clients` parent + `client1`/`client2` child cgroups and applies
   `memory.max`.
3. Creates the two test files if absent (reused on later runs).
4. Warms client1's file into cache.
5. Runs each phase with both clients concurrent; snapshots `memory.stat`,
   `io.stat`, `memory.current` before/after each phase; saves per-client fio JSON.
6. Runs the memory-vs-IO micro-benchmark.
7. Tears down the cgroups.

Runtime ≈ `nphases × --runtime` plus file creation and ~25 s for the micro-bench.

## Step 6 — Analyze

```bash
./analyze_shared_fairness.py shared_results/
```

This prints the full per-phase report for all seven metrics (see table above).

## Step 7 — Read the results

The interference signature you're looking for, phase by phase, is:

- **Phase 0/2 (neighbor quiet):** client1 hit_ratio high, p99 low (µs), low refaults.
- **Phase 1 (neighbor bursts):** client2's `memory.current` balloons and pushes
  the parent toward `memory.max`; **client1's `memory.current` collapses**,
  its **refaults and pgsteal jump**, hit_ratio drops, and **p99/p99.9 spike**.
- **Metric vii** quantifies the penalty of each of those new misses: the ratio of
  memory bandwidth/latency to disk bandwidth/latency (expect 100×–1000×+ on real
  storage).

That phase-1 p99 spike, tied to client2's eviction of client1, is the core
fairness failure this project targets.

---

## Output files (for your own tooling)

Everything is plain JSON/text in the `-o` directory:

```
shared_results/
├── run_meta.json            # config, cgroup names, memory.max, phase params
├── phaseN_client1.json      # fio JSON (latency percentiles, bw, iops)
├── phaseN_client2.json
├── phaseN_stats.json        # before/after memory.stat + io.stat + memory.current
├── micro_mem.json           # tmpfs (memory) read speed
├── micro_io.json            # O_DIRECT (disk) read speed
└── iostat.log               # iostat -x 1 for the whole run
```

## Troubleshooting

| Symptom | Fix |
|--------|-----|
| `ERROR: must run as root` | run with `sudo`. |
| `ERROR: cgroup v2 not found` | see Step 2 (enable unified hierarchy). |
| `could not write ... memory.max: Permission denied` | your shell is in a delegated subtree; run from a login root shell, or enable delegation. |
| `io.stat` rbytes ≈ total bytes (hit_ratio ~0%) even when cached | you're on an overlay/virtiofs filesystem (e.g. Docker Desktop) that routes cached reads through the block device. Use a real local disk for `--data-dir`. |
| cache never pressured (no eviction) | `--parent-mem` is too large relative to the working sets — shrink it, or grow `--c2-size`. |

## Running the original C++ suite (optional)

The repo also has the original sequential/dual-client benchmark:

```bash
make                                   # builds fairness_benchmark
sudo ./fairness_benchmark --cgroup-config cgroup_shared.ini dual
./quick_fairness_analysis.py fairness_results/
```

The two Python scripts above supersede it for the shared-pool study, adding the
per-phase refault / hit-miss / memory / mem-vs-IO capture.
