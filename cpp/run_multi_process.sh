#!/bin/bash

# Multi-Process Benchmark Runner (C++)
# This script runs multiple instances of the C++ single camera benchmark in parallel

set -e

# Color codes for better UI
RED=$'\033[0;31m'
GREEN=$'\033[0;32m'
YELLOW=$'\033[1;33m'
BLUE=$'\033[0;34m'
CYAN=$'\033[0;36m'
NC=$'\033[0m' # No Color
BOLD=$'\033[1m'

# Default values
NUM_STEREO=1
NUM_MONO=0
WARMUP=5
DURATION=60
SVO_INPUT=""
CAMERA_FPS=30
RESOLUTION="HD1080"
DEPTH_MODE=""
OD_MODEL=""
OD_CUSTOM_ONNX_FILE=""
OD_CUSTOM_ONNX_INPUT_SHAPE=""
BT_MODEL=""
COMPRESSION_RECORDING=""
COMPRESSION_STREAMING=""
POSITIONAL_TRACKING=""
GLASS_TO_GLASS=""
ENCRYPTION_KEY=""
ENCODING_PRESET=""
LOG_DIR="./benchmark_logs"
CLEANUP_LOGS=false
STAGGER_START=5 # Seconds to wait between starting processes

# Help function
show_help() {
    cat << EOF
${BOLD}Multi-Process ZED Benchmark Runner (C++)${NC}

${BOLD}USAGE:${NC}
    $0 [OPTIONS]

${BOLD}OPTIONS:${NC}
    ${CYAN}--num_stereo${NC} <number>
        Number of stereo (ZED X) cameras to use (default: 1)
        Example: --num_stereo 4

    ${CYAN}--num_mono${NC} <number>
        Number of mono (ZED X One) cameras to use (default: 0)
        Example: --num_mono 4

    ${CYAN}-d, --duration${NC} <seconds>
        Duration of each benchmark in seconds (default: 60)
        Use -1 for infinite duration
        Example: -d 120

    ${CYAN}-w, --warmup${NC} <seconds>
        Warmup duration in seconds (default: 5)
        Example: -w 10

    ${CYAN}--svo-input${NC} <filename>
        Input SVO filename (default: empty, meaning live camera)
        Example: --svo-input input.svo

    ${CYAN}--camera_fps${NC} <fps>
        Camera FPS (e.g. 15, 30, 60) (default: 30)
        Example: --camera_fps 60

    ${CYAN}--resolution${NC} <resolution>
        Resolution (e.g. HD1200) (default: HD1080)
        Example: --resolution HD720

    ${CYAN}--depth_mode${NC} <mode>
        Depth Mode (e.g. NEURAL_LIGHT). If set, activates depth.
        Example: --depth_mode NEURAL

    ${CYAN}--od_model${NC} <model>
        Object Detection Model (e.g. MULTI_CLASS_BOX_FAST). If set, activates OD.
        Example: --od_model MULTI_CLASS_BOX_FAST

    ${CYAN}--od_custom_onnx_file${NC} <path>
        Path to custom ONNX file (requires od_model=CUSTOM_YOLOLIKE_BOX_OBJECTS).
        Example: --od_custom_onnx_file /path/to/model.onnx

    ${CYAN}--od_custom_onnx_input_shape${NC} <WxH>
        Input resolution for custom ONNX (e.g. 640x640, default: 512x512).
        Example: --od_custom_onnx_input_shape 1024x1024

    ${CYAN}--bt_model${NC} <model>
        Body Tracking Model (e.g. HUMAN_BODY_FAST). If set, activates BT.
        Example: --bt_model HUMAN_BODY_FAST

    ${CYAN}--compression_mode_recording${NC} <compression>
        Compression Mode for Recording (e.g. H265). If set, activates recording.
        Example: --compression_mode_recording H265

    ${CYAN}--encoding_preset${NC} <preset>
        Encoding preset for recording: ULTRAFAST, FAST, MEDIUM, SLOW, DEFAULT.
        Example: --encoding_preset ULTRAFAST

    ${CYAN}--compression_mode_streaming${NC} <compression>
        Compression Mode for Streaming (e.g. H265). If set, activates streaming.
        Example: --compression_mode_streaming H265

    ${CYAN}--positional-tracking${NC}
        Activates positional tracking.

    ${CYAN}--glass_to_glass_latency${NC}
        Measure glass-to-glass latency (screen-to-camera round-trip) on every camera.

    ${CYAN}--log-dir${NC} <directory>
        Directory for log files (default: ./benchmark_logs)

    ${CYAN}--benchmark${NC} <filename>
        Base filename for benchmark JSON report (e.g. output.json -> output_process_1.json)

    ${CYAN}--cleanup-logs${NC}
        Remove logs after run

    ${CYAN}--stagger-start${NC} <seconds>
        Seconds to wait between starting processes (default: 5)

    ${CYAN}-h, --help${NC}
        Show this help message

EOF
}

