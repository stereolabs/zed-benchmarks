#include <sl/Camera.hpp>
#include <sl/CameraOne.hpp>
#include "profiler.hpp"
#include "json.hpp"
#include <iostream>
#include <string>
#include <vector>
#include <atomic>
#include <chrono>
#include <thread>
#include <csignal>
#include "getopt_compat.h"
#include "glass_to_glass.hpp"
#include <cstdlib>
#include <fstream>
#include <filesystem>
#include <algorithm>
#include <numeric>
#include <cmath>
#include <memory>
#include <map>
#include <cstdint>

std::atomic<bool> is_running {true};
std::atomic<int> signal_count {0};

void signal_handler(int sig) {
    int count = ++signal_count;
    if (count >= 2) {
        const char msg[] = "\nForce exit.\n";
        write(STDOUT_FILENO, msg, sizeof(msg) - 1);
        _exit(1);
    }
    const char msg[] = "\nBenchmark stopped by user. Press Ctrl+C again to force exit.\n";
    write(STDOUT_FILENO, msg, sizeof(msg) - 1);
    is_running = false;
}

void printHelp() {
    std::cout << "Usage: benchmark_single_cam [options]\n";
    std::cout << "Options:\n";
    std::cout << "  --svo_input <filename>           Input SVO filename (default: empty)\n";
    std::cout << "  --camera_fps <fps>               Camera FPS (default: 30)\n";
    std::cout << "  --resolution <res>               Resolution (default: HD1080)\n";
    std::cout << "  --depth_mode <mode>              Depth Mode (default: NONE)\n";
    std::cout << "  --od_model <model>               Object Detection Model (default: OFF)\n";
    std::cout << "  --od_custom_onnx_file <path>     Path to custom ONNX file (requires od_model=CUSTOM_YOLOLIKE_BOX_OBJECTS)\n";
    std::cout << "  --od_custom_onnx_input_shape <WxH> Input resolution for custom ONNX (e.g. 640x640, default: 512x512)\n";
    std::cout << "  --bt_model <model>               Body Tracking Model (default: OFF)\n";
    std::cout << "  --compression_mode_recording <m> Compression Mode for Recording (default: OFF)\n";
    std::cout << "  --encoding_preset <preset>       Encoding preset: ULTRAFAST, FAST, MEDIUM, SLOW, DEFAULT (default: DEFAULT)\n";
    std::cout << "  --encryption_key <key>           Encryption key/passphrase for SVO recording (default: empty)\n";
    std::cout << "  --output_svo <filename>          Output SVO filename (default: benchmark_recording.svo)\n";
    std::cout << "  --compression_mode_streaming <m> Compression Mode for Streaming (default: OFF)\n";
    std::cout << "  --streaming_port <port>          Streaming port (default: 30000)\n";
    std::cout << "  --serial_number <sn>             Camera serial number (default: auto-detect)\n";
    std::cout << "  --enable_positional_tracking     Enable Positional Tracking (default: OFF)\n";
    std::cout << "  --warmup <seconds>               Warmup duration in seconds (default: 5.0)\n";
    std::cout << "  --duration <seconds>             Duration in seconds (default: 60)\n";
    std::cout << "  --benchmark <filename>           Benchmark report filename (default: benchmark_report.json)\n";
    std::cout << "  --camera_one                     Use CameraOne (mono) API instead of Camera (stereo)\n";
    std::cout << "  --sync-dir <directory>           Barrier sync directory for multi-process coordination\n";
    std::cout << "  --no_monitor_imu                 Disable IMU monitoring (enabled by default)\n";
    std::cout << "  --glass_to_glass_latency         Measure glass-to-glass latency via an on-screen pattern (default: OFF)\n";
    std::cout << "  --help                           Show this help message\n";
}

// Parse a CLI integer argument. On malformed/garbage input (non-numeric, overflow, or trailing
// junk) print a clear error and exit(2), rather than letting std::stoi/stoll throw an uncaught
// exception that aborts the process with a cryptic "terminate called after throwing...".
static long long parse_int_arg(const char* val, const char* opt) {
    try {
        size_t pos = 0;
        long long v = std::stoll(val, &pos);
        while (val[pos] == ' ' || val[pos] == '\t')
            ++pos;
        if (val[pos] == '\0')
            return v;
    } catch (const std::exception&) {
        // fall through to the error below
    }
    std::cerr << "Error: " << opt << " expects an integer, got '" << val << "'" << std::endl;
    std::exit(2);
}

// Same as parse_int_arg for floating-point options (--warmup / --duration).
static double parse_float_arg(const char* val, const char* opt) {
    try {
        size_t pos = 0;
        double v = std::stod(val, &pos);
        while (val[pos] == ' ' || val[pos] == '\t')
            ++pos;
        if (val[pos] == '\0')
            return v;
    } catch (const std::exception&) {
        // fall through to the error below
    }
    std::cerr << "Error: " << opt << " expects a number, got '" << val << "'" << std::endl;
    std::exit(2);
}

