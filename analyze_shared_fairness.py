#!/usr/bin/env python3
"""
analyze_shared_fairness.py
==========================
Parse the output of run_shared_fairness.py and report, per phase and per client:

  i)   p99 / p99.9 latency
  ii)  refaults (workingset_refault_file delta)  [+ pgscan/pgsteal/pgmajfault]
  iii) cache hits / misses  (derived: misses = bytes read from disk via io.stat;
                             hits = total bytes read by fio - misses)
  iv)  memory consumption per client and total (parent) memory.current
  v)   memory allocated: memory.max of parent and each child cgroup
  vi)  iostat device activity + per-client bandwidth (from fio)
  vii) speed of reading a page from memory (tmpfs) vs IO/disk (O_DIRECT)

Usage:  ./analyze_shared_fairness.py shared_results/
"""
import argparse
import json
import os
import sys


PAGE = 4096  # bytes; page-cache page size


def load(path):
    with open(path) as f:
        return json.load(f)


def fio_read(job_json):
    """Return (iops, bw_KiBs, bytes, p50_us, p99_us, p999_us) for the read side.
       Falls back to the write side for write-only workloads."""
    j = load(job_json)["jobs"][0]
    side = j["read"] if j["read"]["io_bytes"] > 0 else j["write"]
    pct = side.get("clat_ns", {}).get("percentile", {})
    return {
        "iops": side["iops"],
        "bw_kibs": side["bw"],
        "bytes": side["io_bytes"],
        "p50": pct.get("50.000000", 0) / 1000.0,
        "p99": pct.get("99.000000", 0) / 1000.0,
        "p999": pct.get("99.900000", 0) / 1000.0,
    }


def stat_delta(before, after, key):
    b = before.get("memory_stat", {}).get(key, 0)
    a = after.get("memory_stat", {}).get(key, 0)
    return a - b


def io_rbytes(snap):
    return sum(dev.get("rbytes", 0) for dev in snap.get("io_stat", {}).values())


def mib(x):
    return x / (1024 * 1024)


def parse_iostat(path):
    """Average r/s, w/s, rMB/s, wMB/s per device across all samples."""
    if not os.path.exists(path):
        return {}
    devs = {}
    header = None
    with open(path) as f:
        for line in f:
            toks = line.split()
            if not toks:
                continue
            if toks[0] == "Device":
                header = toks
                continue
            if header and toks[0] not in ("avg-cpu:", "Linux") and "%" not in toks[0]:
                # A device row (only if it aligns with the Device header width).
                if len(toks) >= len(header):
                    row = dict(zip(header, toks))
                    d = row["Device"]
                    rec = devs.setdefault(d, {"r/s": [], "w/s": [], "rkB/s": [], "wkB/s": []})
                    for col in ("r/s", "w/s", "rkB/s", "wkB/s"):
                        if col in row:
                            try:
                                rec[col].append(float(row[col]))
                            except ValueError:
                                pass
    out = {}
    for d, rec in devs.items():
        if any(rec.values()):
            avg = {k: (sum(v) / len(v) if v else 0.0) for k, v in rec.items()}
            # skip idle devices
            if avg["r/s"] + avg["w/s"] > 0.5:
                out[d] = avg
    return out