# Compile function
compile_code() {
    echo -e "${BLUE}Compiling benchmark code...${NC}"
    if [ ! -d "$SCRIPT_DIR/build" ]; then
        mkdir -p "$SCRIPT_DIR/build"
    fi
    
    pushd "$SCRIPT_DIR/build" > /dev/null
    cmake -DCMAKE_BUILD_TYPE=Release ..
    make -j$(nproc)
    if [ $? -ne 0 ]; then
         echo -e "${RED}Compilation failed.${NC}"
         exit 1
    fi
    popd > /dev/null
    echo -e "${GREEN}Compilation successful.${NC}"
}

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        -d|--duration)
            DURATION="$2"
            shift 2
            ;;
        -w|--warmup)
            WARMUP="$2"
            shift 2
            ;;
        --svo-input|--svo_input)
            SVO_INPUT="$2"
            shift 2
            ;;
        --camera_fps)
            CAMERA_FPS="$2"
            shift 2
            ;;
        --resolution)
            RESOLUTION="$2"
            shift 2
            ;;
        --depth_mode)
            DEPTH_MODE="$2"
            shift 2
            ;;
        --od_model)
            OD_MODEL="$2"
            shift 2
            ;;
        --od_custom_onnx_file)
            OD_CUSTOM_ONNX_FILE="$2"
            shift 2
            ;;
        --od_custom_onnx_input_shape)
            OD_CUSTOM_ONNX_INPUT_SHAPE="$2"
            shift 2
            ;;
        --bt_model)
            BT_MODEL="$2"
            shift 2
            ;;
        --compression_mode_recording)
            COMPRESSION_RECORDING="$2"
            shift 2
            ;;
        --encoding_preset)
            ENCODING_PRESET="$2"
            shift 2
            ;;
        --compression_mode_streaming)
            COMPRESSION_STREAMING="$2"
            shift 2
            ;;
        --positional-tracking|--enable_positional_tracking)
            POSITIONAL_TRACKING="--enable_positional_tracking"
            shift
            ;;
        --glass_to_glass_latency)
            GLASS_TO_GLASS="--glass_to_glass_latency"
            shift
            ;;
        --encryption_key)
            ENCRYPTION_KEY="$2"
            shift 2
            ;;
        --num_stereo)
            NUM_STEREO="$2"
            shift 2
            ;;
        --num_mono)
            NUM_MONO="$2"
            shift 2
            ;;
        --log-dir)
            LOG_DIR="$2"
            shift 2
            ;;
        --benchmark)
            BENCHMARK_JSON="$2"
            shift 2
            ;;
        --cleanup-logs)
            CLEANUP_LOGS=true
            shift
            ;;
        --stagger-start)
            STAGGER_START="$2"
            shift 2
            ;;
        -h|--help)
            show_help
            exit 0
            ;;
        *)
            echo -e "${RED}Error: Unknown option: $1${NC}"
            show_help
            exit 1
            ;;
    esac
done

# Get script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Compile the code
compile_code

# Locate executable
if [ -f "$SCRIPT_DIR/build/benchmark_single_cam" ]; then
    BENCHMARK_EXECUTABLE="$SCRIPT_DIR/build/benchmark_single_cam"
elif [ -f "$SCRIPT_DIR/benchmark_single_cam" ]; then
    BENCHMARK_EXECUTABLE="$SCRIPT_DIR/benchmark_single_cam"
