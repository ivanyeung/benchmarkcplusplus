#!/usr/bin/env python3
"""
run_isolated_fairness.py
========================
Run the fairness_configs.ini workloads with client1 and client2 in SEPARATE,
fully ISOLATED top-level cgroups (each with its own dedicated, non-reclaimable
memory cap). This is the control condition for the shared-pool experiment
(run_shared_fairness.py): here client2 CANNOT evict client1's pages, because
each tenant's page cache is accounted to its own cgroup and capped independently.

Hierarchy created under cgroup v2 (two INDEPENDENT top-level cgroups, no shared
parent):

    /sys/fs/cgroup/client1_iso     <- steady tenant, memory.max = memory.min = --c1-mem
    /sys/fs/cgroup/client2_iso     <- bursty tenant, memory.max = memory.min = --c2-mem

memory.min == memory.max gives each tenant a guaranteed floor that global reclaim
never touches, so the two caches are walled off from each other. Compare the
per-phase report against the shared run: the shared run shows client1's memory
collapse + p99 spike in the bursty phase; the isolated run should NOT.

Per phase (from the ini's phase_N_* keys) both clients run concurrently, each fio
pinned into its own cgroup. Between phases the page cache is NOT dropped, so
warming/eviction carries across phases (one optional cold drop before phase 1).

For every phase we snapshot, before and after, each cgroup's:
  * memory.current, memory.max, memory.min
  * memory.stat  (workingset_refault_file, pgscan, pgsteal, pgmajfault, file, ...)
  * io.stat      (rbytes -> bytes actually read from disk = cache misses)
and save each client's fio JSON (latency percentiles, bandwidth, iops).

We also run a MEMORY-vs-IO micro-benchmark (tmpfs vs O_DIRECT device read) so the
analyzer can report how fast a page comes from RAM vs disk.

REQUIREMENTS: Linux with cgroup v2, root, and fio + iostat installed. Inside this
repo's Docker setup:  docker compose run --rm bench ./run_isolated_fairness.py
"""
import argparse
import configparser
import json
import os
import re
import shutil
import subprocess
import sys
import time

CG = "/sys/fs/cgroup"
# Two independent top-level cgroups (no shared parent).
CGROUPS = {"client1": "client1_iso", "client2": "client2_iso"}


# --------------------------------------------------------------------------- #
# small helpers  (kept identical to run_shared_fairness.py so the two scripts
# stay drop-in comparable)
# --------------------------------------------------------------------------- #
def write_cg(path, value):
    """Write a value to a cgroup control file, tolerating benign failures."""
    try:
        with open(path, "w") as f:
            f.write(str(value))
        return True
    except OSError as e:
        print(f"  WARN: could not write {value!r} -> {path}: {e}", file=sys.stderr)
        return False


def read_kv(path):
    """Parse a 'key value' style file (memory.stat) into a dict of ints."""
    out = {}
    try:
        with open(path) as f:
            for line in f:
                parts = line.split()
                if len(parts) == 2:
                    try:
                        out[parts[0]] = int(parts[1])
                    except ValueError:
                        out[parts[0]] = parts[1]
    except OSError:
        pass
    return out


def read_io_stat(path):
    """Parse io.stat -> {'MAJ:MIN': {'rbytes': .., 'wbytes': ..}}."""
    out = {}
    try:
        with open(path) as f:
            for line in f:
                toks = line.split()
                if not toks:
                    continue
                dev, fields = toks[0], {}
                for t in toks[1:]:
                    if "=" in t:
                        k, v = t.split("=", 1)
                        try:
                            fields[k] = int(v)
                        except ValueError:
                            pass
                out[dev] = fields
    except OSError:
        pass
    return out


