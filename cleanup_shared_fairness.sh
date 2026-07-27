#!/usr/bin/env bash
# cleanup_shared_fairness.sh
# ==========================
# Reset host state left behind by run_shared_fairness.py so the next run starts
# from a clean slate. A normal run already tears down its cgroups; this handles
# results, the micro-benchmark tmpfs, stale cgroups (if a run crashed), leftover
# fio processes, and the page cache.
#
# Usage:
#   sudo ./cleanup_shared_fairness.sh [options]
#
# By default the big test files (client1_file/client2_file/micro_io) are KEPT so
# the next run reuses them and starts fast. Pass --delete-files for a full wipe.
#
# Options:
#   -d, --data-dir DIR   data dir holding the test files (default: ./pcf_data)
#   -o, --output DIR     results dir to delete (default: shared_results)
#   --delete-files       also delete client1_file/client2_file/micro_io
#                        (full wipe; the next run recreates them, which is slow)
#   --no-drop-caches     do NOT drop the page cache
#   -h, --help           show this help

set -u

DATA_DIR="./pcf_data"
OUTPUT="shared_results"
KEEP_FILES=1
DROP_CACHES=1
CG="/sys/fs/cgroup"
PARENT="clients"

usage() { sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while [ $# -gt 0 ]; do
    case "$1" in
        -d|--data-dir)   DATA_DIR="$2"; shift 2 ;;
        -o|--output)     OUTPUT="$2"; shift 2 ;;
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

echo "=== Cleaning up shared-fairness run state ==="

# 1. Results / stats from the last run.
if [ -d "$OUTPUT" ]; then
    echo "1. Removing results dir: $OUTPUT/"
    rm -rf "$OUTPUT"
else
    echo "1. No results dir at $OUTPUT/ (skip)"
fi

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

# 4. Stale cgroups (only present if a run crashed before cleanup). Kill any
#    leftover fio still charged to them first, otherwise rmdir reports EBUSY.
if [ -d "$CG/$PARENT" ]; then
    echo "4. Removing stale cgroup tree: $CG/$PARENT"
    pkill -f fairness 2>/dev/null
    pkill fio 2>/dev/null
    sleep 1
    rmdir "$CG/$PARENT/client1" "$CG/$PARENT/client2" "$CG/$PARENT" 2>/dev/null
    if [ -d "$CG/$PARENT" ]; then
        echo "   WARN: $CG/$PARENT still present (processes may still be attached)." >&2
    fi
else
    echo "4. No stale cgroup tree at $CG/$PARENT (skip)"
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