else
    echo -e "${RED}Error: benchmark_single_cam executable not found.${NC}"
    echo "Please build the project first:"
    echo "  mkdir build && cd build && cmake .. && make"
    exit 1
fi

# Create log directory
mkdir -p "$LOG_DIR"
LOG_DIR="$(cd "$LOG_DIR" && pwd)"

# ─── Derive run parameters from num_stereo / num_mono ───────────────────────
NUM_PROCESSES=$((NUM_STEREO + NUM_MONO))
HYBRID_MODE=false
CAMERA_ONE=""

if [ "$NUM_STEREO" -gt 0 ] && [ "$NUM_MONO" -gt 0 ]; then
    HYBRID_MODE=true
elif [ "$NUM_STEREO" -eq 0 ] && [ "$NUM_MONO" -gt 0 ]; then
    CAMERA_ONE="--camera_one"
fi

if [ "$NUM_PROCESSES" -lt 1 ]; then
    echo -e "${RED}Error: num_stereo + num_mono must be at least 1${NC}"
    exit 1
fi

# ─── Camera discovery ───────────────────────────────────────────────────────
# Detect connected ZED cameras by serial number so each process targets a
# specific camera instead of racing for "first available".
# Try Python (pyzed) first; fall back to the C++ list_cameras binary.
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LIST_CAMERAS_SCRIPT="$REPO_ROOT/scripts/list_zed_cameras.py"
LIST_CAMERAS_BIN="$SCRIPT_DIR/build/list_cameras"

# Helper: run the best available camera lister with the given flags.
# Tries Python first, then the compiled C++ binary.
run_list_cameras() {
    local flags="$*"
    # Attempt Python lister (requires pyzed)
    if [ -f "$LIST_CAMERAS_SCRIPT" ] && python3 -c 'import pyzed.sl' 2>/dev/null; then
        python3 "$LIST_CAMERAS_SCRIPT" $flags 2>/dev/null
        return $?
    fi
    # Fall back to compiled C++ lister
    if [ -x "$LIST_CAMERAS_BIN" ]; then
        "$LIST_CAMERAS_BIN" $flags 2>/dev/null
        return $?
    fi
    return 1
}

declare -a STEREO_SERIALS=()
declare -a MONO_SERIALS=()
declare -a CAMERA_SERIALS=()

