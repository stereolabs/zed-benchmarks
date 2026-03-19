#!/bin/bash
# Python Benchmark Runner Wrapper
# Called by scripts/run_benchmarks.py

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Check if pyzed is installed
python3 -c "import pyzed.sl" 2>/dev/null || {
    echo "Error: pyzed not installed!"
    echo "Install with from ZED SDK:"
    echo "  python /usr/local/zed/get_python_api.py"
    exit 1
}

# Run single camera benchmark with all passed arguments
exec python3 "${SCRIPT_DIR}/benchmark_single_cam.py" "$@"
