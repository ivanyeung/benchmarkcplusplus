# Page Cache Fairness Benchmark — Linux runtime image.
#
# This benchmark targets the LINUX page cache and cgroup v2. Running it in a
# container gives you a real Linux kernel (unlike bare macOS). See the caveats
# in docker-compose.yml about --privileged (needed for /proc/sys/vm/drop_caches)
# and VM memory sizing.
FROM ubuntu:24.04

# Avoid interactive tzdata/apt prompts during build.
ENV DEBIAN_FRONTEND=noninteractive

# fio        — the actual I/O workload driver
# g++/make   — build the C++ benchmark binaries
# sysstat    — provides iostat for per-second I/O monitoring
# python3    — quick_fairness_analysis.py (stdlib only, no pip deps)
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        fio \
        g++ \
        make \
        sysstat \
        python3 \
        sudo \
    && rm -rf /var/lib/apt/lists/*

# The benchmark shells out to `sudo` for every cgroup and drop_caches operation
# (fairness_benchmark.cpp). Inside the container we already run as root, so sudo
# is redundant — but the binary calls it unconditionally. We DON'T rely on the
# apt `sudo` package's config here; running as root, `sudo <cmd>` works with the
# default sudoers (root may run anything). This only matters for the cgroup runs.

WORKDIR /work

# The repo is bind-mounted at runtime (see docker-compose.yml), so we don't COPY
# sources here — that keeps results and test files on the host and avoids stale
# copies. Build happens via the compose command / entrypoint.

# Default: build, then run the concurrent dual-client fairness test without
# cgroups. Override with `docker compose run bench <your command>`.
#
# NOTE: `make clean` first. The repo is bind-mounted, so if you also built on
# the host (e.g. macOS) the shared directory holds host binaries that `make`
# considers up-to-date but that are the wrong architecture/OS for this Linux
# container. A clean build forces correct Linux ELF binaries. It's cheap (~seconds).
CMD ["bash", "-lc", "make clean && make && ./fairness_benchmark --no-cgroup dual"]