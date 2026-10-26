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
#include <mutex>
#include <csignal>
#include "getopt_compat.h"
#include "glass_to_glass.hpp"
#include <iomanip>
#include <cstdio>
#include <fstream>
#include <filesystem>
#include <system_error>
#include <sstream>
#include <algorithm>
#include <numeric>
#include <cmath>
#include <memory>
#include <map>
#include <cstdint>
#include <cstdlib>

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
    std::cout << "Usage: benchmark_multi_cam [options]\n";
    std::cout << "Options:\n";
    std::cout << "  --num_stereo <n>                 Number of stereo (ZED X) cameras (default: all detected)\n";
    std::cout << "  --num_mono <n>                   Number of mono (ZED X One) cameras (default: all detected)\n";
    std::cout << "  --svo_input <paths>              SVO file(s), comma-separated, one per camera (default: live)\n";
    std::cout << "  --camera_fps <fps>               Camera FPS (default: 30)\n";
    std::cout << "  --resolution <res>               Resolution (default: HD1080)\n";
    std::cout << "  --depth_mode <mode>              Depth Mode - stereo only (default: NONE)\n";
    std::cout << "  --od_model <model>               Object Detection Model - stereo only (default: OFF)\n";
    std::cout << "  --od_custom_onnx_file <path>     Path to custom ONNX file (requires od_model=CUSTOM_YOLOLIKE_BOX_OBJECTS)\n";
    std::cout << "  --od_custom_onnx_input_shape <WxH> Input resolution for custom ONNX (e.g. 640x640, default: 512x512)\n";
    std::cout << "  --bt_model <model>               Body Tracking Model - stereo only (default: OFF)\n";
    std::cout << "  --compression_mode_recording <m> Compression Mode for Recording (default: OFF)\n";
    std::cout << "  --encoding_preset <preset>       Encoding preset: DEFAULT, ULTRAFAST, FAST, MEDIUM, SLOW (default: DEFAULT)\n";
    std::cout << "  --encryption_key <key>           Encryption key/passphrase for SVO recording (default: empty)\n";
    std::cout << "  --compression_mode_streaming <m> Compression Mode for Streaming (default: OFF)\n";
    std::cout << "  --enable_positional_tracking     Enable Positional Tracking - stereo only (default: OFF)\n";
    std::cout << "  --duration <seconds>             Duration in seconds (default: 60)\n";
    std::cout << "  --warmup <seconds>               Warmup duration in seconds (default: 5.0)\n";
    std::cout << "  --benchmark <filename>           Benchmark report filename (default: benchmark_report.json)\n";
    std::cout << "  --no_monitor_imu                 Disable IMU monitoring (default: ON)\n";
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

// Holds either a stereo Camera or a mono CameraOne
struct CameraHandle {
    sl::Camera* stereo = nullptr;
    sl::CameraOne* mono = nullptr;
    bool is_mono = false;
    unsigned int serial_number = 0;
    bool recording_enabled = false; // true only if enableRecording() actually succeeded

    void close() {
        if (is_mono && mono) {
            mono->close();
            delete mono;
            mono = nullptr;
        }
        if (!is_mono && stereo) {
            stereo->close();
            delete stereo;
            stereo = nullptr;
        }
    }
};

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
    // Anchor the blob at a real in-range frame timestamp (the last grabbed IMAGE ts)
    // so retrieveSVOData finds it.
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

// Round to 3 decimals so JSON numbers stay tidy (matches the Python report's round(x, 3)).
double round3(double v) {
    return std::round(v * 1000.0) / 1000.0;
}

// ── Profiler report ──────────────────────────────────────────────────────────
void write_profiler_report(
    const std::string& dump_file,
    const std::string& out_file,
    const std::vector<std::string>& serials,
    const std::vector<std::atomic<int>>& frames_counts,
    const std::vector<double>& total_seconds // per-camera elapsed (cameras may finish at different times in SVO playback)
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

    const bool has_profiles = dump.contains("profile_stats") && dump["profile_stats"].is_object();
    // All cameras profile into the default context; each section name is
    // "camera_sn_<sn>_<function>(...)", so a single context holds every camera's metrics.
    sl::json profiles = sl::json::object();
    if (has_profiles && dump["profile_stats"].contains("default") && dump["profile_stats"]["default"].is_object())
        profiles = dump["profile_stats"]["default"];
    sl::json report = sl::json::object();

    for (size_t i = 0; i < serials.size(); ++i) {
        const std::string& sn = serials[i];
        auto prof_key = [&](const std::string& fn) {
            return "camera_sn_" + sn + "_" + fn;
        };

        // read() and grab() are profiled as two separate sections; the reported "Grab" cost is
        // their field-by-field sum, so it reflects the full capture+process time.
        const sl::json read_s = profiles.contains(prof_key("read(...)")) ? profiles[prof_key("read(...)")] : sl::json::object();
        const sl::json grab_s = profiles.contains(prof_key("grab(...)")) ? profiles[prof_key("grab(...)")] : sl::json::object();
        sl::json period = profiles.contains(prof_key("grab_period")) ? profiles[prof_key("grab_period")] : sl::json::object();
        const double freq = (total_seconds[i] > 0.0) ? frames_counts[i].load(std::memory_order_relaxed) / total_seconds[i] : 0.0;
        auto grab_sum = [&](const char* k) {
            return read_s.value(k, 0.0) + grab_s.value(k, 0.0);
        };

        report["Grab_SN" + sn] = {
            {"grab_call_frequency_hz", freq},
            {"grab_call_mean_period_ms", period.value("mean", 0.0)},
            {"grab_call_median_period_ms", period.value("median", 0.0)},
            {"cpu_stats",
             {{"mean", grab_sum("mean")},
              {"median", grab_sum("median")},
              {"stddev", grab_sum("stddev")},
              {"min", grab_sum("min")},
              {"max", grab_sum("max")},
              {"p99", grab_sum("p99")},
              {"nb_samples", grab_s.value("count", read_s.value("count", 0))}}}
        };

        // Per-operation sections (only those actually profiled for this camera appear). The profiled
        // section is "camera_sn_<sn>_<function>(...)"; the report key is "<ReportName>_SN<sn>", so each
        // camera's per-event timings stay serial-keyed and coherent with the single-camera report
        // schema. A mono camera only profiles retrieveImage, so only that key appears for it.
        auto event_stats = [&](const std::string& section) -> sl::json {
            const sl::json s = profiles.contains(section) ? profiles[section] : sl::json::object();
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
                    {"cpu_stats", event_stats(prof_key(ev.first))}
                };
        }

        // Latency sections, fed to the profiler with addSample() (see the grab loop). Statistics are
        // computed over the WHOLE sample set: the G2G decoder only emits a sample after the per-column
        // agreement + CRC + plausibility checks pass, so mis-detections are rejected at decode time
        // and no statistical tail-trim is needed.
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
    }

    std::ofstream f(out_file, std::ios::trunc);
    if (f.is_open()) {
        f << report.dump(4) << "\n";
        std::cout << "[Profiler] Results written to " << out_file << std::endl;
    }
}

