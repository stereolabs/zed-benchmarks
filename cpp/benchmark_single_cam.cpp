#include <sl/Camera.hpp>
#include <sl/CameraOne.hpp>
#include <iostream>
#include <string>
#include <vector>
#include <atomic>
#include <chrono>
#include <thread>
#include <csignal>
#include <getopt.h>
#include <cstdlib>
#include <fstream>
#include <filesystem>
#include <unistd.h>
#include <algorithm>
#include <numeric>
#include <cmath>

// Global flag to stop the loop
std::atomic<bool> is_running {true};

void signal_handler(int sig) {
    std::cout << "\nBenchmark stopped by user." << std::endl;
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
    std::cout << "  --timer                          Use manual timer instead of SDK benchmark mode\n";
    std::cout << "  --no_monitor_imu                 Disable IMU monitoring (enabled by default)\n";
    std::cout << "  --help                           Show this help message\n";
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

// ── Timer mode helpers ──────────────────────────────────────────────────────
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

void write_timer_json(
    const std::string& filename,
    const std::string& grab_key,
    int frames_count,
    double total_seconds,
    std::vector<double>& grab_ms,
    std::vector<double>& period_ms,
    std::vector<double>& ri_ms,
    std::vector<double>& rb_ms,
    std::vector<double>& ro_ms,
    std::vector<double>& gp_ms,
    const std::string& compression_rec_str = "",
    bool encrypted = false
) {
    std::ofstream f(filename);
    f << std::fixed;
    f.precision(3);
    f << "{\n";

    // Config section so results can be identified
    if (!compression_rec_str.empty()) {
        f << "  \"config\": {\n";
        f << "    \"recording_compression\": \"" << compression_rec_str << "\",\n";
        f << "    \"recording_encrypted\": " << (encrypted ? "true" : "false") << "\n";
        f << "  },\n";
    }

    double freq = (total_seconds > 0) ? frames_count / total_seconds : 0;
    TimingStats gs = compute_stats(grab_ms);
    TimingStats ps = compute_stats(period_ms);
    f << "  \"" << grab_key << "\": {\n";
    f << "    \"grab_call_frequency_hz\": " << freq << ",\n";
    f << "    \"grab_call_mean_period_ms\": " << ps.mean << ",\n";
    f << "    \"grab_call_median_period_ms\": " << ps.median << ",\n";
    f << "    \"cpu_stats\": { \"mean\": " << gs.mean << ", \"median\": " << gs.median << ", \"stddev\": " << gs.stddev << " }\n";
    f << "  }";

    auto write_event = [&](const std::string& name, std::vector<double>& samples) {
        if (samples.empty())
            return;
        TimingStats s = compute_stats(samples);
        f << ",\n  \"" << name << "\": {\n";
        f << "    \"cpu_stats\": { \"mean\": " << s.mean << ", \"median\": " << s.median << ", \"stddev\": " << s.stddev << " }\n";
        f << "  }";
    };

    write_event("RetrieveImage", ri_ms);
    write_event("RetrieveBodies", rb_ms);
    write_event("RetrieveObjects", ro_ms);
    write_event("GetPosition", gp_ms);

    f << "\n}\n";
    f.close();
    std::cout << "[Timer] Results written to " << filename << std::endl;
}

void append_imu_json(
    const std::string& filename,
    const std::string& imu_key,
    size_t imu_sample_count,
    double total_seconds,
    std::vector<double>& imu_per_frame
) {
    if (imu_sample_count == 0)
        return;

    // Read existing JSON, strip trailing "\n}\n", append IMU section, re-close
    std::string content;
    {
        std::ifstream in(filename);
        if (!in.is_open())
            return;
        content.assign(std::istreambuf_iterator<char>(in), std::istreambuf_iterator<char> {});
    }

    // Find the last '}'
    auto pos = content.rfind('}');
    if (pos == std::string::npos)
        return;
    content.resize(pos); // Remove closing '}'

    double rate = (total_seconds > 0) ? static_cast<double>(imu_sample_count) / total_seconds : 0;
    TimingStats ipf = compute_stats(imu_per_frame);

    std::ofstream f(filename);
    f << std::fixed;
    f.precision(3);
    f << content;
    f << ",\n  \"" << imu_key << "\": {\n";
    f << "    \"imu_sample_count\": " << imu_sample_count << ",\n";
    f << "    \"imu_rate_hz\": " << rate << ",\n";
    f << "    \"imu_per_frame\": { \"mean\": " << ipf.mean << ", \"median\": " << ipf.median << ", \"stddev\": " << ipf.stddev << " }\n";
    f << "  }\n}\n";
    f.close();
}

// Run benchmark for a mono camera (CameraOne)
int runMonoBenchmark(
    int camera_fps,
    const std::string& resolution_str,
    const std::string& svo_input,
    const std::string& compression_rec_str,
    const std::string& encryption_key_str,
    const std::string& output_svo,
    const std::string& compression_stream_str,
    unsigned int serial_number,
    int streaming_port,
    float warmup,
    float duration,
    const std::string& sync_dir,
    const bool use_timer,
    const std::string& output_report,
    const bool monitor_imu
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

    std::cout << "Opening CameraOne..." << std::endl;
    sl::ERROR_CODE status = zed.open(init_params);
    if (status != sl::ERROR_CODE::SUCCESS) {
        std::cerr << "Failed to open CameraOne: " << status << std::endl;
        return 1;
    }

    auto cam_info = zed.getCameraInformation();
    std::cout << "Opened CameraOne (" << sl::toString(cam_info.camera_model) << ")" << std::endl;

    // Enable Recording
    if (!compression_rec_str.empty()) {
        sl::RecordingParameters rec_params;
        sl::fromString(compression_rec_str.c_str(), rec_params.compression_mode);
        rec_params.video_filename = sl::String(output_svo.c_str());
        if (!encryption_key_str.empty())
            rec_params.encryption_key = sl::String(encryption_key_str.c_str());
        sl::ERROR_CODE err = zed.enableRecording(rec_params);
        if (err != sl::ERROR_CODE::SUCCESS) {
            std::cerr << "Failed to enable recording: " << err << std::endl;
            return 1;
        }
        std::cout << "Recording enabled (" << compression_rec_str << (encryption_key_str.empty() ? "" : ", encrypted") << ") to "
                  << output_svo << std::endl;
    }

    // Enable Streaming
    if (!compression_stream_str.empty()) {
        sl::StreamingParameters stream_params;
        sl::fromString(compression_stream_str.c_str(), stream_params.codec);
        stream_params.port = streaming_port;
        sl::ERROR_CODE err = zed.enableStreaming(stream_params);
        if (err != sl::ERROR_CODE::SUCCESS) {
            std::cerr << "Failed to enable streaming: " << err << std::endl;
            return 1;
        }
        std::cout << "Streaming enabled (" << compression_stream_str << ") on port " << streaming_port << std::endl;
    }

    // Barrier sync: wait for all processes before starting
    sync_barrier_signal_and_wait(sync_dir);

    signal(SIGINT, signal_handler);

    sl::Mat image;

    if (warmup > 0.f) {
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
    }

    if (!use_timer)
        sl::resetBenchmarkTimer();

    int frames_count = 0;
    auto start_time = clk::now();
    clk::time_point last_grab_tp = start_time;
    std::vector<double> t_grab, t_period, t_ri, t_rb_unused, t_ro_unused, t_gp_unused;
    if (use_timer) {
        int res = std::max(1, (int)(duration * camera_fps * 1.1));
        t_grab.reserve(res);
        t_period.reserve(res);
        t_ri.reserve(res);
    }

    // IMU monitoring state
    size_t imu_sample_count = 0;
    std::vector<double> imu_per_frame;
    if (monitor_imu)
        imu_per_frame.reserve(std::max(1, (int)(duration * camera_fps * 1.1)));

    std::cout << "\nStarting benchmark loop for " << duration << " seconds. Press Ctrl+C to stop." << std::endl;

    while (is_running) {
        auto current_time = clk::now();
        std::chrono::duration<double> elapsed = current_time - start_time;
        if (duration > 0 && elapsed.count() > duration) {
            std::cout << "\nBenchmark finished (duration reached)." << std::endl;
            break;
        }

        auto tg0 = clk::now();
        if (zed.grab() <= sl::ERROR_CODE::SUCCESS) {
            auto tg1 = clk::now();
            auto tri0 = clk::now();
            // Use Resolution(-1,-1) to retrieve at the SDK's working resolution
            // instead of the camera resolution (default). This is more efficient.
            zed.retrieveImage(image, sl::VIEW::LEFT, sl::MEM::CPU, sl::Resolution(-1, -1));
            auto tri1 = clk::now();
            if (use_timer) {
                t_grab.push_back(std::chrono::duration<double, std::milli>(tg1 - tg0).count());
                t_period.push_back(std::chrono::duration<double, std::milli>(tg1 - last_grab_tp).count());
                last_grab_tp = tg1;
                t_ri.push_back(std::chrono::duration<double, std::milli>(tri1 - tri0).count());
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
            std::this_thread::sleep_for(std::chrono::milliseconds(1));
        }
    }

    printf("\nFinal FPS: %.2f\n", zed.getCurrentFPS());

    if (use_timer) {
        double total_s = std::chrono::duration<double>(clk::now() - start_time).count();
        std::string sn = std::to_string(cam_info.serial_number);
        write_timer_json(
            output_report,
            "Grab_SN" + sn,
            frames_count,
            total_s,
            t_grab,
            t_period,
            t_ri,
            t_rb_unused,
            t_ro_unused,
            t_gp_unused,
            compression_rec_str,
            !encryption_key_str.empty()
        );
    }

    auto end_time = clk::now();

    if (!compression_rec_str.empty())
        zed.disableRecording();
    if (!compression_stream_str.empty())
        zed.disableStreaming();
    zed.close();

    // Append IMU data after close (SDK writes its JSON during close)
    if (monitor_imu && imu_sample_count > 0) {
        double total_s = std::chrono::duration<double>(end_time - start_time).count();
        std::string sn = std::to_string(cam_info.serial_number);
        append_imu_json(output_report, "IMU_SN" + sn, imu_sample_count, total_s, imu_per_frame);
    }

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
    const std::string& encryption_key_str,
    const std::string& output_svo,
    const std::string& compression_stream_str,
    bool enable_positional_tracking,
    unsigned int serial_number,
    int streaming_port,
    float warmup,
    float duration,
    const std::string& sync_dir,
    const bool use_timer,
    const std::string& output_report,
    const bool monitor_imu
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

    std::cout << "Opening Camera..." << std::endl;
    sl::ERROR_CODE status = zed.open(init_params);
    if (status != sl::ERROR_CODE::SUCCESS) {
        std::cerr << "Failed to open camera: " << status << std::endl;
        return 1;
    }

    std::cout << "Opened Camera (" << sl::toString(zed.getCameraInformation().camera_model) << ")" << std::endl;

    // Enable Recording
    if (!compression_rec_str.empty()) {
        sl::RecordingParameters rec_params;
        sl::fromString(compression_rec_str.c_str(), rec_params.compression_mode);
        rec_params.video_filename = sl::String(output_svo.c_str());
        if (!encryption_key_str.empty())
            rec_params.encryption_key = sl::String(encryption_key_str.c_str());
        sl::ERROR_CODE err = zed.enableRecording(rec_params);
        if (err != sl::ERROR_CODE::SUCCESS) {
            std::cerr << "Failed to enable recording: " << err << std::endl;
            return 1;
        }
        std::cout << "Recording enabled (" << compression_rec_str << (encryption_key_str.empty() ? "" : ", encrypted") << ") to "
                  << output_svo << std::endl;
    }

    // Enable Streaming
    if (!compression_stream_str.empty()) {
        sl::StreamingParameters stream_params;
        sl::fromString(compression_stream_str.c_str(), stream_params.codec);
        stream_params.port = streaming_port;
        sl::ERROR_CODE err = zed.enableStreaming(stream_params);
        if (err != sl::ERROR_CODE::SUCCESS) {
            std::cerr << "Failed to enable streaming: " << err << std::endl;
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
                // Parse "WxH" format
                size_t xpos = od_custom_onnx_input_shape.find('x');
                if (xpos != std::string::npos) {
                    int w = std::stoi(od_custom_onnx_input_shape.substr(0, xpos));
                    int h = std::stoi(od_custom_onnx_input_shape.substr(xpos + 1));
                    od_params.custom_onnx_dynamic_input_shape = sl::Resolution(w, h);
                }
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
            return 1;
        }
        std::cout << "Body Tracking enabled (" << bt_model_str << ")" << std::endl;
    }

    // Barrier sync: wait for all processes before starting
    sync_barrier_signal_and_wait(sync_dir);

    signal(SIGINT, signal_handler);

    sl::RuntimeParameters runtime_params;
    sl::Mat image, depth;
    sl::Objects objects;
    sl::Bodies bodies;
    sl::Pose pose;
    sl::ObjectDetectionRuntimeParameters od_runtime_params;
    sl::BodyTrackingRuntimeParameters bt_runtime_params;

    if (warmup > 0.f) {
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
    }

    if (!use_timer)
        sl::resetBenchmarkTimer();

    int frames_count = 0;
    auto start_time = clk::now();
    clk::time_point last_grab_tp = start_time;
    std::vector<double> t_grab, t_period, t_ri, t_rb, t_ro, t_gp;
    if (use_timer) {
        int res = std::max(1, (int)(duration * camera_fps * 1.1));
        t_grab.reserve(res);
        t_period.reserve(res);
        t_ri.reserve(res);
        t_rb.reserve(res);
        t_ro.reserve(res);
        t_gp.reserve(res);
    }
    // IMU monitoring state
    size_t imu_sample_count = 0;
    std::vector<double> imu_per_frame;
    if (monitor_imu)
        imu_per_frame.reserve(std::max(1, (int)(duration * camera_fps * 1.1)));

    std::cout << "\nStarting benchmark loop for " << duration << " seconds. Press Ctrl+C to stop." << std::endl;

    while (is_running) {
        auto current_time = clk::now();
        std::chrono::duration<double> elapsed = current_time - start_time;
        if (duration > 0 && elapsed.count() > duration) {
            std::cout << "\nBenchmark finished (duration reached)." << std::endl;
            break;
        }

        auto tg0 = clk::now();
        if (zed.grab(runtime_params) <= sl::ERROR_CODE::SUCCESS) {
            auto tg1 = clk::now();
            auto tri0 = clk::now();
            // Use Resolution(-1,-1) to retrieve at the SDK's working resolution
            // instead of the camera resolution (default). This is more efficient.
            zed.retrieveImage(image, sl::VIEW::LEFT, sl::MEM::CPU, sl::Resolution(-1, -1));
            auto tri1 = clk::now();

            if (init_params.depth_mode != sl::DEPTH_MODE::NONE)
                zed.retrieveMeasure(depth, sl::MEASURE::XYZ, sl::MEM::CPU, sl::Resolution(-1, -1));

            if (pt_enabled) {
                auto tp0 = clk::now();
                zed.getPosition(pose, sl::REFERENCE_FRAME::WORLD);
                auto tp1 = clk::now();
                if (use_timer)
                    t_gp.push_back(std::chrono::duration<double, std::milli>(tp1 - tp0).count());
            }

            if (!od_model_str.empty()) {
                auto to0 = clk::now();
                zed.retrieveObjects(objects, od_runtime_params);
                auto to1 = clk::now();
                if (use_timer)
                    t_ro.push_back(std::chrono::duration<double, std::milli>(to1 - to0).count());
            }

            if (!bt_model_str.empty()) {
                auto tb0 = clk::now();
                zed.retrieveBodies(bodies, bt_runtime_params);
                auto tb1 = clk::now();
                if (use_timer)
                    t_rb.push_back(std::chrono::duration<double, std::milli>(tb1 - tb0).count());
            }

            if (use_timer) {
                t_grab.push_back(std::chrono::duration<double, std::milli>(tg1 - tg0).count());
                t_period.push_back(std::chrono::duration<double, std::milli>(tg1 - last_grab_tp).count());
                last_grab_tp = tg1;
                t_ri.push_back(std::chrono::duration<double, std::milli>(tri1 - tri0).count());
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
            std::this_thread::sleep_for(std::chrono::milliseconds(1));
        }
    }

    printf("\nFinal FPS: %.2f\n", zed.getCurrentFPS());

    if (use_timer) {
        double total_s = std::chrono::duration<double>(clk::now() - start_time).count();
        std::string sn = std::to_string(zed.getCameraInformation().serial_number);
        write_timer_json(
            output_report,
            "Grab_SN" + sn,
            frames_count,
            total_s,
            t_grab,
            t_period,
            t_ri,
            t_rb,
            t_ro,
            t_gp,
            compression_rec_str,
            !encryption_key_str.empty()
        );
    }

    auto end_time = clk::now();
    unsigned int cam_serial = zed.getCameraInformation().serial_number;

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

    // Append IMU data after close (SDK writes its JSON during close)
    if (monitor_imu && imu_sample_count > 0) {
        double total_s = std::chrono::duration<double>(end_time - start_time).count();
        std::string sn = std::to_string(cam_serial);
        append_imu_json(output_report, "IMU_SN" + sn, imu_sample_count, total_s, imu_per_frame);
    }

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
    std::string encryption_key_str = "";
    std::string output_svo = "benchmark_recording.svo";
    std::string output_report = "benchmark_report.json";
    std::string compression_stream_str = "";
    std::string sync_dir = "";
    bool use_timer = false;
    bool enable_positional_tracking = false;
    bool use_camera_one = false;
    bool monitor_imu = true;
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
        {"timer",                      no_argument,       0, 'T'},
        {"no_monitor_imu",             no_argument,       0, 'N'},
        {"help",                       no_argument,       0, 'h'},
        {0,                            0,                 0, 0  }
    };

    int opt;
    int option_index = 0;
    while ((opt = getopt_long(argc, argv, "i:f:r:d:w:o:O:I:b:c:E:s:t:S:P:pu:m:1y:TNh", long_options, &option_index)) != -1) {
        switch (opt) {
            case 'i':
                svo_input = optarg;
                break;
            case 'f':
                camera_fps = std::stoi(optarg);
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
                serial_number = std::stoul(optarg);
                break;
            case 'P':
                streaming_port = std::stoi(optarg);
                break;
            case 'w':
                warmup = std::stof(optarg);
                break;
            case 'u':
                duration = std::stof(optarg);
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
            case 'T':
                use_timer = true;
                break;
            case 'N':
                monitor_imu = false;
                break;
            case 'h':
            default:
                printHelp();
                return 0;
        }
    }

    if (!use_timer)
        setenv("ZED_SDK_BENCHMARK_MODE", output_report.c_str(), 1);

    if (use_camera_one) {
        std::cout << "[CameraOne] Mono camera detected. Stereo-only features (depth, OD, BT, tracking) are not available." << std::endl;
        return runMonoBenchmark(
            camera_fps,
            resolution_str,
            svo_input,
            compression_rec_str,
            encryption_key_str,
            output_svo,
            compression_stream_str,
            serial_number,
            streaming_port,
            warmup,
            duration,
            sync_dir,
            use_timer,
            output_report,
            monitor_imu
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
            encryption_key_str,
            output_svo,
            compression_stream_str,
            enable_positional_tracking,
            serial_number,
            streaming_port,
            warmup,
            duration,
            sync_dir,
            use_timer,
            output_report,
            monitor_imu
        );
    }
}