def read_int(path):
    try:
        with open(path) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def read_str(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def snapshot(cg_rel):
    """Full stat snapshot of one cgroup (relative path under /sys/fs/cgroup)."""
    base = f"{CG}/{cg_rel}"
    return {
        "memory_current": read_int(f"{base}/memory.current"),
        "memory_max": read_str(f"{base}/memory.max"),
        "memory_min": read_str(f"{base}/memory.min"),
        "memory_stat": read_kv(f"{base}/memory.stat"),
        "io_stat": read_io_stat(f"{base}/io.stat"),
    }


def drop_caches():
    subprocess.run("sync", shell=True)
    write_cg("/proc/sys/vm/drop_caches", "3")


def build_benchmark():
    """Best-effort `make` so the C++ suite is built alongside the Python run.
       The Python path itself only needs fio, so a build failure is non-fatal."""
    if not os.path.exists("Makefile"):
        print("Build: no Makefile in cwd, skipping.")
        return
    if not shutil.which("make"):
        print("Build: 'make' not found, skipping.")
        return
    print("Build: running 'make' ...")
    r = subprocess.run(["make"], capture_output=True, text=True)
    if r.returncode == 0:
        print("Build: OK")
    else:
        print(f"Build: FAILED (non-fatal, Python path uses fio directly)\n{r.stderr.strip()}",
              file=sys.stderr)


# --------------------------------------------------------------------------- #
# config parsing
# --------------------------------------------------------------------------- #
def parse_phases(cfg, section):
    """Turn phase_N_* keys of a client section into an ordered list of dicts."""
    idxs = set()
    for key in cfg[section]:
        m = re.match(r"phase_(\d+)_", key)
        if m:
            idxs.add(int(m.group(1)))
    phases = []
    for i in sorted(idxs):
        g = lambda k, d=None: cfg[section].get(f"phase_{i}_{k}", d)
        phases.append({
            "numjobs": g("numjobs", "1"),
            "runtime": g("runtime", "30"),
            "pattern": g("pattern", "read"),
            "block_size": g("block_size", "4k"),
            "rate_iops": g("rate_iops"),
            "iodepth": g("iodepth", "16"),
            "ioengine": g("ioengine", "libaio"),
        })
    return phases


def pick_client_sections(cfg):
    c1 = next((s for s in cfg.sections() if s.startswith("client1")), None)
    c2 = next((s for s in cfg.sections() if s.startswith("client2")), None)
    if not c1 or not c2:
        sys.exit("ERROR: config must define client1* and client2* sections")
    return c1, c2


# --------------------------------------------------------------------------- #
# cpuset helper: split available cores into two non-overlapping, dedicated sets
# --------------------------------------------------------------------------- #
def compute_cpu_split():
    """Return (cpus_client1, cpus_client2) as cpuset.cpus strings, or (None, None)
       if there aren't enough cores to give each client a dedicated core."""
    ncpu = os.cpu_count() or 1
    if ncpu < 2:
        print(f"  WARN: only {ncpu} core(s) online; cannot pin two isolated "
              f"clients to dedicated cores. Skipping cpuset pinning.", file=sys.stderr)
        return None, None
    half = ncpu // 2
    c1 = f"0-{half - 1}" if half > 1 else "0"
    c2 = f"{half}-{ncpu - 1}" if (ncpu - 1) > half else f"{half}"
    return c1, c2


# --------------------------------------------------------------------------- #
# cgroup lifecycle
# --------------------------------------------------------------------------- #
def setup_cgroups(mem_caps, pin):
    print("Setting up ISOLATED cgroups (independent top-level pools):")
    controllers = "+memory +io +cpu" + (" +cpuset" if pin else "")
    write_cg(f"{CG}/cgroup.subtree_control", controllers)

    cpu1, cpu2 = (compute_cpu_split() if pin else (None, None))
    cpus = {"client1": cpu1, "client2": cpu2}

    for client, cg in CGROUPS.items():
        base = f"{CG}/{cg}"
        os.makedirs(base, exist_ok=True)
        cap = mem_caps[client]
        # memory.min == memory.max => guaranteed, non-reclaimable floor == the cap.
        write_cg(f"{base}/memory.max", cap)
        write_cg(f"{base}/memory.min", cap)
        write_cg(f"{base}/memory.swap.max", "0")
        write_cg(f"{base}/cpu.weight", "100")
        write_cg(f"{base}/io.weight", "100")
        if pin and cpus[client]:
            write_cg(f"{base}/cpuset.cpus", cpus[client])
        pinned = f"  cpuset.cpus={cpus[client]}" if (pin and cpus[client]) else ""
        print(f"  {cg:<12} memory.max=memory.min={cap}{pinned}")


def cleanup_cgroups():
    for cg in CGROUPS.values():
        try:
            os.rmdir(f"{CG}/{cg}")
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# fio launching
# --------------------------------------------------------------------------- #
def fio_cmd(name, cg, filename, phase, out_json, runtime, size, loops=None):
    parts = [
        "fio", f"--name={name}", f"--filename={filename}", f"--size={size}",
        f"--rw={phase['pattern']}", f"--bs={phase['block_size']}",
        f"--iodepth={phase['iodepth']}", f"--ioengine={phase['ioengine']}",
        f"--numjobs={phase['numjobs']}",
        "--direct=0", "--group_reporting",
        "--output-format=json", f"--output={out_json}",
    ]
    if loops:
        # Fixed number of complete passes over the file; NO wall-clock cap.
        # (--time_based would override --loops, so we omit both it and --runtime.)
        parts.append(f"--loops={loops}")
    else:
        # Time-based: run for `runtime` seconds, looping over the file as needed.
        parts += [f"--runtime={runtime}", "--time_based"]
    if phase.get("rate_iops"):
        parts.append(f"--rate_iops={phase['rate_iops']}")
    fio = " ".join(parts)
    # Move the shell into the target cgroup, then exec fio so all I/O is charged
    # to that cgroup (avoids the PID-migration race).
    return ["bash", "-c",
            f"echo $$ > {CG}/{cg}/cgroup.procs; exec {fio} > /dev/null 2>&1"]


def run_phase(idx, p1, p2, files, sizes, outdir, runtime, loops=None):
    j1 = f"{outdir}/phase{idx}_client1.json"
    j2 = f"{outdir}/phase{idx}_client2.json"

    before = {c: snapshot(CGROUPS[c]) for c in ("client1", "client2")}

    t0 = time.time()
    procs = [
        subprocess.Popen(fio_cmd("client1", CGROUPS["client1"], files["client1"], p1, j1, runtime, sizes["client1"], loops)),
        subprocess.Popen(fio_cmd("client2", CGROUPS["client2"], files["client2"], p2, j2, runtime, sizes["client2"], loops)),
    ]
    for pr in procs:
        pr.wait()
    dur = round(time.time() - t0, 1)

    after = {c: snapshot(CGROUPS[c]) for c in ("client1", "client2")}

    stats = {"phase": idx, "duration_s": dur, "before": before, "after": after}
    with open(f"{outdir}/phase{idx}_stats.json", "w") as f:
        json.dump(stats, f, indent=2)
    mode = f"{loops} loops" if loops else f"{runtime}s"
    print(f"  phase {idx}: done in {dur}s [{mode}]  "
          f"(client1={p1['pattern']}@{p1.get('rate_iops') or 'max'} iops, "
          f"client2={p2['pattern']}@{p2.get('rate_iops') or 'max'} iops)")


# --------------------------------------------------------------------------- #
# memory-vs-IO micro-benchmark (item vii)
# --------------------------------------------------------------------------- #
def micro_mem_vs_io(outdir, data_dir):
    print("Running memory-vs-IO page-read micro-benchmark ...")
    # Memory: randread on a tmpfs (pure RAM) file.
    os.makedirs("/mnt/ram", exist_ok=True)
    subprocess.run("mountpoint -q /mnt/ram || mount -t tmpfs -o size=512m tmpfs /mnt/ram",
                   shell=True)
    subprocess.run("dd if=/dev/zero of=/mnt/ram/f bs=1M count=256 status=none", shell=True)
    subprocess.run(
        "fio --name=mem --filename=/mnt/ram/f --rw=randread --bs=4k --iodepth=32 "
        "--ioengine=libaio --runtime=10 --time_based --direct=0 "
        f"--output-format=json --output={outdir}/micro_mem.json > /dev/null 2>&1",
        shell=True)
    # IO: O_DIRECT randread on a device-backed file (bypasses cache = real disk).
    subprocess.run(f"dd if=/dev/zero of={data_dir}/micro_io bs=1M count=256 status=none",
                   shell=True)
    drop_caches()
    subprocess.run(
        f"fio --name=io --filename={data_dir}/micro_io --rw=randread --bs=4k --iodepth=32 "
        "--ioengine=libaio --runtime=15 --time_based --direct=1 "
        f"--output-format=json --output={outdir}/micro_io.json > /dev/null 2>&1",
        shell=True)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def human_to_bytes(s):
    m = re.match(r"(\d+)([KMGT]?)", s.upper())
    mult = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
    return int(m.group(1)) * mult[m.group(2)]


def ensure_file(path, size_str):
    want = human_to_bytes(size_str)
    if os.path.exists(path) and os.path.getsize(path) >= want:
        print(f"  reuse {path} ({size_str})")
        return
    print(f"  creating {path} ({size_str}) ...")
    mb = max(1, want // (1024 * 1024))
    subprocess.run(f"dd if=/dev/zero of={path} bs=1M count={mb} status=none", shell=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-c", "--config", default="fairness_configs.ini")
    ap.add_argument("-o", "--output", default="isolated_results")
    ap.add_argument("--client-mem", default="256M",
                    help="memory.max (== memory.min) applied to EACH client cgroup "
                         "unless overridden by --c1-mem/--c2-mem (default 256M each, "
                         "so total ~= the shared run's default 512M parent pool)")
    ap.add_argument("--c1-mem", default=None, help="override client1 memory cap (e.g. 256M)")
    ap.add_argument("--c2-mem", default=None, help="override client2 memory cap (e.g. 256M)")
    ap.add_argument("--pin", action="store_true",
                    help="pin each client to a dedicated, non-overlapping set of CPU "
                         "cores (auto-split from the online cores; needs cpuset controller)")
    ap.add_argument("--data-dir", default="./pcf_data",
                    help="directory for client test files; created if absent. Point it at "
                         "the disk whose page-cache behavior you want to measure "
                         "(default ./pcf_data in cwd). NOTE: this does NOT mount anything.")
    ap.add_argument("--skip-build", action="store_true",
                    help="do not run 'make' before the benchmark")
    ap.add_argument("--c1-size", default=None, help="override client1 file size (e.g. 256M)")
    ap.add_argument("--c2-size", default=None, help="override client2 file size (e.g. 8G)")
    ap.add_argument("--runtime", type=int, default=None,
                    help="override per-phase runtime in seconds (time-based mode)")
    ap.add_argument("--loops", type=int, default=None,
                    help="run a FIXED number of complete passes over each file per "
                         "phase instead of time-based looping. Disables --time_based/"
                         "--runtime; rate_iops caps still apply. Phase ends when both "
                         "clients finish their passes. e.g. --loops 3")
    ap.add_argument("--cold-drop", action="store_true",
                    help="drop caches ONCE before phase 1 (never between phases)")
    args = ap.parse_args()

    if os.geteuid() != 0:
        sys.exit("ERROR: must run as root (cgroup + drop_caches access).")
    if not os.path.exists(f"{CG}/cgroup.controllers"):
        sys.exit("ERROR: cgroup v2 not found at /sys/fs/cgroup.")
    for tool in ("fio", "iostat"):
        if not shutil.which(tool):
            sys.exit(f"ERROR: {tool} not installed.")

    if not args.skip_build:
        build_benchmark()

    cfg = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
    cfg.read(args.config)
    c1_sec, c2_sec = pick_client_sections(cfg)
    p1_phases = parse_phases(cfg, c1_sec)
    p2_phases = parse_phases(cfg, c2_sec)
    nphases = max(len(p1_phases), len(p2_phases))
    if nphases == 0:
        sys.exit("ERROR: no phase_N_* definitions found in config.")

    os.makedirs(args.output, exist_ok=True)
    os.makedirs(args.data_dir, exist_ok=True)

    mem_caps = {"client1": args.c1_mem or args.client_mem,
                "client2": args.c2_mem or args.client_mem}

    c1_size = args.c1_size or cfg[c1_sec].get("file_size", "1G")
    c2_size = args.c2_size or cfg[c2_sec].get("file_size", "32G")
    files = {"client1": f"{args.data_dir}/client1_file",
             "client2": f"{args.data_dir}/client2_file"}
    sizes = {"client1": c1_size, "client2": c2_size}
    print("Preparing test files:")
    ensure_file(files["client1"], c1_size)
    ensure_file(files["client2"], c2_size)

    setup_cgroups(mem_caps, args.pin)

    # Persist run metadata for the analyzer.
    meta = {
        "mode": "isolated",
        "config": args.config,
        "cgroups": {"client1": CGROUPS["client1"], "client2": CGROUPS["client2"]},
        "mem_caps": mem_caps,
        "pinned": bool(args.pin),
        "loops": args.loops,
        "files": files, "c1_size": c1_size, "c2_size": c2_size,
        "nphases": nphases,
        "client1_phases": p1_phases, "client2_phases": p2_phases,
        "cold_drop": args.cold_drop,
    }
    with open(f"{args.output}/run_meta.json", "w") as f:
        json.dump(meta, f, indent=2)

    # Warm client1 (so a cache baseline is possible), no drop afterwards unless asked.
    subprocess.run(f"cat {files['client1']} > /dev/null 2>&1", shell=True)
    if args.cold_drop:
        print("Cold drop of page cache before phase 1 (none between phases).")
        drop_caches()

    # Start iostat monitoring for the whole run.
    iostat_log = open(f"{args.output}/iostat.log", "w")
    iostat = subprocess.Popen(["iostat", "-x", "1"], stdout=iostat_log)

    print(f"Running {nphases} phase(s), both clients concurrent, no drop between phases:")
    try:
        for i in range(nphases):
            p1 = p1_phases[min(i, len(p1_phases) - 1)]
            p2 = p2_phases[min(i, len(p2_phases) - 1)]
            rt = args.runtime or int(p1["runtime"])
            run_phase(i, p1, p2, files, sizes, args.output, rt, args.loops)
    finally:
        iostat.terminate()
        iostat.wait()
        iostat_log.close()

    micro_mem_vs_io(args.output, args.data_dir)
    cleanup_cgroups()
    print(f"\nDone. Results in {args.output}/  ->  analyze with:\n"
          f"    ./analyze_isolated_fairness.py {args.output}/")


if __name__ == "__main__":
    main()