// Barrier sync: signal ready and wait for start
void sync_barrier_signal_and_wait(const std::string& sync_dir) {
    if (sync_dir.empty())
        return;

    // Write ready file using PID as unique identifier
    std::string ready_file = sync_dir + "/ready_" + std::to_string(getpid());
    std::ofstream(ready_file).close();
    std::cout << "[Sync] Camera ready. Waiting for all processes..." << std::endl;

    // Poll for start signal
    std::string start_file = sync_dir + "/start";
    while (is_running) {
        if (std::filesystem::exists(start_file)) {
            std::cout << "[Sync] All processes ready. Starting benchmark." << std::endl;
            return;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
}

// ── Loop timing & IMU report helpers ────────────────────────────────────────
using clk = std::chrono::high_resolution_clock;

struct TimingStats final {
    double mean = 0., median = 0., stddev = 0.;
};

TimingStats compute_stats(std::vector<double>& samples) {
    TimingStats s;
    if (samples.empty())
        return s;
    size_t n = samples.size();
    s.mean = std::accumulate(samples.begin(), samples.end(), 0.0) / n;
    std::sort(samples.begin(), samples.end());
    s.median = (n % 2 == 0) ? (samples[n / 2 - 1] + samples[n / 2]) / 2.0 : samples[n / 2];
    double sq = 0;
    for (double v : samples)
        sq += (v - s.mean) * (v - s.mean);
    s.stddev = std::sqrt(sq / n);
    return s;
}

// Round to 3 decimals so JSON numbers stay tidy (matches the Python report's round(x, 3)).
double round3(double v) {
    return std::round(v * 1000.0) / 1000.0;
}

// Parse the existing report, add one section, and re-dump the whole object with dump(4).
// Round-tripping through sl::json (instead of string-splicing the closing brace) keeps the
// output well-formed and consistently indented no matter how many sections get appended.
void append_imu_json(
    const std::string& filename,
    const std::string& imu_key,
    size_t imu_sample_count,
    double total_seconds,
    std::vector<double>& imu_per_frame
) {
    if (imu_sample_count == 0)
        return;

    sl::json report;
    {
        std::ifstream in(filename);
        if (!in.is_open())
            return;
        try {
            in >> report;
        } catch (const std::exception& e) {
            std::cerr << "[Profiler] Could not parse " << filename << " to append IMU data: " << e.what() << std::endl;
            return;
        }
    }
    if (!report.is_object())
        return;

    const double rate = (total_seconds > 0) ? static_cast<double>(imu_sample_count) / total_seconds : 0.0;
    const TimingStats ipf = compute_stats(imu_per_frame);

    report[imu_key] = {
        {"imu_sample_count", imu_sample_count                                                                            },
        {"imu_rate_hz",      round3(rate)                                                                                },
        {"imu_per_frame",    {{"mean", round3(ipf.mean)}, {"median", round3(ipf.median)}, {"stddev", round3(ipf.stddev)}}},
    };

    std::ofstream f(filename, std::ios::trunc);
    if (f.is_open())
        f << report.dump(4) << "\n";
}

// ── Live-timestamp correspondence (G2G reconstruction at SVO playback) ────────
// At SVO playback, getTimestamp(CURRENT) returns the playback wall-clock, while the filmed
// screen shows the live capture time, so a G2G latency computed from it is meaningless. To fix
// this we persist, during live capture, a map from each frame's TIME_REFERENCE::IMAGE timestamp
// (baked into the frame, identical across record/playback) to the live "now" sampled right
// after read(). At playback we look the frame up by its IMAGE timestamp and replay the recorded
// "now". G2I does not need it: at playback it measures the SVO processing time instead.

static const char* const LIVE_TS_KEY = "live_timestamp_correspondence";

std::string serialize_live_ts_map(const std::map<sl::Timestamp, sl::Timestamp>& m) {
    sl::json j = sl::json::object();
    for (const auto& kv : m)
        j[std::to_string(kv.first.getNanoseconds())] = kv.second.getNanoseconds(); // key=ns (string), value=ns (number)
    return j.dump();
}

bool deserialize_live_ts_map(const std::string& payload, std::map<sl::Timestamp, sl::Timestamp>& m) {
    try {
        sl::json j = sl::json::parse(payload);
        for (auto it = j.begin(); it != j.end(); ++it)
            m[sl::Timestamp(std::stoull(it.key()))] = sl::Timestamp(it.value().get<uint64_t>());
    } catch (const std::exception& e) {
        std::cerr << "[LiveTS] Failed to parse live-timestamp correspondences: " << e.what() << std::endl;
        return false;
    }
    return true;
}

// RECORD: At the end of capture time we ingest the whole live-timestamp correspondence map into the saved SVO2.
template <typename CameraT>
bool ingest_live_ts_map(CameraT& zed, const std::map<sl::Timestamp, sl::Timestamp>& m, sl::Timestamp anchor_ts) {
    if (m.empty()) {
        std::cerr << "[LiveTS] No live timestamps collected; nothing to ingest." << std::endl;
        return false;
    }
    sl::SVOData data;
    data.key = LIVE_TS_KEY;
    data.setContent(serialize_live_ts_map(m));
    // Anchor the blob at a real in-range frame timestamp (the last grabbed IMAGE ts, in ns:
    // the map keys are milliseconds, not valid frame ns) so retrieveSVOData finds it.
    data.timestamp_ns = anchor_ts;
    sl::ERROR_CODE err = zed.ingestDataIntoSVO(data);
    if (err != sl::ERROR_CODE::SUCCESS) {
        std::cerr << "[LiveTS] Failed to ingest live-timestamp correspondences: " << err << std::endl;
        return false;
    }
    std::cout << "[LiveTS] Ingested " << m.size() << " live-timestamp correspondences into the SVO." << std::endl;
    return true;
}

// LOAD: At load time, we try to load the saved live-timestamp correspondence map from SVO2
template <typename CameraT>
bool load_live_ts_map(CameraT& zed, std::map<sl::Timestamp, sl::Timestamp>& m) {
    std::vector<std::string> keys = zed.getSVODataKeys();
    if (std::find(keys.begin(), keys.end(), std::string(LIVE_TS_KEY)) == keys.end()) {
        std::cerr << "[LiveTS] This SVO has no '" << LIVE_TS_KEY
                  << "' channel: the original live "
                     "capture timestamps were not recorded, so glass-to-glass latency "
                     "cannot be reconstructed at playback. Re-record the SVO with recording enabled to "
                     "embed live timestamps."
                  << std::endl;
        return false;
    }
    std::map<sl::Timestamp, sl::SVOData> svo_data;
    sl::ERROR_CODE err = zed.retrieveSVOData(LIVE_TS_KEY, svo_data, 0, 0); // (0,0) = retrieve all
    if (err != sl::ERROR_CODE::SUCCESS || svo_data.empty()) {
        std::cerr << "[LiveTS] Failed to retrieve '" << LIVE_TS_KEY << "' from the SVO: " << err << std::endl;
        return false;
    }
    std::string payload;
    svo_data.begin()->second.getContent(payload); // single blob, anchored at one frame ts
    if (!deserialize_live_ts_map(payload, m) || m.empty()) {
        std::cerr << "[LiveTS] '" << LIVE_TS_KEY << "' present but empty/corrupt after parse." << std::endl;
        return false;
    }
    std::cout << "[LiveTS] Loaded " << m.size() << " live-timestamp correspondences from the SVO." << std::endl;
    return true;
}

// ── Profiler report ──────────────────────────────────────────────────────────
void write_profiler_report(
    const std::string& dump_file,
    const std::string& out_file,
    const std::string& sn,
    int frames_count,
    double total_seconds,
    const std::string& compression_rec_str = "",
    bool encrypted = false
) {
    sl::json dump = sl::json::object();
    {
        std::ifstream in(dump_file);
        if (in.is_open()) {
            try {
                in >> dump;
            } catch (...) {
                dump = sl::json::object();
            }
        }
    }

    // Scoped profiles live under the default context; each section name is
    // "camera_sn_<sn>_<function>(...)", so build that key once per lookup below.
    auto prof_key = [&](const std::string& fn) {
        return "camera_sn_" + sn + "_" + fn;
    };
    sl::json profiles = sl::json::object();
    if (dump.contains("profile_stats") && dump["profile_stats"].is_object()) {
        const auto& ps = dump["profile_stats"];
        if (ps.contains("default") && ps["default"].is_object())
            profiles = ps["default"];
    }

    auto cpu_stats = [&](const std::string& name) -> sl::json {
        sl::json s = profiles.contains(name) ? profiles[name] : sl::json::object();
        return {
            {"mean",       s.value("mean",   0.0)},
            {"median",     s.value("median", 0.0)},
            {"stddev",     s.value("stddev", 0.0)},
            {"min",        s.value("min",    0.0)},
            {"max",        s.value("max",    0.0)},
            {"p99",        s.value("p99",    0.0)},
            {"nb_samples", s.value("count",  0)  }
        };
    };

    // read() and grab() are profiled as two separate sections; the reported "Grab" cost is their
    // field-by-field sum, so it reflects the full capture+process time.
    auto cpu_stats_sum = [&](const std::string& a, const std::string& b) -> sl::json {
        const sl::json sa = profiles.contains(a) ? profiles[a] : sl::json::object();
        const sl::json sb = profiles.contains(b) ? profiles[b] : sl::json::object();
        auto sum = [&](const char* k) {
            return sa.value(k, 0.0) + sb.value(k, 0.0);
        };
        return {
            {"mean", sum("mean")},
            {"median", sum("median")},
            {"stddev", sum("stddev")},
            {"min", sum("min")},
            {"max", sum("max")},
            {"p99", sum("p99")},
            {"nb_samples", sb.value("count", sa.value("count", 0))}
        };
    };

    sl::json report = sl::json::object();

    if (!compression_rec_str.empty()) {
        report["config"] = {
            {"recording_compression", compression_rec_str},
            {"recording_encrypted",   encrypted          }
        };
    }

    const double freq = (total_seconds > 0.0) ? frames_count / total_seconds : 0.0;
    sl::json period = profiles.contains(prof_key("grab_period")) ? profiles[prof_key("grab_period")] : sl::json::object();
    report["Grab_SN" + sn] = {
        {"grab_call_frequency_hz", freq},
        {"grab_call_mean_period_ms", period.value("mean", 0.0)},
        {"grab_call_median_period_ms", period.value("median", 0.0)},
        {"cpu_stats", cpu_stats_sum(prof_key("read(...)"), prof_key("grab(...)"))}
    };

    // Per-operation sections (only those actually profiled appear in the dump). The profiled
    // section is "camera_sn_<sn>_<function>(...)"; the report key is "<ReportName>_SN<sn>" so
    // per-event timings stay serial-keyed and coherent across single- and multi-camera reports.
    // GrabProcessing is the grab() call alone (SDK processing, without the read() capture step),
    // so it can be stacked on top of glass-to-image without counting read() twice.
    const std::vector<std::pair<std::string, std::string>> event_sections = {
        {"grab(...)",            "GrabProcessing" },
        {"retrieveImage(...)",   "RetrieveImage"  },
        {"retrieveMeasure(...)", "RetrieveMeasure"},
        {"retrieveObjects(...)", "RetrieveObjects"},
        {"retrieveBodies(...)",  "RetrieveBodies" },
        {"getPosition(...)",     "GetPosition"    },
    };
    for (const auto& ev : event_sections) {
        if (profiles.contains(prof_key(ev.first)))
            report[ev.second + "_SN" + sn] = {
                {"cpu_stats", cpu_stats(prof_key(ev.first))}
            };
    }

    // Latency sections, fed to the profiler with addSample() (see the grab loop). Statistics are
    // computed over the WHOLE sample set: the G2G decoder only emits a sample after the per-column
    // agreement + CRC + plausibility checks pass, so mis-detections are rejected at decode time and
    // no statistical tail-trim is needed.
    const std::vector<std::pair<std::string, std::string>> latency_sections = {
        {"glass_to_image", "GlassToImageLatency"},
        {"glass_to_glass", "GlassToGlassLatency"},
    };
    for (const auto& lat : latency_sections) {
        if (!profiles.contains(prof_key(lat.first)))
            continue;
        const sl::json s = profiles[prof_key(lat.first)];
        report[lat.second + "_SN" + sn] = {
            {"sample_count", s.value("count", 0)},
            {"latency_ms",
             {{"mean", round3(s.value("mean", 0.0))},
              {"median", round3(s.value("median", 0.0))},
              {"stddev", round3(s.value("stddev", 0.0))},
              {"min", round3(s.value("min", 0.0))},
              {"max", round3(s.value("max", 0.0))}}},
        };
    }

    std::ofstream f(out_file, std::ios::trunc);
    if (f.is_open()) {
        f << report.dump(4) << "\n";
        std::cout << "[Profiler] Results written to " << out_file << std::endl;
    }
}

// Run benchmark for a mono camera (CameraOne)
int runMonoBenchmark(
    int camera_fps,
    const std::string& resolution_str,
    const std::string& svo_input,
    const std::string& compression_rec_str,
    const std::string& encoding_preset_str,
    const std::string& encryption_key_str,
    const std::string& output_svo,
    const std::string& compression_stream_str,
    unsigned int serial_number,
    int streaming_port,
    float warmup,
    float duration,
    const std::string& sync_dir,
    const std::string& output_report,
    const bool monitor_imu,
    const bool measure_glass_to_glass_latency
) {
    sl::CameraOne zed;
    sl::InitParametersOne init_params;

    if (!resolution_str.empty()) {
        sl::fromString(resolution_str.c_str(), init_params.camera_resolution);
    }
    init_params.camera_fps = camera_fps;
    if (!svo_input.empty()) {
        init_params.input.setFromSVOFile(svo_input.c_str());
        init_params.svo_real_time_mode = false;
        if (!encryption_key_str.empty())
            init_params.svo_decryption_key = sl::String(encryption_key_str.c_str());
    } else if (serial_number > 0) {
        init_params.input.setFromSerialNumber(serial_number);
    }

    signal(SIGINT, signal_handler);

    std::cout << "Opening CameraOne..." << std::endl;
    sl::ERROR_CODE status = zed.open(init_params);
    if (status != sl::ERROR_CODE::SUCCESS) {
        std::cerr << "Failed to open CameraOne: " << status << std::endl;
        return 1;
    }

    auto cam_info = zed.getCameraInformation();
    std::cout << "Opened CameraOne (" << sl::toString(cam_info.camera_model) << ")" << std::endl;

    // Live timestamp map: populated while recording live, then used to reconstruct the
    // G2G timestamps during SVO playback.
    //   key   = IMAGE timestamp inside SVO
    //   value = CURRENT-after-read "now", RAW (no G2G offset applied; the offset is
    //           display/machine-specific and re-measured + applied at playback)
    // Both key and value are stored as integer nanoseconds.
    const bool record_live_ts = !compression_rec_str.empty();
    const bool playback_live_ts = !svo_input.empty() && measure_glass_to_glass_latency;
    std::map<sl::Timestamp, sl::Timestamp> live_ts_map;
    sl::Timestamp last_image_ts; // ns anchor for the ingested blob (map keys are ms)

    // The image timestamp stored inside the SVO doesn't tell us when the image was
    // actually retrieved on the host, which G2G needs. To recover that, we decode the map
    // embedded in the SVO. If the map isn't present, we return early to avoid reporting a
    // bad measurement.
    if (playback_live_ts && !load_live_ts_map(zed, live_ts_map)) {
        zed.close();
        return 1;
    }

    // Enable Recording
    if (!compression_rec_str.empty()) {
        sl::RecordingParameters rec_params;
        sl::fromString(compression_rec_str.c_str(), rec_params.compression_mode);
        rec_params.video_filename = sl::String(output_svo.c_str());
        if (!encoding_preset_str.empty())
            sl::fromString(encoding_preset_str.c_str(), rec_params.encoding_preset);
        if (!encryption_key_str.empty())
            rec_params.encryption_key = sl::String(encryption_key_str.c_str());
        sl::ERROR_CODE err = zed.enableRecording(rec_params);
        if (err != sl::ERROR_CODE::SUCCESS) {
            std::cerr << "Failed to enable recording: " << err << std::endl;
            zed.close();
            return 1;
        }
        std::cout << "Recording enabled (" << compression_rec_str << (encoding_preset_str.empty() ? "" : ", preset=" + encoding_preset_str)
                  << (encryption_key_str.empty() ? "" : ", encrypted") << ") to " << output_svo << std::endl;
    }

    // Enable Streaming
    if (!compression_stream_str.empty()) {
        sl::StreamingParameters stream_params;
        sl::fromString(compression_stream_str.c_str(), stream_params.codec);
        stream_params.port = streaming_port;
        sl::ERROR_CODE err = zed.enableStreaming(stream_params);
        if (err != sl::ERROR_CODE::SUCCESS) {
            std::cerr << "Failed to enable streaming: " << err << std::endl;
            zed.close();
            return 1;
        }
        std::cout << "Streaming enabled (" << compression_stream_str << ") on port " << streaming_port << std::endl;
    }

    // Barrier sync: wait for all processes before starting
    sync_barrier_signal_and_wait(sync_dir);

    sl::Mat image;

    // Warmup. Skip on SVO playback: there is no hardware to warm up, and warmup would consume
    // frames from the file (svo_real_time_mode is off), shrinking the benchmark window.
    if (warmup > 0.f && svo_input.empty()) {
        std::cout << "\nStarting warmup loop for " << warmup << " seconds..." << std::endl;
        auto warmup_start_time = std::chrono::high_resolution_clock::now();
        while (is_running) {
            auto current_time = std::chrono::high_resolution_clock::now();
            std::chrono::duration<double> elapsed = current_time - warmup_start_time;
            if (elapsed.count() > warmup) {
                std::cout << "Warmup finished." << std::endl;
                break;
            }
            if (zed.grab() > sl::ERROR_CODE::SUCCESS) {
                std::this_thread::sleep_for(std::chrono::milliseconds(1));
            }
        }
    } else if (warmup > 0.f) {
        std::cout << "\nSVO input detected: skipping warmup." << std::endl;
    }

    // Enable scoped profiling after warmup so warmup grabs are not measured.
    bench::Profiler::enable();
    const std::string sn = std::to_string(cam_info.serial_number);

    // Profiled section names are "camera_sn_<sn>_<function>(...)"; precompute them once so
    // the grab loop never allocates a string per call.
    const std::string prof_read = "camera_sn_" + sn + "_read(...)";
    const std::string prof_grab = "camera_sn_" + sn + "_grab(...)";
    const std::string prof_period = "camera_sn_" + sn + "_grab_period";
    const std::string prof_img = "camera_sn_" + sn + "_retrieveImage(...)";
    // Latency sections, fed with addSample() rather than start()/stop().
    const std::string prof_g2i = "camera_sn_" + sn + "_glass_to_image";
    const std::string prof_g2g = "camera_sn_" + sn + "_glass_to_glass";

    int frames_count = 0;
    auto start_time = clk::now();
    bool period_open = false;

    // IMU monitoring state
    size_t imu_sample_count = 0;
    std::vector<double> imu_per_frame;
    if (monitor_imu)
        imu_per_frame.reserve(std::max(1, (int)(duration * camera_fps * 1.1)));

#ifdef HAVE_G2G
    std::unique_ptr<g2g::Display> g2g_display;
    std::unique_ptr<g2g::AsyncDecoder> g2g_async;   // live capture: decode off the grab loop (may drop frames)
    std::unique_ptr<g2g::Decoder> g2g_sync_decoder; // SVO playback: decode every frame inline (repeatable)
    if (measure_glass_to_glass_latency) {
        // On SVO playback the filmed screen is already baked into the recorded frames, so we
        // decode those rather than driving a live screen, no display window is opened. The
        // window is only useful for live capture.
        bool g2g_ok = true;
        if (svo_input.empty()) {
            g2g_display.reset(new g2g::Display());
            g2g_display->setCameras({sn}); // single readout row, labeled by serial
            if (!g2g_display->start()) {
                std::cerr << "[G2G] Failed to open the display window; disabling G2G." << std::endl;
                g2g_display.reset();
                g2g_ok = false;
            }
        } else {
            std::cout << "[G2G] SVO playback: decoding the recorded screen; no display window opened." << std::endl;
        }
        if (g2g_ok) {
            std::cout << "[G2G] enabled" << std::endl;
            if (svo_input.empty()) {
                // Live capture: decode on a worker thread so it never inflates the grab loop period.
                g2g_async.reset(new g2g::AsyncDecoder());
                g2g_async->start(g2g_display.get(), sn, (size_t)std::max(1, (int)(duration * camera_fps * 1.1)));
            } else {
                // SVO playback: decode synchronously on EVERY frame so two replays of the same SVO
                // produce the exact same G2G samples (the async path drops a timing-dependent subset).
                g2g_sync_decoder.reset(new g2g::Decoder());
            }
        }
    }
#endif

    std::cout << "\nStarting benchmark loop for " << duration << " seconds. Press Ctrl+C to stop." << std::endl;

    while (is_running) {
        auto current_time = clk::now();
        std::chrono::duration<double> elapsed = current_time - start_time;
        // SVO playback ignores --duration: process the whole file so two replays of the same SVO
        // cover the exact same frames (repeatability). Live capture stays time-bounded.
        if (svo_input.empty() && duration > 0 && elapsed.count() > duration) {
            std::cout << "\nBenchmark finished (duration reached)." << std::endl;
            break;
        }

        // Split capture: read() pulls + rectifies the frame (the blocking step); grab() then
        // runs the SDK measurements. Each is profiled separately (.read() /
        // .grab()) and summed in the report into the full capture+process "Grab" cost.
        //
        // Cross-language note: pyzed.sl.CameraOne has no read() method, so the Python mono
        // runner's glass-to-image includes grab().
        const long long before_read_ns = (long long)zed.getTimestamp(sl::TIME_REFERENCE::CURRENT).getNanoseconds();

        bench::Profiler::start(prof_read);
        auto read_err = zed.read();
        bench::Profiler::stop(prof_read);

        const long long after_read_ns = (long long)zed.getTimestamp(sl::TIME_REFERENCE::CURRENT).getNanoseconds();

        bench::Profiler::start(prof_grab);
        auto grab_err = zed.grab();
        bench::Profiler::stop(prof_grab);

        if (grab_err == sl::ERROR_CODE::SUCCESS) {
            const sl::Timestamp image_ts = zed.getTimestamp(sl::TIME_REFERENCE::IMAGE);

            const long long capture_ref_ns = svo_input.empty() ? (long long)image_ts.getNanoseconds() : before_read_ns;

            // Recording: store the raw after-read "now" keyed by the IMAGE timestamp,
            // both truncated to integer milliseconds.
            if (record_live_ts) {
                live_ts_map[image_ts] = sl::Timestamp((uint64_t)after_read_ns);
                last_image_ts = image_ts;
            }

            if (period_open)
                bench::Profiler::stop(prof_period);
            bench::Profiler::start(prof_period);
            period_open = true;

            // Use Resolution(-1,-1) for optimal working resolution
            const long long before_img_ns = (long long)zed.getTimestamp(sl::TIME_REFERENCE::CURRENT).getNanoseconds();
            bench::Profiler::start(prof_img);
            zed.retrieveImage(image, sl::VIEW::LEFT, sl::MEM::CPU, sl::Resolution(-1, -1));
            bench::Profiler::stop(prof_img);
            const long long after_img_ns = (long long)zed.getTimestamp(sl::TIME_REFERENCE::CURRENT).getNanoseconds();

            bench::Profiler::addSample(prof_g2i, ((after_read_ns - capture_ref_ns) + (after_img_ns - before_img_ns)) / 1.0e6);
#ifdef HAVE_G2G
            // Glass-to-glass: offload the just-grabbed frame to the async decoder worker
            // so the expensive decode never inflates this grab loop. The "now" timestamp
            // is the after-read one sampled above. We submit a deep copy of the frame; the
            // worker then decodes it, computes the latency, and updates the display.
            if (measure_glass_to_glass_latency) {
                long long g2g_now_ns = after_read_ns;
                bool g2g_now_ok = true;
                if (playback_live_ts) {
                    auto it = live_ts_map.find(image_ts);
                    if (it == live_ts_map.end()) {
                        std::cerr << "[LiveTS] No correspondence for IMAGE ts " << image_ts.getNanoseconds()
                                  << "; no G2G sample for this frame." << std::endl;
                        g2g_now_ok = false;
                    } else {
                        g2g_now_ns = (long long)it->second.getNanoseconds();
                    }
                }
                if (g2g_now_ok && g2g_async) {
                    g2g_async->submit(image, g2g_now_ns);
                } else if (g2g_now_ok && g2g_sync_decoder) {
                    // SVO: decode every frame here (no async drops) for a repeatable result.
                    g2g::DecodeResult r = g2g_sync_decoder->decode(image, (uint64_t)g2g_now_ns);
                    if (r.status == g2g::Status::LOCKED) {
                        const double g2g_lat_ms = (g2g_now_ns - (long long)r.display_ns) / 1.0e6;
                        if (g2g_lat_ms > 0.0) // drop non-positive latency
                            bench::Profiler::addSample(prof_g2g, g2g_lat_ms);
                    }
                }
            }
#endif

            frames_count++;
            if (monitor_imu) {
                std::vector<sl::SensorsData> batch;
                auto sens_err = zed.getSensorsDataBatch(batch);
                if (sens_err == sl::ERROR_CODE::SUCCESS) {
                    imu_sample_count += batch.size();
                    imu_per_frame.push_back(static_cast<double>(batch.size()));
                } else {
                    imu_per_frame.push_back(0.0);
                }
            }
            if (frames_count % camera_fps == 0) {
                printf("\rFPS: %.2f | Frames: %d", zed.getCurrentFPS(), frames_count);
                fflush(stdout);
            }
        } else {
            // End of SVO: stop here instead of spinning. This both terminates the otherwise
            // unbounded SVO run and keeps the reported frequency free of a post-EOF idle tail.
            if (!svo_input.empty() && grab_err == sl::ERROR_CODE::END_OF_SVOFILE_REACHED) {
                std::cout << "\nBenchmark finished (end of SVO reached)." << std::endl;
                break;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(1));
        }
    }
    bench::Profiler::stop(prof_period);

    printf("\nFinal FPS: %.2f\n", zed.getCurrentFPS());

    auto end_time = clk::now();
    const double total_s = std::chrono::duration<double>(end_time - start_time).count();

#ifdef HAVE_G2G
    // Stop + join the async decoder before the profiler dump, so every in-flight latency sample
    // is collected; then hand its latencies to the profiler.
    if (g2g_async) {
        g2g_async->stop();
        for (double lat_ms : g2g_async->takeLatencies())
            bench::Profiler::addSample(prof_g2g, lat_ms);
        g2g_async.reset();
    }
#endif

    // Dump profiler stats while the camera is still open (closing it resets the
    // profiler), then reshape into the historical report schema.
    const std::string raw_report = output_report + ".profiler.json";
    bench::Profiler::dumpStats(raw_report);

    // Persist the live-timestamp correspondence map into the SVO before recording is
    // disabled (ingestDataIntoSVO is only valid while recording).
    if (record_live_ts)
        ingest_live_ts_map(zed, live_ts_map, last_image_ts);

    if (!compression_rec_str.empty())
        zed.disableRecording();
    if (!compression_stream_str.empty())
        zed.disableStreaming();
    zed.close();

    write_profiler_report(raw_report, output_report, sn, frames_count, total_s, compression_rec_str, !encryption_key_str.empty());
    std::error_code ec;
    std::filesystem::remove(raw_report, ec);

#ifdef HAVE_G2G
    if (g2g_display)
        g2g_display->stop();
#endif

    // Append IMU data to the benchmark report
    if (monitor_imu && imu_sample_count > 0)
        append_imu_json(output_report, "IMU_SN" + sn, imu_sample_count, total_s, imu_per_frame);

    return 0;
}

// Run benchmark for a stereo camera (Camera)
int runStereoBenchmark(
    int camera_fps,
    const std::string& resolution_str,
    const std::string& svo_input,
    const std::string& depth_mode_str,
    const std::string& od_model_str,
    const std::string& od_custom_onnx_file,
    const std::string& od_custom_onnx_input_shape,
    const std::string& bt_model_str,
    const std::string& compression_rec_str,
    const std::string& encoding_preset_str,
    const std::string& encryption_key_str,
    const std::string& output_svo,
    const std::string& compression_stream_str,
    bool enable_positional_tracking,
    unsigned int serial_number,
    int streaming_port,
    float warmup,
    float duration,
    const std::string& sync_dir,
    const std::string& output_report,
    const bool monitor_imu,
    const bool measure_glass_to_glass_latency
) {
    sl::Camera zed;
    sl::InitParameters init_params;

    if (!resolution_str.empty()) {
        bool error_from_string = sl::fromString(resolution_str.c_str(), init_params.camera_resolution);
        if (error_from_string)
            std::cout << "Invalid Resolution " << resolution_str << ". Using " << sl::toString(init_params.camera_resolution) << std::endl;
    }
    init_params.camera_fps = camera_fps;
    if (!depth_mode_str.empty()) {
        bool error_from_string = sl::fromString(depth_mode_str.c_str(), init_params.depth_mode);
        if (error_from_string) {
            init_params.depth_mode = sl::DEPTH_MODE::NONE;
            std::cout << "Invalid Depth Mode " << depth_mode_str << ". Using NONE." << std::endl;
        }
    } else {
        init_params.depth_mode = sl::DEPTH_MODE::NONE;
    }
    if (!svo_input.empty()) {
        init_params.input.setFromSVOFile(svo_input.c_str());
        init_params.svo_real_time_mode = false;
        if (!encryption_key_str.empty())
            init_params.svo_decryption_key = sl::String(encryption_key_str.c_str());
    } else if (serial_number > 0) {
        init_params.input.setFromSerialNumber(serial_number);
    }

    signal(SIGINT, signal_handler);

    std::cout << "Opening Camera..." << std::endl;
    sl::ERROR_CODE status = zed.open(init_params);
    if (status != sl::ERROR_CODE::SUCCESS) {
        std::cerr << "Failed to open camera: " << status << std::endl;
        return 1;
    }

    std::cout << "Opened Camera (" << sl::toString(zed.getCameraInformation().camera_model) << ")" << std::endl;

    // Live timestamp map: populated while recording live, then used to reconstruct the
    // G2G timestamps during SVO playback.
    //   key   = IMAGE timestamp inside SVO
    //   value = CURRENT-after-read "now", RAW (no G2G offset applied; the offset is
    //           display/machine-specific and re-measured + applied at playback)
    // Both key and value are stored as integer nanoseconds.
    const bool record_live_ts = !compression_rec_str.empty();
    const bool playback_live_ts = !svo_input.empty() && measure_glass_to_glass_latency;
    std::map<sl::Timestamp, sl::Timestamp> live_ts_map;
    sl::Timestamp last_image_ts; // ns anchor for the ingested blob (map keys are ms)

    // The image timestamp stored inside the SVO doesn't tell us when the image was
    // actually retrieved on the host, which G2G needs. To recover that, we decode the map
    // embedded in the SVO. If the map isn't present, we return early to avoid reporting a
    // bad measurement.
    if (playback_live_ts && !load_live_ts_map(zed, live_ts_map)) {
        zed.close();
        return 1;
    }

    // Enable Recording
    if (!compression_rec_str.empty()) {
        sl::RecordingParameters rec_params;
        sl::fromString(compression_rec_str.c_str(), rec_params.compression_mode);
        rec_params.video_filename = sl::String(output_svo.c_str());
        if (!encoding_preset_str.empty())
            sl::fromString(encoding_preset_str.c_str(), rec_params.encoding_preset);
        if (!encryption_key_str.empty())
            rec_params.encryption_key = sl::String(encryption_key_str.c_str());
        sl::ERROR_CODE err = zed.enableRecording(rec_params);
        if (err != sl::ERROR_CODE::SUCCESS) {
            std::cerr << "Failed to enable recording: " << err << std::endl;
            zed.close();
            return 1;
        }
        std::cout << "Recording enabled (" << compression_rec_str << (encoding_preset_str.empty() ? "" : ", preset=" + encoding_preset_str)
                  << (encryption_key_str.empty() ? "" : ", encrypted") << ") to " << output_svo << std::endl;
    }

    // Enable Streaming
    if (!compression_stream_str.empty()) {
        sl::StreamingParameters stream_params;
        sl::fromString(compression_stream_str.c_str(), stream_params.codec);
        stream_params.port = streaming_port;
        sl::ERROR_CODE err = zed.enableStreaming(stream_params);
        if (err != sl::ERROR_CODE::SUCCESS) {
            std::cerr << "Failed to enable streaming: " << err << std::endl;
            zed.close();
            return 1;
        }
        std::cout << "Streaming enabled (" << compression_stream_str << ") on port " << streaming_port << std::endl;
    }

    // Positional Tracking
    bool pt_enabled = false;
    if (enable_positional_tracking) {
        sl::PositionalTrackingParameters pt_params;
        pt_params.enable_area_memory = true;
        sl::ERROR_CODE err = zed.enablePositionalTracking(pt_params);
        if (err != sl::ERROR_CODE::SUCCESS) {
            std::cerr << "Failed to enable Positional Tracking: " << err << std::endl;
            zed.close();
            return 1;
        }
        std::cout << "Positional Tracking enabled (Area Memory)" << std::endl;
        pt_enabled = true;
    }

    // Object Detection
    if (!od_model_str.empty()) {
        sl::ObjectDetectionParameters od_params;
        sl::fromString(od_model_str.c_str(), od_params.detection_model);
        if (!od_custom_onnx_file.empty()) {
            od_params.custom_onnx_file = sl::String(od_custom_onnx_file.c_str());
            if (!od_custom_onnx_input_shape.empty()) {
                // Parse "WxH" format. Validate both tokens are positive integers; a malformed value
                // (e.g. "640", "640x", "640xABC", negatives) must not crash via std::stoi nor silently
                // configure a bogus resolution: warn and keep the default instead.
                size_t xpos = od_custom_onnx_input_shape.find('x');
                bool shape_ok = false;
                if (xpos != std::string::npos) {
                    try {
                        int w = std::stoi(od_custom_onnx_input_shape.substr(0, xpos));
                        int h = std::stoi(od_custom_onnx_input_shape.substr(xpos + 1));
                        if (w > 0 && h > 0) {
                            od_params.custom_onnx_dynamic_input_shape = sl::Resolution(w, h);
                            shape_ok = true;
                        }
                    } catch (const std::exception&) {
                        // handled by the shape_ok warning below
                    }
                }
                if (!shape_ok)
                    std::cerr << "[OD] Invalid --od_custom_onnx_input_shape '" << od_custom_onnx_input_shape
                              << "' (expected positive WxH, e.g. 640x640); using default." << std::endl;
            }
            std::cout << "Custom ONNX file: " << od_custom_onnx_file << std::endl;
            std::cout << "Custom ONNX input shape: " << od_params.custom_onnx_dynamic_input_shape.width << "x"
                      << od_params.custom_onnx_dynamic_input_shape.height << std::endl;
        }
        if (!pt_enabled) {
            sl::PositionalTrackingParameters pt_params;
            pt_params.enable_area_memory = true;
            zed.enablePositionalTracking(pt_params);
            pt_enabled = true;
        }
        od_params.enable_tracking = true;
        sl::ERROR_CODE err = zed.enableObjectDetection(od_params);
        if (err != sl::ERROR_CODE::SUCCESS) {
            std::cerr << "Failed to enable Object Detection: " << err << std::endl;
            zed.close();
            return 1;
        }
        std::cout << "Object Detection enabled (" << od_model_str << ")" << std::endl;
    }

    // Body Tracking
    if (!bt_model_str.empty()) {
        sl::BodyTrackingParameters bt_params;
        sl::fromString(bt_model_str.c_str(), bt_params.detection_model);
        if (!pt_enabled) {
            sl::PositionalTrackingParameters pt_params;
            pt_params.enable_area_memory = true;
            zed.enablePositionalTracking(pt_params);
            pt_enabled = true;
        }
        bt_params.enable_tracking = true;
        sl::ERROR_CODE err = zed.enableBodyTracking(bt_params);
        if (err != sl::ERROR_CODE::SUCCESS) {
            std::cerr << "Failed to enable Body Tracking: " << err << std::endl;
            zed.close();
            return 1;
        }
        std::cout << "Body Tracking enabled (" << bt_model_str << ")" << std::endl;
    }

    // Barrier sync: wait for all processes before starting
    sync_barrier_signal_and_wait(sync_dir);

    sl::RuntimeParameters runtime_params;
    sl::Mat image, depth;
    sl::Objects objects;
    sl::Bodies bodies;
    sl::Pose pose;
    sl::ObjectDetectionRuntimeParameters od_runtime_params;
    sl::BodyTrackingRuntimeParameters bt_runtime_params;

    // Warmup. Skip on SVO playback: there is no hardware to warm up, and warmup would consume
    // frames from the file (svo_real_time_mode is off), shrinking the benchmark window.
    if (warmup > 0.f && svo_input.empty()) {
        std::cout << "\nStarting warmup loop for " << warmup << " seconds..." << std::endl;
        auto warmup_start_time = std::chrono::high_resolution_clock::now();
        while (is_running) {
            auto current_time = std::chrono::high_resolution_clock::now();
            std::chrono::duration<double> elapsed = current_time - warmup_start_time;
            if (elapsed.count() > warmup) {
                std::cout << "Warmup finished." << std::endl;
                break;
            }
            if (zed.grab(runtime_params) > sl::ERROR_CODE::SUCCESS)
                std::this_thread::sleep_for(std::chrono::milliseconds(1));
        }
    } else if (warmup > 0.f) {
        std::cout << "\nSVO input detected: skipping warmup." << std::endl;
    }

    // Enable scoped profiling after warmup so warmup grabs are not measured.
    bench::Profiler::enable();
    const std::string sn = std::to_string(zed.getCameraInformation().serial_number);

    // Profiled section names are "camera_sn_<sn>_<function>(...)"; precompute them once so
    // the grab loop never allocates a string per call.
    const std::string prof_read = "camera_sn_" + sn + "_read(...)";
    const std::string prof_grab = "camera_sn_" + sn + "_grab(...)";
    const std::string prof_period = "camera_sn_" + sn + "_grab_period";
    const std::string prof_img = "camera_sn_" + sn + "_retrieveImage(...)";
    // Latency sections, fed with addSample() rather than start()/stop().
    const std::string prof_g2i = "camera_sn_" + sn + "_glass_to_image";
    const std::string prof_g2g = "camera_sn_" + sn + "_glass_to_glass";
    const std::string prof_measure = "camera_sn_" + sn + "_retrieveMeasure(...)";
    const std::string prof_pos = "camera_sn_" + sn + "_getPosition(...)";
    const std::string prof_obj = "camera_sn_" + sn + "_retrieveObjects(...)";
    const std::string prof_bodies = "camera_sn_" + sn + "_retrieveBodies(...)";

    int frames_count = 0;
    auto start_time = clk::now();
    bool period_open = false;
    // IMU monitoring state
    size_t imu_sample_count = 0;
    std::vector<double> imu_per_frame;
    if (monitor_imu)
        imu_per_frame.reserve(std::max(1, (int)(duration * camera_fps * 1.1)));

#ifdef HAVE_G2G
    std::unique_ptr<g2g::Display> g2g_display;
    std::unique_ptr<g2g::AsyncDecoder> g2g_async;   // live capture: decode off the grab loop (may drop frames)
    std::unique_ptr<g2g::Decoder> g2g_sync_decoder; // SVO playback: decode every frame inline (repeatable)
    if (measure_glass_to_glass_latency) {
        // On SVO playback the filmed screen is already baked into the recorded frames, so we
        // decode those rather than driving a live screen, no display window is opened. The
        // window is only useful for live capture.
        bool g2g_ok = true;
        if (svo_input.empty()) {
            g2g_display.reset(new g2g::Display());
            g2g_display->setCameras({sn}); // single readout row, labeled by serial
            if (!g2g_display->start()) {
                std::cerr << "[G2G] Failed to open the display window; disabling G2G." << std::endl;
                g2g_display.reset();
                g2g_ok = false;
            }
        } else {
            std::cout << "[G2G] SVO playback: decoding the recorded screen; no display window opened." << std::endl;
        }
        if (g2g_ok) {
            std::cout << "[G2G] enabled" << std::endl;
            if (svo_input.empty()) {
                // Live capture: decode on a worker thread so it never inflates the grab loop period.
                g2g_async.reset(new g2g::AsyncDecoder());
                g2g_async->start(g2g_display.get(), sn, (size_t)std::max(1, (int)(duration * camera_fps * 1.1)));
            } else {
                // SVO playback: decode synchronously on EVERY frame so two replays of the same SVO
                // produce the exact same G2G samples (the async path drops a timing-dependent subset).
                g2g_sync_decoder.reset(new g2g::Decoder());
            }
        }
    }
#endif

    std::cout << "\nStarting benchmark loop for " << duration << " seconds. Press Ctrl+C to stop." << std::endl;

    while (is_running) {
        auto current_time = clk::now();
        std::chrono::duration<double> elapsed = current_time - start_time;
        // SVO playback ignores --duration: process the whole file so two replays of the same SVO
        // cover the exact same frames (repeatability). Live capture stays time-bounded.
        if (svo_input.empty() && duration > 0 && elapsed.count() > duration) {
            std::cout << "\nBenchmark finished (duration reached)." << std::endl;
            break;
        }

        // Split capture: read() pulls + rectifies the frame (the blocking step); grab() then
        // runs the SDK measurements. Each is profiled separately (.read() /
        // .grab()) and summed in the report into the full capture+process "Grab" cost.
        const long long before_read_ns = (long long)zed.getTimestamp(sl::TIME_REFERENCE::CURRENT).getNanoseconds();

        bench::Profiler::start(prof_read);
        auto read_err = zed.read();
        bench::Profiler::stop(prof_read);

        const long long after_read_ns = (long long)zed.getTimestamp(sl::TIME_REFERENCE::CURRENT).getNanoseconds();

        bench::Profiler::start(prof_grab);
        auto grab_err = zed.grab(runtime_params);
        bench::Profiler::stop(prof_grab);

        if (grab_err == sl::ERROR_CODE::SUCCESS) {
            const sl::Timestamp image_ts = zed.getTimestamp(sl::TIME_REFERENCE::IMAGE);

            const long long capture_ref_ns = svo_input.empty() ? (long long)image_ts.getNanoseconds() : before_read_ns;

            // Recording: store the raw after-read "now" keyed by the IMAGE timestamp,
            // both truncated to integer milliseconds.
            if (record_live_ts) {
                live_ts_map[image_ts] = sl::Timestamp((uint64_t)after_read_ns);
                last_image_ts = image_ts;
            }

            if (period_open)
                bench::Profiler::stop(prof_period);
            bench::Profiler::start(prof_period);
            period_open = true;

            // Use Resolution(-1,-1) for optimal working resolution
            const long long before_img_ns = (long long)zed.getTimestamp(sl::TIME_REFERENCE::CURRENT).getNanoseconds();
            bench::Profiler::start(prof_img);
            zed.retrieveImage(image, sl::VIEW::LEFT, sl::MEM::CPU, sl::Resolution(-1, -1));
            bench::Profiler::stop(prof_img);
            const long long after_img_ns = (long long)zed.getTimestamp(sl::TIME_REFERENCE::CURRENT).getNanoseconds();

            // Glass-to-image latency: reference -> end of read(), plus retrieveImage(). grab() runs
            // in between and is excluded.
            bench::Profiler::addSample(prof_g2i, ((after_read_ns - capture_ref_ns) + (after_img_ns - before_img_ns)) / 1.0e6);
#ifdef HAVE_G2G
            // Glass-to-glass: offload the just-grabbed frame to the async decoder worker
            // so the expensive decode never inflates this grab loop. The "now" timestamp
            // is the after-read one sampled above. We submit a deep copy of the frame; the
            // worker then decodes it, computes the latency, and updates the display.
            if (measure_glass_to_glass_latency) {
                long long g2g_now_ns = after_read_ns;
                bool g2g_now_ok = true;
                if (playback_live_ts) {
                    auto it = live_ts_map.find(image_ts);
                    if (it == live_ts_map.end()) {
                        std::cerr << "[LiveTS] No correspondence for IMAGE ts " << image_ts.getNanoseconds()
                                  << "; no G2G sample for this frame." << std::endl;
                        g2g_now_ok = false;
                    } else {
                        g2g_now_ns = (long long)it->second.getNanoseconds();
                    }
                }
                if (g2g_now_ok && g2g_async) {
                    g2g_async->submit(image, g2g_now_ns);
                } else if (g2g_now_ok && g2g_sync_decoder) {
                    // SVO: decode every frame here (no async drops) for a repeatable result.
                    g2g::DecodeResult r = g2g_sync_decoder->decode(image, (uint64_t)g2g_now_ns);
                    if (r.status == g2g::Status::LOCKED) {
                        const double g2g_lat_ms = (g2g_now_ns - (long long)r.display_ns) / 1.0e6;
                        if (g2g_lat_ms > 0.0) // drop non-positive latency
                            bench::Profiler::addSample(prof_g2g, g2g_lat_ms);
                    }
                }
            }
#endif

            if (init_params.depth_mode != sl::DEPTH_MODE::NONE) {
                bench::Profiler::start(prof_measure);
                zed.retrieveMeasure(depth, sl::MEASURE::XYZ, sl::MEM::CPU, sl::Resolution(-1, -1));
                bench::Profiler::stop(prof_measure);
            }

            if (pt_enabled) {
                bench::Profiler::start(prof_pos);
                zed.getPosition(pose, sl::REFERENCE_FRAME::WORLD);
                bench::Profiler::stop(prof_pos);
            }

            if (!od_model_str.empty()) {
                bench::Profiler::start(prof_obj);
                zed.retrieveObjects(objects, od_runtime_params);
                bench::Profiler::stop(prof_obj);
            }

            if (!bt_model_str.empty()) {
                bench::Profiler::start(prof_bodies);
                zed.retrieveBodies(bodies, bt_runtime_params);
                bench::Profiler::stop(prof_bodies);
            }

            frames_count++;
            if (monitor_imu) {
                std::vector<sl::SensorsData> batch;
                auto sens_err = zed.getSensorsDataBatch(batch);
                if (sens_err == sl::ERROR_CODE::SUCCESS) {
                    imu_sample_count += batch.size();
                    imu_per_frame.push_back(static_cast<double>(batch.size()));
                } else {
                    imu_per_frame.push_back(0.0);
                }
            }
            if (frames_count % camera_fps == 0) {
                printf("\rFPS: %.2f | Frames: %d", zed.getCurrentFPS(), frames_count);
                fflush(stdout);
            }
        } else {
            // End of SVO: stop here instead of spinning. This both terminates the otherwise
            // unbounded SVO run and keeps the reported frequency free of a post-EOF idle tail.
            if (!svo_input.empty() && grab_err == sl::ERROR_CODE::END_OF_SVOFILE_REACHED) {
                std::cout << "\nBenchmark finished (end of SVO reached)." << std::endl;
                break;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(1));
        }
    }
    bench::Profiler::stop(prof_period);

    printf("\nFinal FPS: %.2f\n", zed.getCurrentFPS());

    auto end_time = clk::now();
    const double total_s = std::chrono::duration<double>(end_time - start_time).count();

#ifdef HAVE_G2G
    // Stop + join the async decoder before the profiler dump, so every in-flight latency sample
    // is collected; then hand its latencies to the profiler.
    if (g2g_async) {
        g2g_async->stop();
        for (double lat_ms : g2g_async->takeLatencies())
            bench::Profiler::addSample(prof_g2g, lat_ms);
        g2g_async.reset();
    }
#endif

    // Dump profiler stats while the camera is still open (closing it resets the
    // profiler), then reshape into the historical report schema.
    const std::string raw_report = output_report + ".profiler.json";
    bench::Profiler::dumpStats(raw_report);

    // Persist the live-timestamp correspondence map into the SVO before recording is
    // disabled (ingestDataIntoSVO is only valid while recording).
    if (record_live_ts)
        ingest_live_ts_map(zed, live_ts_map, last_image_ts);

    if (!od_model_str.empty())
        zed.disableObjectDetection();
    if (!bt_model_str.empty())
        zed.disableBodyTracking();
    if (pt_enabled)
        zed.disablePositionalTracking();
    if (!compression_rec_str.empty())
        zed.disableRecording();
    if (!compression_stream_str.empty())
        zed.disableStreaming();
    zed.close();

    write_profiler_report(raw_report, output_report, sn, frames_count, total_s, compression_rec_str, !encryption_key_str.empty());
    std::error_code ec;
    std::filesystem::remove(raw_report, ec);

#ifdef HAVE_G2G
    if (g2g_display)
        g2g_display->stop();
#endif

    // Append IMU data to the benchmark report
    if (monitor_imu && imu_sample_count > 0)
        append_imu_json(output_report, "IMU_SN" + sn, imu_sample_count, total_s, imu_per_frame);

    return 0;
}

int main(int argc, char** argv) {
    // Arguments
    int camera_fps = 30;
    std::string svo_input = "";
    std::string resolution_str = "HD1080";
    std::string depth_mode_str = "NONE";
    std::string od_model_str = "";
    std::string od_custom_onnx_file = "";
    std::string od_custom_onnx_input_shape = "";
    std::string bt_model_str = "";
    std::string compression_rec_str = "";
    std::string encoding_preset_str = "";
    std::string encryption_key_str = "";
    std::string output_svo = "benchmark_recording.svo";
    std::string output_report = "benchmark_report.json";
    std::string compression_stream_str = "";
    std::string sync_dir = "";
    bool enable_positional_tracking = false;
    bool use_camera_one = false;
    bool monitor_imu = true;
    bool measure_glass_to_glass_latency = false;
    unsigned int serial_number = 0;
    int streaming_port = 30000;
    float duration = 60.0;
    float warmup = 5.0;

    // Parse command line arguments
    static struct option long_options[] = {
        {"svo_input",                  required_argument, 0, 'i'},
        {"camera_fps",                 required_argument, 0, 'f'},
        {"resolution",                 required_argument, 0, 'r'},
        {"depth_mode",                 required_argument, 0, 'd'},
        {"od_model",                   required_argument, 0, 'o'},
        {"od_custom_onnx_file",        required_argument, 0, 'O'},
        {"od_custom_onnx_input_shape", required_argument, 0, 'I'},
        {"bt_model",                   required_argument, 0, 'b'},
        {"enable_positional_tracking", no_argument,       0, 'p'},
        {"compression_mode_recording", required_argument, 0, 'c'},
        {"encoding_preset",            required_argument, 0, 'e'},
        {"encryption_key",             required_argument, 0, 'E'},
        {"output_svo",                 required_argument, 0, 's'},
        {"compression_mode_streaming", required_argument, 0, 't'},
        {"serial_number",              required_argument, 0, 'S'},
        {"streaming_port",             required_argument, 0, 'P'},
        {"duration",                   required_argument, 0, 'u'},
        {"warmup",                     required_argument, 0, 'w'},
        {"benchmark",                  required_argument, 0, 'm'},
        {"camera_one",                 no_argument,       0, '1'},
        {"sync-dir",                   required_argument, 0, 'y'},
        {"no_monitor_imu",             no_argument,       0, 'N'},
        {"glass_to_glass_latency",     no_argument,       0, 'G'},
        {"help",                       no_argument,       0, 'h'},
        {0,                            0,                 0, 0  }
    };

    int opt;
    int option_index = 0;
    while ((opt = getopt_long(argc, argv, "i:f:r:d:w:o:O:I:b:c:e:E:s:t:S:P:pu:m:1y:NGh", long_options, &option_index)) != -1) {
        switch (opt) {
            case 'i':
                svo_input = optarg;
                break;
            case 'f':
                camera_fps = (int)parse_int_arg(optarg, "--camera_fps");
                if (camera_fps < 1) {
                    std::cerr << "Error: --camera_fps must be >= 1" << std::endl;
                    return 1;
                }
                break;
            case 'r':
                resolution_str = optarg;
                break;
            case 'd':
                depth_mode_str = optarg;
                break;
            case 'o':
                od_model_str = optarg;
                break;
            case 'O':
                od_custom_onnx_file = optarg;
                break;
            case 'I':
                od_custom_onnx_input_shape = optarg;
                break;
            case 'b':
                bt_model_str = optarg;
                break;
            case 'c':
                compression_rec_str = optarg;
                break;
            case 'e':
                encoding_preset_str = optarg;
                break;
            case 'E':
                encryption_key_str = optarg;
                break;
            case 's':
                output_svo = optarg;
                break;
            case 't':
                compression_stream_str = optarg;
                break;
            case 'p':
                enable_positional_tracking = true;
                break;
            case 'S':
                serial_number = (unsigned int)parse_int_arg(optarg, "--serial_number");
                break;
            case 'P':
                streaming_port = (int)parse_int_arg(optarg, "--streaming_port");
                break;
            case 'w':
                warmup = (float)parse_float_arg(optarg, "--warmup");
                break;
            case 'u':
                duration = (float)parse_float_arg(optarg, "--duration");
                break;
            case 'm':
                output_report = optarg;
                break;
            case '1':
                use_camera_one = true;
                break;
            case 'y':
                sync_dir = optarg;
                break;
            case 'N':
                monitor_imu = false;
                break;
            case 'G':
                measure_glass_to_glass_latency = true;
                break;
            case 'h':
                printHelp();
                return 0;
            default:
                // Unknown option or missing required argument (getopt returns '?'). Exit non-zero
                // so a typo'd flag fails the run instead of silently exiting 0 with no benchmark.
                printHelp();
                return 2;
        }
    }

#ifndef HAVE_G2G
    if (measure_glass_to_glass_latency) {
        std::cerr << "[G2G] --glass_to_glass_latency requested but this build has no G2G support.\n"
                  << "      Rebuild with GLFW + OpenGL + OpenCV (see cpp/README.md). Continuing without G2G." << std::endl;
        measure_glass_to_glass_latency = false;
    }
#endif

    if (use_camera_one) {
        std::cout << "[CameraOne] Mono camera detected. Stereo-only features (depth, OD, BT, tracking) are not available." << std::endl;
        return runMonoBenchmark(
            camera_fps,
            resolution_str,
            svo_input,
            compression_rec_str,
            encoding_preset_str,
            encryption_key_str,
            output_svo,
            compression_stream_str,
            serial_number,
            streaming_port,
            warmup,
            duration,
            sync_dir,
            output_report,
            monitor_imu,
            measure_glass_to_glass_latency
        );
    } else {
        return runStereoBenchmark(
            camera_fps,
            resolution_str,
            svo_input,
            depth_mode_str,
            od_model_str,
            od_custom_onnx_file,
            od_custom_onnx_input_shape,
            bt_model_str,
            compression_rec_str,
            encoding_preset_str,
            encryption_key_str,
            output_svo,
            compression_stream_str,
            enable_positional_tracking,
            serial_number,
            streaming_port,
            warmup,
            duration,
            sync_dir,
            output_report,
            monitor_imu,
            measure_glass_to_glass_latency
        );
    }
}
