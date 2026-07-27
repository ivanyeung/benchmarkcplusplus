#!/usr/bin/env bash
# cleanup_shared_fairness.sh
# ==========================
# Reset host state left behind by run_shared_fairness.py AND
# run_isolated_fairness.py so the next run starts from a clean slate. A normal
# run tears down its own cgroups; this handles results, the micro-benchmark
# tmpfs, stale cgroups from BOTH modes (shared 'clients' tree + isolated
# 'client1_iso'/'client2_iso'), leftover fio processes, and the page cache.
#
# Usage:
#   sudo ./cleanup_shared_fairness.sh [options]
#
# By default the big test files (client1_file/client2_file/micro_io) are KEPT so
# the next run reuses them and starts fast. Pass --delete-files for a full wipe.
# By default BOTH shared_results/ and isolated_results/ are removed; use -o to
# target a single results dir instead.
#
# Options:
#   -d, --data-dir DIR   data dir holding the test files (default: ./pcf_data)
#   -o, --output DIR     remove only this results dir (default: both
#                        shared_results and isolated_results)
#   --delete-files       also delete client1_file/client2_file/micro_io
#                        (full wipe; the next run recreates them, which is slow)
#   --no-drop-caches     do NOT drop the page cache
#   -h, --help           show this help

set -u

DATA_DIR="./pcf_data"
RESULT_DIRS="shared_results isolated_results"
KEEP_FILES=1
DROP_CACHES=1
CG="/sys/fs/cgroup"
SHARED_PARENT="clients"                       # run_shared_fairness.py hierarchy
ISO_CGROUPS="client1_iso client2_iso"         # run_isolated_fairness.py cgroups

usage() { sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while [ $# -gt 0 ]; do
    case "$1" in
        -d|--data-dir)   DATA_DIR="$2"; shift 2 ;;
        -o|--output)     RESULT_DIRS="$2"; shift 2 ;;
        --delete-files)  KEEP_FILES=0; shift ;;
        --no-drop-caches) DROP_CACHES=0; shift ;;
        -h|--help)       usage 0 ;;
        *) echo "Unknown option: $1" >&2; usage 1 ;;
    esac
done

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: must run as root (cgroup + drop_caches access). Use sudo." >&2
    exit 1
fi

echo "=== Cleaning up fairness run state (shared + isolated) ==="

# 1. Results / stats from the last run(s).
for dir in $RESULT_DIRS; do
    if [ -d "$dir" ]; then
        echo "1. Removing results dir: $dir/"
        rm -rf "$dir"
    else
        echo "1. No results dir at $dir/ (skip)"
    fi
done

# 2. Test data files (kept by default so the next run reuses them = faster).
if [ "$KEEP_FILES" -eq 1 ]; then
    echo "2. Keeping test files in $DATA_DIR/ (pass --delete-files for a full wipe)"
else
    echo "2. Removing test files in $DATA_DIR/ (--delete-files)"
    rm -f "$DATA_DIR/client1_file" "$DATA_DIR/client2_file" "$DATA_DIR/micro_io"
fi

# 3. tmpfs the micro-benchmark mounted at /mnt/ram (never unmounted by the run).
if mountpoint -q /mnt/ram; then
    echo "3. Unmounting /mnt/ram tmpfs"
    umount /mnt/ram 2>/dev/null && rmdir /mnt/ram 2>/dev/null
else
    echo "3. /mnt/ram not mounted (skip)"
    rmdir /mnt/ram 2>/dev/null
fi

# 4. Stale cgroups from BOTH modes (only present if a run crashed before its own
#    cleanup). Kill any leftover fio still charged to them first, otherwise
#    rmdir reports EBUSY.
STALE=0
[ -d "$CG/$SHARED_PARENT" ] && STALE=1
for cg in $ISO_CGROUPS; do [ -d "$CG/$cg" ] && STALE=1; done

if [ "$STALE" -eq 1 ]; then
    echo "4. Removing stale cgroups (killing leftover fio first)"
    pkill -f fairness 2>/dev/null
    pkill fio 2>/dev/null
    sleep 1
    # shared hierarchy: children then parent
    rmdir "$CG/$SHARED_PARENT/client1" "$CG/$SHARED_PARENT/client2" "$CG/$SHARED_PARENT" 2>/dev/null
    # isolated: independent top-level cgroups
    for cg in $ISO_CGROUPS; do rmdir "$CG/$cg" 2>/dev/null; done
    for leftover in "$CG/$SHARED_PARENT" $ISO_CGROUPS; do
        p="$leftover"; case "$p" in "$CG"/*) : ;; *) p="$CG/$leftover" ;; esac
        [ -d "$p" ] && echo "   WARN: $p still present (processes may still be attached)." >&2
    done
else
    echo "4. No stale cgroups from either mode (skip)"
fi

# 5. Page cache (not required if the next run uses --cold-drop).
if [ "$DROP_CACHES" -eq 1 ]; then
    echo "5. Dropping page cache"
    sync
    echo 3 > /proc/sys/vm/drop_caches
else
    echo "5. Leaving page cache intact (--no-drop-caches)"
fi

echo "=== Done. Ready for the next run. ==="