// Split a comma-separated --svo_input value into individual paths (one per camera),
// trimming surrounding whitespace and dropping empty entries.
std::vector<std::string> split_svo_paths(const std::string& csv) {
    std::vector<std::string> paths;
    std::stringstream ss(csv);
    std::string item;
    while (std::getline(ss, item, ',')) {
        size_t a = item.find_first_not_of(" \t");
        size_t b = item.find_last_not_of(" \t");
        if (a != std::string::npos)
            paths.push_back(item.substr(a, b - a + 1));
    }
    return paths;
}

// Recover whether an SVO was recorded from a mono camera. There is no way to ask the SDK
// without opening (a mono SVO opens only via CameraOne, a stereo one only via Camera), so we
// read the type token this tool writes into the recorded filename
// (benchmark_output_<stereo|mono>_<SN>.svo2). Defaults to stereo when the token is absent.
bool svo_path_is_mono(const std::string& path) {
    std::string name = std::filesystem::path(path).filename().string();
    std::transform(name.begin(), name.end(), name.begin(), [](unsigned char c) {
        return std::tolower(c);
    });
    return name.find("_mono") != std::string::npos;
}

int main(int argc, char** argv) {
    int num_stereo = -1; // -1 = use all detected
    int num_mono = -1;   // -1 = use all detected
    std::string svo_input_arg = "";
    int camera_fps = 30;
    std::string resolution_str = "HD1080";
    std::string depth_mode_str = "NONE";
    std::string od_model_str = "";
    std::string od_custom_onnx_file = "";
    std::string od_custom_onnx_input_shape = "";
    std::string bt_model_str = "";
    std::string compression_rec_str = "";
    std::string encoding_preset_str = "";
    std::string encryption_key_str = "";
    std::string compression_stream_str = "";
    std::string output_report = "benchmark_report.json";
    bool enable_positional_tracking = false;
    bool monitor_imu = true;
    bool measure_glass_to_glass_latency = false;
    float duration = 60.0;
    float warmup = 5.0;

    static struct option long_options[] = {
        {"num_stereo",                 required_argument, 0, 's'},
        {"num_mono",                   required_argument, 0, 'm'},
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
        {"compression_mode_streaming", required_argument, 0, 't'},
        {"duration",                   required_argument, 0, 'u'},
        {"warmup",                     required_argument, 0, 'w'},
        {"benchmark",                  required_argument, 0, 'k'},
        {"no_monitor_imu",             no_argument,       0, 'N'},
        {"glass_to_glass_latency",     no_argument,       0, 'G'},
        {"help",                       no_argument,       0, 'h'},
        {0,                            0,                 0, 0  }
    };

    int opt;
    int option_index = 0;
    while ((opt = getopt_long(argc, argv, "i:f:r:d:o:O:I:b:c:e:E:t:pu:w:s:m:k:NGh", long_options, &option_index)) != -1) {
        switch (opt) {
            case 'i':
                svo_input_arg = optarg;
                break;
            case 's':
                num_stereo = (int)parse_int_arg(optarg, "--num_stereo");
                break;
            case 'm':
                num_mono = (int)parse_int_arg(optarg, "--num_mono");
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
            case 't':
                compression_stream_str = optarg;
                break;
            case 'p':
                enable_positional_tracking = true;
                break;
            case 'w':
                warmup = (float)parse_float_arg(optarg, "--warmup");
                break;
            case 'u':
                duration = (float)parse_float_arg(optarg, "--duration");
                break;
            case 'k':
                output_report = optarg;
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

    // Comma-separated --svo_input → one SVO path per camera (empty = live capture).
    std::vector<std::string> svo_inputs = split_svo_paths(svo_input_arg);

    // Discover all connected cameras (stereo + mono)
    struct DeviceInfo {
        unsigned int serial_number;
        bool is_mono;
        std::string svo_path; // empty for live capture
    };
    std::vector<DeviceInfo> all_devices;
    int total_cameras = 0;

    if (svo_inputs.empty()) {
        auto stereo_devs = sl::Camera::getDeviceList();
        auto mono_devs = sl::CameraOne::getDeviceList();

        // Clamp requested counts to available cameras
        int avail_stereo = (int)stereo_devs.size();
        int avail_mono = (int)mono_devs.size();

        if (num_stereo < 0)
            num_stereo = avail_stereo; // -1 = use all detected
        num_stereo = std::min(num_stereo, avail_stereo);

        if (num_mono < 0)
            num_mono = avail_mono; // -1 = use all detected
        num_mono = std::min(num_mono, avail_mono);

        total_cameras = num_stereo + num_mono;

        if (total_cameras <= 0) {
            std::cerr << "No ZED cameras detected (or requested 0)." << std::endl;
            return 1;
        }

        // Build device list: stereo first, then mono
        for (int s = 0; s < num_stereo; s++)
            all_devices.push_back({stereo_devs[s].serial_number, false, ""});
        for (int m = 0; m < num_mono; m++)
            all_devices.push_back({mono_devs[m].serial_number, true, ""});

        std::cout << "Detected " << avail_stereo << " stereo + " << avail_mono << " mono camera(s). Using " << num_stereo << " stereo + "
                  << num_mono << " mono (" << total_cameras << " total)." << std::endl;
    } else {
        // SVO playback: one camera per input file. The mono/stereo type is recovered from the
        // filename token written at record time (see svo_path_is_mono); --num_stereo/--num_mono
        // are ignored here since the file set defines the topology.
        for (const auto& path : svo_inputs) {
            bool is_mono = svo_path_is_mono(path);
            all_devices.push_back({0, is_mono, path});
        }
        total_cameras = (int)all_devices.size();
        num_mono = (int)std::count_if(all_devices.begin(), all_devices.end(), [](const DeviceInfo& d) {
            return d.is_mono;
        });
        num_stereo = total_cameras - num_mono;
        std::cout << "SVO playback: " << total_cameras << " file(s) (" << num_stereo << " stereo + " << num_mono << " mono)." << std::endl;
    }

    // Parse common parameters
    sl::DEPTH_MODE depth_mode = sl::DEPTH_MODE::NONE;
    sl::RESOLUTION cam_resolution = sl::RESOLUTION::HD1080;
    if (!resolution_str.empty())
        sl::fromString(resolution_str.c_str(), cam_resolution);
    if (!depth_mode_str.empty())
        sl::fromString(depth_mode_str.c_str(), depth_mode);

    sl::SVO_COMPRESSION_MODE rec_compression = sl::SVO_COMPRESSION_MODE::H265;
    if (!compression_rec_str.empty())
        sl::fromString(compression_rec_str.c_str(), rec_compression);

    sl::STREAMING_CODEC stream_codec = sl::STREAMING_CODEC::H265;
    if (!compression_stream_str.empty())
        sl::fromString(compression_stream_str.c_str(), stream_codec);

    if (!od_model_str.empty() || !bt_model_str.empty())
        enable_positional_tracking = true;

    signal(SIGINT, signal_handler);

    // Open all cameras
    std::vector<CameraHandle> cameras(total_cameras);
    for (int i = 0; i < total_cameras; i++) {
        auto& dev = all_devices[i];
        cameras[i].is_mono = dev.is_mono;
        cameras[i].serial_number = dev.serial_number;

        if (dev.is_mono) {
            // --- Open as CameraOne ---
            cameras[i].mono = new sl::CameraOne();
            sl::InitParametersOne init_one;
            init_one.camera_resolution = cam_resolution;
            init_one.camera_fps = camera_fps;
            if (!dev.svo_path.empty()) {
                init_one.input.setFromSVOFile(dev.svo_path.c_str());
                init_one.svo_real_time_mode = false;
                if (!encryption_key_str.empty())
                    init_one.svo_decryption_key = sl::String(encryption_key_str.c_str());
            } else {
                init_one.input.setFromSerialNumber(dev.serial_number);
            }

            std::cout << "[Cam " << i << "] Opening CameraOne (SN: " << dev.serial_number << ")..." << std::endl;
            sl::ERROR_CODE status = cameras[i].mono->open(init_one);
            if (status != sl::ERROR_CODE::SUCCESS) {
                std::cerr << "[Cam " << i << "] Failed to open CameraOne: " << status << std::endl;
                for (int j = 0; j < i; j++)
                    cameras[j].close();
                return 1;
            }
            auto info = cameras[i].mono->getCameraInformation();
            // Recover the real serial from the opened camera/SVO header. On SVO playback the device
            // list carries SN 0 (see all_devices), so without this every camera collapses onto the
            // same "_SN0" report keys and profiler sections, merging their metrics.
            cameras[i].serial_number = info.serial_number;
            std::cout << "[Cam " << i << "] Opened CameraOne (SN: " << cameras[i].serial_number << ", " << sl::toString(info.camera_model)
                      << ")" << std::endl;

            // Recording (mono)
            if (!compression_rec_str.empty()) {
                sl::RecordingParameters rec_params;
                rec_params.compression_mode = rec_compression;
                if (!encoding_preset_str.empty())
                    sl::fromString(encoding_preset_str.c_str(), rec_params.encoding_preset);
                // Type token (mono/stereo) lets --svo_input recover the camera class at playback.
                std::string filename = "benchmark_output_mono_" + std::to_string(dev.serial_number) + ".svo2";
                rec_params.video_filename = sl::String(filename.c_str());
                if (!encryption_key_str.empty())
                    rec_params.encryption_key = sl::String(encryption_key_str.c_str());
                if (cameras[i].mono->enableRecording(rec_params) != sl::ERROR_CODE::SUCCESS) {
                    std::cerr << "[Cam " << i << "] Failed to enable recording" << std::endl;
                } else {
                    cameras[i].recording_enabled = true;
                    std::cout << "[Cam " << i << "] Recording enabled" << (encryption_key_str.empty() ? "" : " (encrypted)") << " to "
                              << filename << std::endl;
                }
            }

            // Streaming (mono)
            if (!compression_stream_str.empty()) {
                sl::StreamingParameters sp;
                sp.codec = stream_codec;
                sp.port = 30000 + i * 2;
                if (cameras[i].mono->enableStreaming(sp) != sl::ERROR_CODE::SUCCESS)
                    std::cerr << "[Cam " << i << "] Failed to enable streaming" << std::endl;
                else
                    std::cout << "[Cam " << i << "] Streaming enabled (" << compression_stream_str << ")" << std::endl;
            }

            // Stereo-only features not available
            if (depth_mode != sl::DEPTH_MODE::NONE)
                std::cout << "[Cam " << i << "] [SKIP] Depth - mono camera" << std::endl;
            if (!od_model_str.empty())
                std::cout << "[Cam " << i << "] [SKIP] Object Detection - mono camera" << std::endl;
            if (!bt_model_str.empty())
                std::cout << "[Cam " << i << "] [SKIP] Body Tracking - mono camera" << std::endl;
            if (enable_positional_tracking)
                std::cout << "[Cam " << i << "] [SKIP] Positional Tracking - mono camera" << std::endl;

        } else {
            // --- Open as stereo Camera ---
            cameras[i].stereo = new sl::Camera();
            sl::InitParameters init_params;
            init_params.camera_resolution = cam_resolution;
            init_params.camera_fps = camera_fps;
            init_params.depth_mode = depth_mode;
            if (!dev.svo_path.empty()) {
                init_params.input.setFromSVOFile(dev.svo_path.c_str());
                init_params.svo_real_time_mode = false;
                if (!encryption_key_str.empty())
                    init_params.svo_decryption_key = sl::String(encryption_key_str.c_str());
            } else {
                init_params.input.setFromSerialNumber(dev.serial_number);
            }

            std::cout << "[Cam " << i << "] Opening Camera (SN: " << dev.serial_number << ")..." << std::endl;
            sl::ERROR_CODE status = cameras[i].stereo->open(init_params);
            if (status != sl::ERROR_CODE::SUCCESS) {
                std::cerr << "[Cam " << i << "] Failed to open Camera: " << status << std::endl;
                for (int j = 0; j < i; j++)
                    cameras[j].close();
                return 1;
            }
            // Recover the real serial from the opened camera/SVO header. On SVO playback the device
            // list carries SN 0 (see all_devices), so without this every camera collapses onto the
            // same "_SN0" report keys and profiler sections, merging their metrics.
            auto info = cameras[i].stereo->getCameraInformation();
            cameras[i].serial_number = info.serial_number;
            std::cout << "[Cam " << i << "] Opened Camera (SN: " << cameras[i].serial_number << ", " << sl::toString(info.camera_model)
                      << ")" << std::endl;

            // Recording (stereo)
            if (!compression_rec_str.empty()) {
                sl::RecordingParameters rec_params;
                rec_params.compression_mode = rec_compression;
                if (!encoding_preset_str.empty())
                    sl::fromString(encoding_preset_str.c_str(), rec_params.encoding_preset);
                // Type token (mono/stereo) lets --svo_input recover the camera class at playback.
                std::string filename = "benchmark_output_stereo_" + std::to_string(dev.serial_number) + ".svo2";
                rec_params.video_filename = sl::String(filename.c_str());
                if (!encryption_key_str.empty())
                    rec_params.encryption_key = sl::String(encryption_key_str.c_str());
                if (cameras[i].stereo->enableRecording(rec_params) != sl::ERROR_CODE::SUCCESS) {
                    std::cerr << "[Cam " << i << "] Failed to enable recording" << std::endl;
                } else {
                    cameras[i].recording_enabled = true;
                    std::cout << "[Cam " << i << "] Recording enabled" << (encryption_key_str.empty() ? "" : " (encrypted)") << " to "
                              << filename << std::endl;
                }
            }

            // Streaming (stereo)
            if (!compression_stream_str.empty()) {
                sl::StreamingParameters sp;
                sp.codec = stream_codec;
                sp.port = 30000 + i * 2;
                if (cameras[i].stereo->enableStreaming(sp) != sl::ERROR_CODE::SUCCESS)
                    std::cerr << "[Cam " << i << "] Failed to enable streaming" << std::endl;
                else
                    std::cout << "[Cam " << i << "] Streaming enabled (" << compression_stream_str << ")" << std::endl;
            }

            // Positional Tracking (stereo)
            bool pt_enabled = false;
            if (enable_positional_tracking) {
                sl::PositionalTrackingParameters pt_params;
                pt_params.enable_area_memory = true;
                if (cameras[i].stereo->enablePositionalTracking(pt_params) != sl::ERROR_CODE::SUCCESS)
                    std::cerr << "[Cam " << i << "] Failed to enable PT" << std::endl;
                else
                    pt_enabled = true;
            }

            // Object Detection (stereo)
            if (!od_model_str.empty()) {
                if (!pt_enabled) {
                    sl::PositionalTrackingParameters pt_params;
                    pt_params.enable_area_memory = true;
                    cameras[i].stereo->enablePositionalTracking(pt_params);
                    pt_enabled = true;
                }
                sl::ObjectDetectionParameters od_params;
                sl::fromString(od_model_str.c_str(), od_params.detection_model);
                if (!od_custom_onnx_file.empty()) {
                    od_params.custom_onnx_file = sl::String(od_custom_onnx_file.c_str());
                    if (!od_custom_onnx_input_shape.empty()) {
                        // Validate both tokens are positive integers; a malformed value (e.g. "640",
                        // "640x", "640xABC", negatives) must not crash via std::stoi nor silently
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
                            std::cerr << "[Cam " << i << "] Invalid --od_custom_onnx_input_shape '" << od_custom_onnx_input_shape
                                      << "' (expected positive WxH, e.g. 640x640); using default." << std::endl;
                    }
                    std::cout << "[Cam " << i << "] Custom ONNX: " << od_custom_onnx_file << " ("
                              << od_params.custom_onnx_dynamic_input_shape.width << "x" << od_params.custom_onnx_dynamic_input_shape.height
                              << ")" << std::endl;
                }
                od_params.enable_tracking = true;
                if (cameras[i].stereo->enableObjectDetection(od_params) != sl::ERROR_CODE::SUCCESS)
                    std::cerr << "[Cam " << i << "] Failed to enable OD" << std::endl;
            }

            // Body Tracking (stereo)
            if (!bt_model_str.empty()) {
                if (!pt_enabled) {
                    sl::PositionalTrackingParameters pt_params;
                    pt_params.enable_area_memory = true;
                    cameras[i].stereo->enablePositionalTracking(pt_params);
                    pt_enabled = true;
                }
                sl::BodyTrackingParameters bt_params;
                sl::fromString(bt_model_str.c_str(), bt_params.detection_model);
                bt_params.enable_tracking = true;
                if (cameras[i].stereo->enableBodyTracking(bt_params) != sl::ERROR_CODE::SUCCESS)
                    std::cerr << "[Cam " << i << "] Failed to enable BT" << std::endl;
            }
        }
    }

    // Report keys and profiler sections are disambiguated only by serial. If two cameras share one
    // (e.g. the same SVO passed twice, or two SVOs recorded from the same camera) their metrics
    // would silently merge into a single "_SN<sn>" block: warn rather than emit a malformed report.
    {
        std::map<unsigned int, int> sn_counts;
        for (int i = 0; i < total_cameras; i++)
            sn_counts[cameras[i].serial_number]++;
        for (const auto& kv : sn_counts)
            if (kv.second > 1)
                std::cerr << "[Warning] Serial " << kv.first << " is shared by " << kv.second
                          << " cameras; their metrics will be merged under a single \"_SN" << kv.first << "\" key in the report."
                          << std::endl;
    }

    std::vector<std::atomic<int>> frames_counts(total_cameras);
    std::vector<size_t> imu_sample_counts(total_cameras, 0);
    std::vector<std::vector<double>> imu_per_frame_all(total_cameras);

    // Live timestamp map (per camera): populated while recording live, then used to reconstruct
    // the G2G timestamps during SVO playback.
    //   key   = IMAGE timestamp inside SVO
    //   value = CURRENT-after-read "now", RAW (no G2G offset applied; the offset is
    //           display/machine-specific and re-measured + applied at playback)
    // Both key and value are stored as integer nanoseconds.
    const bool record_live_ts = !compression_rec_str.empty();
    const bool playback_live_ts = !svo_inputs.empty() && measure_glass_to_glass_latency;
    std::vector<std::map<sl::Timestamp, sl::Timestamp>> live_ts_maps(total_cameras);
    std::vector<sl::Timestamp> last_image_ts(total_cameras); // frame-ts anchor for each ingested blob

    // Recording was requested but failed to enable on one or more cameras (only logged per-camera
    // above). The recorded SVO set is then incomplete and those cameras' live-timestamp map can't be
    // ingested, so their G2G replay is unavailable: warn loudly rather than letting the run look
    // fully successful with silently missing output.
    if (record_live_ts) {
        int rec_failed = 0;
        for (int i = 0; i < total_cameras; i++)
            if (!cameras[i].recording_enabled)
                rec_failed++;
        if (rec_failed > 0)
            std::cerr << "[Warning] Recording failed on " << rec_failed << " of " << total_cameras
                      << " camera(s); the recorded SVO set is incomplete and those cameras' "
                         "glass-to-glass replay will be unavailable."
                      << std::endl;
    }

    // The image timestamp stored inside the SVO doesn't tell us when the image was actually
    // retrieved on the host, which G2G needs. To recover that, we decode the map embedded in each camera's SVO.
    // If a map isn't present, we return early to avoid reporting a bad measurement.
    if (playback_live_ts) {
        for (int i = 0; i < total_cameras; i++) {
            bool ok = cameras[i].is_mono ? load_live_ts_map(*cameras[i].mono, live_ts_maps[i])
                                         : load_live_ts_map(*cameras[i].stereo, live_ts_maps[i]);
            if (!ok) {
                for (int j = 0; j < total_cameras; j++)
                    cameras[j].close();
                return 1;
            }
        }
    }

    // Warmup. Skip on SVO playback: there is no hardware to warm up, and warmup would consume
    // frames from the file (svo_real_time_mode is off), shrinking the benchmark window.
    if (warmup > 0.f && svo_inputs.empty()) {
        std::cout << "\nStarting warmup loop for " << warmup << " seconds..." << std::endl;
        std::vector<std::thread> warmup_threads;
        for (int i = 0; i < total_cameras; i++) {
            warmup_threads.emplace_back([&, i]() {
                std::cout << "[Cam " << i << "] Warmup Started." << std::endl;
                auto warmup_start_time = std::chrono::high_resolution_clock::now();
                while (is_running) {
                    auto current_time = std::chrono::high_resolution_clock::now();
                    std::chrono::duration<double> elapsed = current_time - warmup_start_time;
                    if (elapsed.count() > warmup) {
                        std::cout << "[Cam " << i << "] Warmup Finished." << std::endl;
                        break;
                    }
                    sl::ERROR_CODE err;
                    if (cameras[i].is_mono)
                        err = cameras[i].mono->grab();
                    else
                        err = cameras[i].stereo->grab();
                    if (err > sl::ERROR_CODE::SUCCESS)
                        std::this_thread::sleep_for(std::chrono::milliseconds(1));
                }
            });
        }
        for (auto& t : warmup_threads)
            if (t.joinable())
                t.join();
    } else if (warmup > 0.f) {
        std::cout << "\nSVO input detected: skipping warmup." << std::endl;
    }

    // Enable scoped profiling after warmup so warmup grabs are not measured.
    bench::Profiler::enable();

    // Glass-to-glass: one shared display for the whole process; each camera worker decodes
    // the same on-screen timestamp pattern independently on its own async decoder thread.
#ifdef HAVE_G2G
    std::unique_ptr<g2g::Display> g2g_display;
    bool g2g_active = false;
    if (measure_glass_to_glass_latency) {
        // On SVO playback the filmed screen is already baked into the recorded frames, so we
        // decode those rather than driving a live screen, no display window is opened. The
        // window is only useful for live capture.
        bool g2g_ok = true;
        if (svo_inputs.empty()) {
            g2g_display.reset(new g2g::Display());
            // One on-screen readout row per camera, in worker-index order, labeled by serial.
            std::vector<std::string> g2g_labels(total_cameras);
            for (int i = 0; i < total_cameras; i++)
                g2g_labels[i] = std::to_string(cameras[i].serial_number);
            g2g_display->setCameras(g2g_labels);
            if (!g2g_display->start()) {
                std::cerr << "[G2G] Failed to open the display window; disabling G2G." << std::endl;
                g2g_display.reset();
                g2g_ok = false;
            }
        } else {
            std::cout << "[G2G] SVO playback: decoding the recorded screen; no display window opened." << std::endl;
        }
        if (g2g_ok && total_cameras > 0) {
            std::cout << "[G2G] enabled" << std::endl;
            g2g_active = true;
        }
    }
#endif

    auto start_time = std::chrono::high_resolution_clock::now();

    // SVO playback runs until every camera reaches end-of-file (see the status loop below); each
    // worker bumps this when its grab loop ends so the run can stop once all cameras are done.
    std::atomic<int> cameras_finished {0};

    // Per-camera end time. In SVO playback each camera runs its own file to EOF and finishes at a
    // different moment, so FPS/IMU-rate must divide each camera's counts by *its own* elapsed time,
    // not the shared window (which would understate every camera that finished before the longest).
    // In live capture all cameras stop together when is_running clears, so these all match the window.
    std::vector<std::chrono::high_resolution_clock::time_point> cam_end_times(total_cameras, start_time);

    // Main benchmark threads
    std::vector<std::thread> threads;
    for (int i = 0; i < total_cameras; i++) {
        threads.emplace_back([&, i]() {
            size_t& imu_count = imu_sample_counts[i];
            auto& imu_per_frame = imu_per_frame_all[i];
            imu_per_frame.reserve(std::max(1, (int)(duration * camera_fps * 1.1)));
            auto& live_ts_map = live_ts_maps[i];
            sl::Timestamp& last_img_ts = last_image_ts[i];
            sl::Mat image;
            const std::string sn = std::to_string(cameras[i].serial_number);
            // Profiled section names are "camera_sn_<sn>_<function>(...)" so every camera's
            // metrics coexist in the default context; precompute them once (no per-call alloc).
            const std::string prof_read = "camera_sn_" + sn + "_read(...)";
            const std::string prof_grab = "camera_sn_" + sn + "_grab(...)";
            const std::string prof_period = "camera_sn_" + sn + "_grab_period";
            const std::string prof_img = "camera_sn_" + sn + "_retrieveImage(...)";
            // Latency sections, fed with addSample() rather than start()/stop().
            const std::string prof_g2i = "camera_sn_" + sn + "_glass_to_image";
            const std::string prof_g2g = "camera_sn_" + sn + "_glass_to_glass";
#ifdef HAVE_G2G
            std::unique_ptr<g2g::AsyncDecoder> g2g_async;   // live: decode off the grab loop (may drop frames)
            std::unique_ptr<g2g::Decoder> g2g_sync_decoder; // SVO: decode every frame inline (repeatable)
            if (g2g_active) {
                if (svo_inputs.empty()) {
                    // Live capture: decode on a worker thread so it never inflates this camera's grab loop.
                    g2g_async.reset(new g2g::AsyncDecoder());
                    g2g_async->start(g2g_display.get(), sn, (size_t)std::max(1, (int)(duration * camera_fps * 1.1)), i);
                } else {
                    // SVO playback: decode synchronously on EVERY frame so two replays of the same SVO
                    // produce the exact same G2G samples (the async path drops a timing-dependent subset).
                    g2g_sync_decoder.reset(new g2g::Decoder());
                }
            }
#endif

            // Generic per-camera capture loop. Mono and stereo differ only in (a) how grab()
            // is invoked and (b) the extra SDK measurements run after the image is retrieved;
            // both are passed in as callables so the read/grab/timestamp/playback/record/G2I/
            // G2G/IMU plumbing lives in exactly one place. `zed` is deduced (sl::CameraOne* or
            // sl::Camera*): both expose the read()/grab()/getTimestamp()/retrieveImage()/
            // getSensorsDataBatch() interface this loop relies on.
            auto run_capture_loop = [&](auto* zed, auto&& do_grab, auto&& do_measurements) {
                bool period_open = false;
                while (is_running) {
                    // Split capture: read() pulls + rectifies the frame (the blocking step); grab()
                    // then runs the SDK measurements. Each is profiled separately (.read() /
                    // .grab()) and summed in the report into the full capture+process "Grab" cost.
                    //
                    // Latency samples: glass-to-image covers the time up to the end of read() plus
                    // retrieveImage() (reference: the image timestamp, or on SVO playback the host time
                    // right before read()). The after-read "now" is also the G2G reference.
                    const sl::Timestamp before_read = zed->getTimestamp(sl::TIME_REFERENCE::CURRENT);

                    bench::Profiler::start(prof_read);
                    auto read_err = zed->read();
                    bench::Profiler::stop(prof_read);

                    const sl::Timestamp after_read = zed->getTimestamp(sl::TIME_REFERENCE::CURRENT);

                    bench::Profiler::start(prof_grab);
                    auto grab_err = do_grab();
                    bench::Profiler::stop(prof_grab);

                    if (grab_err == sl::ERROR_CODE::SUCCESS) {
                        const sl::Timestamp image_ts = zed->getTimestamp(sl::TIME_REFERENCE::IMAGE);

                        // G2I reference. Live: the image (capture) timestamp. SVO playback: the image
                        // timestamp is the original capture time, so use the host time right before
                        // read() instead, which makes G2I the latency of processing the SVO frame.
                        const long long capture_ref_ns = (long long)(svo_inputs.empty() ? image_ts : before_read).getNanoseconds();

                        // Recording: store the raw after-read "now" keyed by the IMAGE timestamp.
                        if (record_live_ts) {
                            live_ts_map[image_ts] = after_read;
                            last_img_ts = image_ts;
                        }

                        if (period_open)
                            bench::Profiler::stop(prof_period);
                        bench::Profiler::start(prof_period);
                        period_open = true;

                        // Use Resolution(-1,-1) for optimal working resolution
                        const long long before_img_ns = (long long)zed->getTimestamp(sl::TIME_REFERENCE::CURRENT).getNanoseconds();
                        bench::Profiler::start(prof_img);
                        zed->retrieveImage(image, sl::VIEW::LEFT, sl::MEM::CPU, sl::Resolution(-1, -1));
                        bench::Profiler::stop(prof_img);
                        const long long after_img_ns = (long long)zed->getTimestamp(sl::TIME_REFERENCE::CURRENT).getNanoseconds();

                        // Glass-to-image latency: reference -> end of read(), plus retrieveImage(). grab()
                        // runs in between and is excluded.
                        bench::Profiler::addSample(
                            prof_g2i,
                            (((long long)after_read.getNanoseconds() - capture_ref_ns) + (after_img_ns - before_img_ns)) / 1.0e6
                        );
#ifdef HAVE_G2G
                        // Glass-to-glass: offload the just-grabbed frame to the async decoder worker
                        // so the expensive decode never inflates this grab loop. The "now" timestamp
                        // is the after-read one sampled above. We submit a deep copy of the frame; the
                        // worker then decodes it, computes the latency, and updates the display.
                        // The after-read "now" (the SDK CURRENT clock) and the decoded on-screen time share
                        // the host realtime clock, so their difference is the latency directly.
                        if (g2g_active) {
                            long long g2g_now_ns = (long long)after_read.getNanoseconds();
                            bool g2g_now_ok = true;
                            // Playback: replace the playback wall-clock "now" with the live "now"
                            // recorded at capture time, keyed by the frame's baked IMAGE ts.
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

                        // Stereo-only post-grab measurements (depth / tracking / OD / BT); no-op for mono.
                        do_measurements();

                        frames_counts[i].fetch_add(1, std::memory_order_relaxed);
                        if (monitor_imu) {
                            std::vector<sl::SensorsData> batch;
                            auto err = zed->getSensorsDataBatch(batch);
                            if (err == sl::ERROR_CODE::SUCCESS) {
                                imu_count += batch.size();
                                imu_per_frame.push_back(static_cast<double>(batch.size()));
                            } else {
                                imu_per_frame.push_back(0.0);
                            }
                        }
                    } else {
                        // End of SVO: this camera is done. Break so the run can end once every
                        // camera has reached EOF (the whole file is processed → repeatable replay).
                        if (!svo_inputs.empty() && grab_err == sl::ERROR_CODE::END_OF_SVOFILE_REACHED)
                            break;
                        std::this_thread::sleep_for(std::chrono::milliseconds(1));
                    }
                }
                bench::Profiler::stop(prof_period);
            };

            if (cameras[i].is_mono) {
                // --- Mono camera ---
                sl::CameraOne* zed = cameras[i].mono;
                std::cout << "[Cam " << i << "] Started (mono)." << std::endl;
                run_capture_loop(
                    zed,
                    [&] {
                        return zed->grab();
                    },
                    [] {
                    }
                );
            } else {
                // --- Stereo camera ---
                sl::Camera* zed = cameras[i].stereo;
                sl::RuntimeParameters runtime_params;
                sl::Mat depth_mat;
                sl::Objects objects;
                sl::Bodies bodies;
                sl::Pose pose;
                sl::ObjectDetectionRuntimeParameters od_runtime_params;
                sl::BodyTrackingRuntimeParameters bt_runtime_params;
                const std::string prof_measure = "camera_sn_" + sn + "_retrieveMeasure(...)";
                const std::string prof_pos = "camera_sn_" + sn + "_getPosition(...)";
                const std::string prof_obj = "camera_sn_" + sn + "_retrieveObjects(...)";
                const std::string prof_bodies = "camera_sn_" + sn + "_retrieveBodies(...)";

                std::cout << "[Cam " << i << "] Started (stereo)." << std::endl;
                run_capture_loop(
                    zed,
                    [&] {
                        return zed->grab(runtime_params);
                    },
                    [&] {
                        if (depth_mode != sl::DEPTH_MODE::NONE) {
                            bench::Profiler::start(prof_measure);
                            zed->retrieveMeasure(depth_mat, sl::MEASURE::XYZ, sl::MEM::CPU, sl::Resolution(-1, -1));
                            bench::Profiler::stop(prof_measure);
                        }
                        if (enable_positional_tracking) {
                            bench::Profiler::start(prof_pos);
                            zed->getPosition(pose, sl::REFERENCE_FRAME::WORLD);
                            bench::Profiler::stop(prof_pos);
                        }
                        if (!od_model_str.empty()) {
                            bench::Profiler::start(prof_obj);
                            zed->retrieveObjects(objects, od_runtime_params);
                            bench::Profiler::stop(prof_obj);
                        }
                        if (!bt_model_str.empty()) {
                            bench::Profiler::start(prof_bodies);
                            zed->retrieveBodies(bodies, bt_runtime_params);
                            bench::Profiler::stop(prof_bodies);
                        }
                    }
                );
            }
            // Stamp this camera's end of capture before any teardown so the decoder shutdown
            // below isn't counted against its FPS/IMU rate.
            cam_end_times[i] = std::chrono::high_resolution_clock::now();
#ifdef HAVE_G2G
            // Stop + join this camera's async decoder before the thread exits, so every in-flight
            // latency sample is collected, then hand its latencies to the profiler.
            if (g2g_async) {
                g2g_async->stop();
                for (double lat_ms : g2g_async->takeLatencies())
                    bench::Profiler::addSample(prof_g2g, lat_ms);
                g2g_async.reset();
            }
#endif
            // This camera's grab loop has ended (EOF in SVO mode, or is_running cleared); mark it
            // finished so the SVO status loop can stop once all cameras have reached EOF.
            ++cameras_finished;
        });
    }

    // Status display loop: single-line \r to avoid ANSI cursor-up issues.
    // FPS is computed from the per-camera frame counters (delta over the sample interval) rather
    // than calling getCurrentFPS() on the camera from this thread: the worker threads are
    // concurrently grabbing on those same camera objects (getCurrentFPS() is non-const and the SDK
    // is not documented thread-safe for concurrent access).
    std::vector<int> last_counts(total_cameras, 0);
    auto last_sample = start_time;
    while (is_running) {
        std::this_thread::sleep_for(std::chrono::seconds(1));
        auto current_time = std::chrono::high_resolution_clock::now();
        double dt = std::chrono::duration<double>(current_time - last_sample).count();
        last_sample = current_time;
        std::chrono::duration<double> elapsed = current_time - start_time;
        int elapsed_s = static_cast<int>(elapsed.count());

        std::string line;
        char buf[80];
        snprintf(buf, sizeof(buf), "[%ds/%ds] ", elapsed_s, static_cast<int>(duration));
        line += buf;
        for (int i = 0; i < total_cameras; i++) {
            if (i > 0)
                line += " | ";
            int cur = frames_counts[i].load(std::memory_order_relaxed);
            double fps = (dt > 0.0) ? (cur - last_counts[i]) / dt : 0.0;
            last_counts[i] = cur;
            snprintf(buf, sizeof(buf), "C%d:%.1ffps/%d", i, fps, cur);
            line += buf;
        }
        printf("\r%s\033[K", line.c_str());
        fflush(stdout);
        // SVO playback: ignore --duration and run until every camera has reached end-of-file, so
        // each file is processed in full and a replay covers the exact same frames (repeatability).
        if (!svo_inputs.empty()) {
            if (cameras_finished.load() >= total_cameras) {
                printf("\n");
                std::cout << "Benchmark finished (all SVOs reached end of file)." << std::endl;
                is_running = false;
                break;
            }
        } else if (duration > 0 && elapsed.count() > duration) {
            printf("\n");
            std::cout << "Benchmark finished (duration reached)." << std::endl;
            is_running = false;
            break;
        }
    }

    std::cout << "\nStopping..." << std::endl;
    for (auto& t : threads)
        if (t.joinable())
            t.join();

#ifdef HAVE_G2G
    if (g2g_display)
        g2g_display->stop();
#endif

    // Per-camera end times were stamped by each worker as its grab loop ended (see cam_end_times);
    // FPS/IMU rates below divide by each camera's own elapsed window, not one shared duration.

    // Dump profiler stats while the cameras are still open (closing the last one
    // resets the profiler), then reshape into the historical report schema.
    const std::string raw_report = output_report + ".profiler.json";
    bench::Profiler::dumpStats(raw_report);

    for (int i = 0; i < total_cameras; i++) {
        // Persist the live-timestamp correspondence map into the SVO before recording is disabled
        // (ingestDataIntoSVO is only valid while recording). Gate on the per-camera flag: a camera
        // whose enableRecording() failed is not recording, so ingesting would just error out.
        if (record_live_ts && cameras[i].recording_enabled) {
            if (cameras[i].is_mono && cameras[i].mono)
                ingest_live_ts_map(*cameras[i].mono, live_ts_maps[i], last_image_ts[i]);
            else if (!cameras[i].is_mono && cameras[i].stereo)
                ingest_live_ts_map(*cameras[i].stereo, live_ts_maps[i], last_image_ts[i]);
        }
        if (cameras[i].is_mono && cameras[i].mono) {
            if (cameras[i].recording_enabled)
                cameras[i].mono->disableRecording();
            if (!compression_stream_str.empty())
                cameras[i].mono->disableStreaming();
        }
        if (!cameras[i].is_mono && cameras[i].stereo) {
            if (!od_model_str.empty())
                cameras[i].stereo->disableObjectDetection();
            if (!bt_model_str.empty())
                cameras[i].stereo->disableBodyTracking();
            if (enable_positional_tracking)
                cameras[i].stereo->disablePositionalTracking();
            if (cameras[i].recording_enabled)
                cameras[i].stereo->disableRecording();
            if (!compression_stream_str.empty())
                cameras[i].stereo->disableStreaming();
        }
        cameras[i].close();
    }

    std::cout << "Benchmark Finished." << std::endl;

    // Reshape the profiler dump into the historical report schema.
    {
        std::vector<std::string> serials(total_cameras);
        std::vector<double> per_cam_seconds(total_cameras);
        for (int i = 0; i < total_cameras; i++) {
            serials[i] = std::to_string(cameras[i].serial_number);
            per_cam_seconds[i] = std::chrono::duration<double>(cam_end_times[i] - start_time).count();
        }
        write_profiler_report(raw_report, output_report, serials, frames_counts, per_cam_seconds);
        std::error_code ec;
        std::filesystem::remove(raw_report, ec);
    }

    // Append IMU data to the benchmark report
    if (monitor_imu) {
        sl::json report;
        {
            std::ifstream ifs(output_report);
            if (ifs.is_open()) {
                try {
                    ifs >> report;
                } catch (const std::exception& e) {
                    std::cerr << "[IMU] Could not parse " << output_report << " to append IMU data: " << e.what() << std::endl;
                }
            }
        }
        if (report.is_object()) {
            for (int i = 0; i < total_cameras; i++) {
                if (imu_sample_counts[i] == 0)
                    continue;
                std::string sn = std::to_string(cameras[i].serial_number);
                double cam_s = std::chrono::duration<double>(cam_end_times[i] - start_time).count();
                double rate = (cam_s > 0) ? imu_sample_counts[i] / cam_s : 0.0;
                // Emit imu_per_frame as the full {mean, median, stddev} distribution (3 decimals),
                // matching the single-camera report schema (benchmark_single_cam.cpp append_imu_json)
                // so the same key has the same shape across both tools.
                double ipf_mean = 0.0, ipf_median = 0.0, ipf_stddev = 0.0;
                auto& pf = imu_per_frame_all[i];
                if (!pf.empty()) {
                    std::sort(pf.begin(), pf.end());
                    size_t n = pf.size();
                    ipf_mean = std::accumulate(pf.begin(), pf.end(), 0.0) / n;
                    ipf_median = (n % 2 == 0) ? (pf[n / 2 - 1] + pf[n / 2]) / 2.0 : pf[n / 2];
                    double sq = 0;
                    for (double v : pf)
                        sq += (v - ipf_mean) * (v - ipf_mean);
                    ipf_stddev = std::sqrt(sq / n);
                }
                report["IMU_SN" + sn] = {
                    {"imu_sample_count", imu_sample_counts[i]                                                                        },
                    {"imu_rate_hz",      round3(rate)                                                                                },
                    {"imu_per_frame",    {{"mean", round3(ipf_mean)}, {"median", round3(ipf_median)}, {"stddev", round3(ipf_stddev)}}},
                };
            }

            std::ofstream ofs(output_report, std::ios::trunc);
            if (ofs.is_open())
                ofs << report.dump(4) << "\n";
        }
    }

    return 0;
}
