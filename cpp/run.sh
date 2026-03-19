#!/bin/bash
# C++ Benchmark Runner Wrapper
# Called by scripts/run_benchmarks.py

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="${SCRIPT_DIR}/build"

# Check if built
if [ ! -f "${BUILD_DIR}/benchmark_single_cam" ]; then
    echo "Error: C++ benchmarks not built!"
    echo "Build with:"
    echo "  cd ${SCRIPT_DIR}"
    echo "  mkdir -p build && cd build && cmake .. && make -j\$(nproc)"
    exit 1
fi

# Run single camera benchmark with all passed arguments
exec "${BUILD_DIR}/benchmark_single_cam" "$@"