if [ -z "$SVO_INPUT" ]; then
    echo -e "${BLUE}Detecting connected ZED cameras...${NC}"

    if [ "$HYBRID_MODE" = true ]; then
        # Discover stereo and mono cameras separately
        while IFS= read -r serial; do
            STEREO_SERIALS+=("$serial")
        done < <(run_list_cameras --stereo-only)

        while IFS= read -r serial; do
            MONO_SERIALS+=("$serial")
        done < <(run_list_cameras --mono-only)

        echo -e "${GREEN}Found ${#STEREO_SERIALS[@]} stereo camera(s): ${STEREO_SERIALS[*]}${NC}"
        echo -e "${GREEN}Found ${#MONO_SERIALS[@]} mono camera(s): ${MONO_SERIALS[*]}${NC}"

        if [ "$NUM_STEREO" -gt "${#STEREO_SERIALS[@]}" ]; then
            echo -e "${YELLOW}Warning: Requested $NUM_STEREO stereo but only ${#STEREO_SERIALS[@]} found.${NC}"
            NUM_STEREO=${#STEREO_SERIALS[@]}
        fi
        if [ "$NUM_MONO" -gt "${#MONO_SERIALS[@]}" ]; then
            echo -e "${YELLOW}Warning: Requested $NUM_MONO mono but only ${#MONO_SERIALS[@]} found.${NC}"
            NUM_MONO=${#MONO_SERIALS[@]}
        fi
        NUM_PROCESSES=$((NUM_STEREO + NUM_MONO))

        # Build combined serial array: stereo first, then mono
        CAMERA_SERIALS=("${STEREO_SERIALS[@]:0:$NUM_STEREO}" "${MONO_SERIALS[@]:0:$NUM_MONO}")
    else
        # Single camera type: filter by stereo vs mono
        CAMERA_TYPE_FLAG="--stereo-only"
        if [ "$NUM_MONO" -gt 0 ]; then
            CAMERA_TYPE_FLAG="--mono-only"
        fi
        while IFS= read -r serial; do
            CAMERA_SERIALS+=("$serial")
        done < <(run_list_cameras $CAMERA_TYPE_FLAG)

        if [ ${#CAMERA_SERIALS[@]} -eq 0 ]; then
            echo -e "${RED}Error: No ZED cameras detected.${NC}"
            echo -e "${RED}Ensure cameras are connected and either pyzed is installed or the C++ list_cameras binary is built.${NC}"
            exit 1
        fi

        echo -e "${GREEN}Found ${#CAMERA_SERIALS[@]} camera(s): ${CAMERA_SERIALS[*]}${NC}"

        if [ "$NUM_PROCESSES" -gt "${#CAMERA_SERIALS[@]}" ]; then
            echo -e "${YELLOW}Warning: Requested $NUM_PROCESSES processes but only ${#CAMERA_SERIALS[@]} camera(s) found.${NC}"
            echo -e "${YELLOW}Limiting to ${#CAMERA_SERIALS[@]} process(es).${NC}"
            NUM_PROCESSES=${#CAMERA_SERIALS[@]}
        fi
    fi
fi

# Array to store PIDs
declare -a PIDS=()

# Cleanup function
cleanup() {
    echo -e "\n${YELLOW}Stopping all processes...${NC}"
    for pid in "${PIDS[@]}"; do
        kill -SIGINT "$pid" 2>/dev/null || true
    done
    sleep 2
    for pid in "${PIDS[@]}"; do
        kill -9 "$pid" 2>/dev/null || true
    done
    [ -n "$SYNC_DIR" ] && rm -rf "$SYNC_DIR"
    echo -e "${GREEN}Stopped.${NC}"
    show_summary
}

trap cleanup EXIT INT TERM

# Summary function
show_summary() {
    echo ""
    echo -e "${BOLD}BENCHMARK SUMMARY${NC}"
    for i in $(seq 1 $NUM_PROCESSES); do
        log_file="$LOG_DIR/process_$i.log"
        if [ -f "$log_file" ]; then
            echo -e "${GREEN}Process $i:${NC}"
            if grep -q "Performance Report" "$log_file"; then
                sed -n '/Performance Report/,$p' "$log_file"
            else
                final_fps=$(grep "Final FPS:" "$log_file" | tail -1 | awk '{print $3}')
                if [ -n "$final_fps" ]; then
                    echo -e "Final FPS = ${BOLD}$final_fps${NC}"
                else
                    echo -e "${YELLOW}No performance data available.${NC}"
                fi
            fi
            echo ""
        else
            echo -e "${YELLOW}Process $i:${NC} Log file not found."
            echo ""
        fi
    done
    if [ "$CLEANUP_LOGS" = true ]; then
        rm -rf "$LOG_DIR"
        echo -e "${YELLOW}Logs removed.${NC}"
    fi
}

# Build args (common args shared across all processes)
# Populates the global CMD_ARGS array (an array, not a string, so values containing spaces: e.g. an
# SVO path or report filename: are passed as single argv entries instead of being word-split).
build_args() {
    CMD_ARGS=(--camera_fps "$CAMERA_FPS" --resolution "$RESOLUTION" --duration "$DURATION")
    [ -n "$WARMUP" ] && CMD_ARGS+=(--warmup "$WARMUP")
    [ -n "$SVO_INPUT" ] && CMD_ARGS+=(--svo_input "$SVO_INPUT")
    [ -n "$COMPRESSION_RECORDING" ] && CMD_ARGS+=(--compression_mode_recording "$COMPRESSION_RECORDING")
    [ -n "$ENCODING_PRESET" ] && CMD_ARGS+=(--encoding_preset "$ENCODING_PRESET")
    [ -n "$COMPRESSION_STREAMING" ] && CMD_ARGS+=(--compression_mode_streaming "$COMPRESSION_STREAMING")
    [ -n "$POSITIONAL_TRACKING" ] && CMD_ARGS+=("$POSITIONAL_TRACKING")
    # Glass-to-glass latency (on-screen pattern -> capture -> detection) applies to every process.
    [ -n "$GLASS_TO_GLASS" ] && CMD_ARGS+=("$GLASS_TO_GLASS")
    [ -n "$ENCRYPTION_KEY" ] && CMD_ARGS+=(--encryption_key "$ENCRYPTION_KEY")
    # In hybrid mode, --camera_one and --depth_mode are per-process (see launch loop)
    if [ "$HYBRID_MODE" = false ]; then
        [ -n "$DEPTH_MODE" ] && CMD_ARGS+=(--depth_mode "$DEPTH_MODE")
        [ -n "$OD_MODEL" ] && CMD_ARGS+=(--od_model "$OD_MODEL")
        [ -n "$OD_CUSTOM_ONNX_FILE" ] && CMD_ARGS+=(--od_custom_onnx_file "$OD_CUSTOM_ONNX_FILE")
        [ -n "$OD_CUSTOM_ONNX_INPUT_SHAPE" ] && CMD_ARGS+=(--od_custom_onnx_input_shape "$OD_CUSTOM_ONNX_INPUT_SHAPE")
        [ -n "$BT_MODEL" ] && CMD_ARGS+=(--bt_model "$BT_MODEL")
        [ -n "$CAMERA_ONE" ] && CMD_ARGS+=("$CAMERA_ONE")
    fi
    # The trailing [ -n ... ] tests above may leave $? != 0; under `set -e` a bare
    # build_args call would then abort the whole script. Force a success status.
    return 0
}

build_args

# ─── Barrier sync setup ─────────────────────────────────────────────────────
SYNC_DIR=$(mktemp -d /tmp/zed_barrier_sync.XXXXXX)
echo -e "${BLUE}Barrier sync directory: $SYNC_DIR${NC}"

echo -e "${GREEN}Starting $NUM_PROCESSES processes with:${NC} ${CMD_ARGS[*]}"

for i in $(seq 1 $NUM_PROCESSES); do
    log_file="$LOG_DIR/process_$i.log"
    > "$log_file"

    PROCESS_ARGS=("${CMD_ARGS[@]}")
    if [ -n "$COMPRESSION_RECORDING" ]; then
        PROCESS_ARGS+=(--output_svo "benchmark_recording_process_$i.svo")
    fi

    # Assign a specific camera by serial number (avoids race for "first available")
    if [ ${#CAMERA_SERIALS[@]} -gt 0 ]; then
        PROCESS_ARGS+=(--serial_number "${CAMERA_SERIALS[$((i-1))]}")
    fi

    # Hybrid mode: add per-process flags based on camera type
    if [ "$HYBRID_MODE" = true ]; then
        if [ "$i" -le "$NUM_STEREO" ]; then
            # Stereo process: depth/OD/BT apply to stereo cameras
            [ -n "$DEPTH_MODE" ] && PROCESS_ARGS+=(--depth_mode "$DEPTH_MODE")
            [ -n "$OD_MODEL" ] && PROCESS_ARGS+=(--od_model "$OD_MODEL")
            [ -n "$OD_CUSTOM_ONNX_FILE" ] && PROCESS_ARGS+=(--od_custom_onnx_file "$OD_CUSTOM_ONNX_FILE")
            [ -n "$OD_CUSTOM_ONNX_INPUT_SHAPE" ] && PROCESS_ARGS+=(--od_custom_onnx_input_shape "$OD_CUSTOM_ONNX_INPUT_SHAPE")
            [ -n "$BT_MODEL" ] && PROCESS_ARGS+=(--bt_model "$BT_MODEL")
        else
            # Mono process
            PROCESS_ARGS+=(--camera_one)
        fi
    fi

    # Assign a unique streaming port per process (avoids port conflict)
    if [ -n "$COMPRESSION_STREAMING" ]; then
        PROCESS_ARGS+=(--streaming_port "$((30000 + (i - 1) * 2))")
    fi

    # Handle Benchmark JSON Output
    if [ -n "$BENCHMARK_JSON" ]; then
        # Check if json extension exists
        if [[ "$BENCHMARK_JSON" == *.json ]]; then
            base="${BENCHMARK_JSON%.json}"
            PROCESS_ARGS+=(--benchmark "${base}_process_${i}.json")
        else
            PROCESS_ARGS+=(--benchmark "${BENCHMARK_JSON}_process_${i}.json")
        fi
    else
        PROCESS_ARGS+=(--benchmark "benchmark_report_process_$i.json")
    fi

    echo -e "${BLUE}Starting process $i...${NC}"
    echo -e "${BLUE}  Log file: $log_file${NC}"
    echo -e "${BLUE}  Command: $BENCHMARK_EXECUTABLE ${PROCESS_ARGS[*]} --sync-dir $SYNC_DIR${NC}"

    "$BENCHMARK_EXECUTABLE" "${PROCESS_ARGS[@]}" --sync-dir "$SYNC_DIR" > "$log_file" 2>&1 &
    pid=$!
    PIDS+=($pid)
    echo -e "${GREEN}✓${NC} Process $i started (PID: $pid)"

    if [ $i -lt $NUM_PROCESSES ]; then
        sleep $STAGGER_START
    fi
done

# ─── Wait for all processes to be ready, then signal start ──────────────────
echo -e "\n${BLUE}Waiting for all $NUM_PROCESSES processes to open cameras...${NC}"
SYNC_TIMEOUT=120  # Max seconds to wait for all cameras to be ready
SYNC_ELAPSED=0
while true; do
    READY_COUNT=$(find "$SYNC_DIR" -maxdepth 1 -name 'ready_*' 2>/dev/null | wc -l)
    if [ "$READY_COUNT" -ge "$NUM_PROCESSES" ]; then
        break
    fi

    # Check if any process has already exited (failed to open camera)
    for j in "${!PIDS[@]}"; do
        pid=${PIDS[$j]}
        if ! kill -0 "$pid" 2>/dev/null; then
            idx=$((j + 1))
            echo -e "${RED}Process $idx (PID: $pid) exited before becoming ready.${NC}"
            log_file="$LOG_DIR/process_$idx.log"
            if [ -f "$log_file" ]; then
                echo -e "${RED}  Last lines of log:${NC}"
                tail -5 "$log_file" | sed 's/^/    /'
            fi
            rm -rf "$SYNC_DIR"
            trap - EXIT
            exit 1
        fi
    done

    sleep 1
    SYNC_ELAPSED=$((SYNC_ELAPSED + 1))
    if [ $SYNC_ELAPSED -ge $SYNC_TIMEOUT ]; then
        echo -e "${RED}Timeout: only $READY_COUNT/$NUM_PROCESSES processes ready after ${SYNC_TIMEOUT}s.${NC}"
        rm -rf "$SYNC_DIR"
        trap - EXIT
        exit 1
    fi
    echo -e "  ${CYAN}$READY_COUNT/$NUM_PROCESSES ready (${SYNC_ELAPSED}s)${NC}"
done

echo -e "${GREEN}All $NUM_PROCESSES processes ready. Signaling start...${NC}"
touch "$SYNC_DIR/start"

# Wait for all processes and check for errors
FAIL_COUNT=0
for i in "${!PIDS[@]}"; do
    pid=${PIDS[$i]}
    idx=$((i + 1))

    if wait "$pid"; then
        EXIT_CODE=0
    else
        EXIT_CODE=$?
    fi

    if [ $EXIT_CODE -ne 0 ]; then
        echo -e "${RED}Process $idx (PID: $pid) failed with exit code $EXIT_CODE${NC}"
        log_file="$LOG_DIR/process_$idx.log"
        if [ -f "$log_file" ]; then
            echo -e "${RED}  Last lines of log:${NC}"
            tail -5 "$log_file" | sed 's/^/    /'
        fi
        FAIL_COUNT=$((FAIL_COUNT+1))
    fi
done

if [ $FAIL_COUNT -eq 0 ]; then
    echo -e "\n${GREEN}All processes completed successfully.${NC}"
else
    echo -e "\n${RED}$FAIL_COUNT process(es) FAILED.${NC}"
fi

# Disable the EXIT trap so cleanup doesn't mask exit code
trap - EXIT
show_summary

# Cleanup sync directory
rm -rf "$SYNC_DIR"

if [ $FAIL_COUNT -gt 0 ]; then
    exit 1
fi