def hr(char="-", n=92):
    return char * n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("results_dir")
    args = ap.parse_args()
    R = args.results_dir.rstrip("/")

    meta = load(f"{R}/run_meta.json")
    nphases = meta["nphases"]

    print("=" * 92)
    print("SHARED-POOL FAIRNESS ANALYSIS")
    print(f"  parent cgroup : {meta['parent_cgroup']}  (memory.max = {meta['parent_mem_max']})")
    print(f"  client1 file  : {meta['files']['client1']}  ({meta['c1_size']})")
    print(f"  client2 file  : {meta['files']['client2']}  ({meta['c2_size']})")
    print(f"  phases        : {nphases}   (no drop_caches between phases)")
    print("=" * 92)

    # ---------------------------------------------------------------- v) allocation
    print("\n[v] MEMORY ALLOCATED (memory.max per cgroup)")
    print(hr())
    s0 = load(f"{R}/phase0_stats.json")
    print(f"  parent  ({meta['parent_cgroup']:<16}) : {s0['before']['parent']['memory_max']}")
    for c in ("client1", "client2"):
        print(f"  {c:<8}({meta['cgroups'][c]:<16}) : {s0['before'][c]['memory_max']}")

    # ---------------------------------------------------------------- per-phase
    for i in range(nphases):
        st = load(f"{R}/phase{i}_stats.json")
        print(f"\n{'#' * 92}\nPHASE {i}   (duration {st['duration_s']}s)\n{'#' * 92}")

        for c in ("client1", "client2"):
            jf = f"{R}/phase{i}_{c}.json"
            if not os.path.exists(jf):
                continue
            r = fio_read(jf)
            b, a = st["before"][c], st["after"][c]

            # ii) refault + reclaim signals
            refault = stat_delta(b, a, "workingset_refault_file")
            pgscan = stat_delta(b, a, "pgscan")
            pgsteal = stat_delta(b, a, "pgsteal")
            pgmajfault = stat_delta(b, a, "pgmajfault")

            # iii) cache hits/misses (bytes read from disk = misses)
            miss_bytes = io_rbytes(a) - io_rbytes(b)
            total_bytes = r["bytes"]
            hit_bytes = max(0, total_bytes - miss_bytes)
            hit_ratio = (100.0 * hit_bytes / total_bytes) if total_bytes else 0.0
            miss_pages = miss_bytes // PAGE

            # iv) memory consumption for this client
            cur = a["memory_current"]

            print(f"\n  --- {c} ---")
            print(f"   i)  latency        : p50={r['p50']:.1f}us  "
                  f"p99={r['p99']:.1f}us  p99.9={r['p999']:.1f}us")
            print(f"   vi) throughput     : {r['iops']:.0f} IOPS   "
                  f"{r['bw_kibs'] / 1024:.1f} MiB/s   (read {mib(total_bytes):.0f} MiB total)")
            print(f"   ii) refaults       : {refault:,}   "
                  f"(pgscan {pgscan:,}  pgsteal {pgsteal:,}  pgmajfault {pgmajfault:,})")
            print(f"   iii) cache hit/miss: hit_ratio={hit_ratio:.1f}%   "
                  f"misses={mib(miss_bytes):.0f} MiB ({miss_pages:,} pages)   "
                  f"hits={mib(hit_bytes):.0f} MiB")
            print(f"   iv) memory.current : {mib(cur):.0f} MiB")

        # iv) total memory consumption (parent)
        pa = st["after"]["parent"]["memory_current"]
        print(f"\n  [iv] TOTAL (parent memory.current) : {mib(pa):.0f} MiB "
              f"/ cap {meta['parent_mem_max']}")

    # ---------------------------------------------------------------- vi) iostat
    print(f"\n{hr('=')}\n[vi] IOSTAT (device averages over the run)")
    print(hr())
    iostat = parse_iostat(f"{R}/iostat.log")
    if iostat:
        print(f"  {'device':<10}{'r/s':>12}{'w/s':>12}{'rMB/s':>12}{'wMB/s':>12}")
        for d, a in sorted(iostat.items()):
            print(f"  {d:<10}{a['r/s']:>12.1f}{a['w/s']:>12.1f}"
                  f"{a['rkB/s'] / 1024:>12.1f}{a['wkB/s'] / 1024:>12.1f}")
    else:
        print("  (no active devices parsed from iostat.log)")

    # ---------------------------------------------------------------- vii) mem vs IO
    print(f"\n{hr('=')}\n[vii] PAGE-READ SPEED: MEMORY vs IO(disk)")
    print(hr())
    try:
        m = fio_read(f"{R}/micro_mem.json")
        io = fio_read(f"{R}/micro_io.json")
        print(f"  {'source':<16}{'IOPS':>12}{'MiB/s':>12}{'p50(us)':>12}{'p99(us)':>12}")
        print(f"  {'memory (tmpfs)':<16}{m['iops']:>12.0f}{m['bw_kibs'] / 1024:>12.1f}"
              f"{m['p50']:>12.2f}{m['p99']:>12.2f}")
        print(f"  {'IO (O_DIRECT)':<16}{io['iops']:>12.0f}{io['bw_kibs'] / 1024:>12.1f}"
              f"{io['p50']:>12.2f}{io['p99']:>12.2f}")
        if io["bw_kibs"] and io["p50"]:
            print(f"  ratio (memory/IO): bandwidth={m['bw_kibs'] / io['bw_kibs']:.0f}x   "
                  f"iops={m['iops'] / io['iops']:.0f}x   "
                  f"per-op latency(p50)={io['p50'] / m['p50']:.0f}x")
    except FileNotFoundError:
        print("  (micro-benchmark files not found)")

    print()


if __name__ == "__main__":
    main()
