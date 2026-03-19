#include <sl/Camera.hpp>
#include <sl/CameraOne.hpp>
#include <iostream>
#include <string>
#include <vector>
#include <atomic>
#include <chrono>
#include <thread>
#include <mutex>
#include <csignal>
#include <getopt.h>
#include <iomanip>
#include <cstdio>
#include <fstream>

std::atomic<bool> is_running {true};

void signal_handler(int sig) {
    std::cout << "\nBenchmark stopped by user." << std::endl;
    is_running = false;
}

void printHelp() {
    std::cout << "Usage: benchmark_multi_cam [options]\n";
    std::cout << "Options:\n";
    std::cout << "  --num_stereo <n>                 Number of stereo (ZED X) cameras (default: all detected)\n";
    std::cout << "  --num_mono <n>                   Number of mono (ZED X One) cameras (default: all detected)\n";
    std::cout << "  --svo_input <path>               Path to SVO file (default: live)\n";
    std::cout << "  --camera_fps <fps>               Camera FPS (default: 30)\n";
    std::cout << "  --resolution <res>               Resolution (default: HD1080)\n";
    std::cout << "  --depth_mode <mode>              Depth Mode - stereo only (default: NONE)\n";
    std::cout << "  --od_model <model>               Object Detection Model - stereo only (default: OFF)\n";
    std::cout << "  --od_custom_onnx_file <path>     Path to custom ONNX file (requires od_model=CUSTOM_YOLOLIKE_BOX_OBJECTS)\n";
    std::cout << "  --od_custom_onnx_input_shape <WxH> Input resolution for custom ONNX (e.g. 640x640, default: 512x512)\n";
    std::cout << "  --bt_model <model>               Body Tracking Model - stereo only (default: OFF)\n";
    std::cout << "  --compression_mode_recording <m> Compression Mode for Recording (default: OFF)\n";
    std::cout << "  --encryption_key <key>           Encryption key/passphrase for SVO recording (default: empty)\n";
    std::cout << "  --compression_mode_streaming <m> Compression Mode for Streaming (default: OFF)\n";
    std::cout << "  --enable_positional_tracking     Enable Positional Tracking - stereo only (default: OFF)\n";
    std::cout << "  --duration <seconds>             Duration in seconds (default: 60)\n";
    std::cout << "  --warmup <seconds>               Warmup duration in seconds (default: 5.0)\n";
    std::cout << "  --benchmark <filename>           Benchmark report filename (default: benchmark_report.json)\n";
    std::cout << "  --no_monitor_imu                 Disable IMU monitoring (default: ON)\n";
    std::cout << "  --help                           Show this help message\n";
}

// Holds either a stereo Camera or a mono CameraOne
struct CameraHandle {
    sl::Camera* stereo = nullptr;
    sl::CameraOne* mono = nullptr;
    bool is_mono = false;
    unsigned int serial_number = 0;

    float getCurrentFPS() const {
        return is_mono ? mono->getCurrentFPS() : stereo->getCurrentFPS();
    }
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

int main(int argc, char** argv) {
    int num_stereo = -1; // -1 = use all detected
    int num_mono = -1;   // -1 = use all detected
    std::string svo_input = "";
    int camera_fps = 30;
    std::string resolution_str = "HD1080";
    std::string depth_mode_str = "NONE";
    std::string od_model_str = "";
    std::string od_custom_onnx_file = "";
    std::string od_custom_onnx_input_shape = "";
    std::string bt_model_str = "";
    std::string compression_rec_str = "";
    std::string encryption_key_str = "";
    std::string compression_stream_str = "";
    std::string output_report = "benchmark_report.json";
    bool enable_positional_tracking = false;
    bool monitor_imu = true;
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
        {"encryption_key",             required_argument, 0, 'E'},
        {"compression_mode_streaming", required_argument, 0, 't'},
        {"duration",                   required_argument, 0, 'u'},
        {"warmup",                     required_argument, 0, 'w'},
        {"benchmark",                  required_argument, 0, 'k'},
        {"no_monitor_imu",             no_argument,       0, 'N'},
        {"help",                       no_argument,       0, 'h'},
        {0,                            0,                 0, 0  }
    };

    int opt;
    int option_index = 0;
    while ((opt = getopt_long(argc, argv, "i:f:r:d:o:O:I:b:c:E:t:pu:w:s:m:k:Nh", long_options, &option_index)) != -1) {
        switch (opt) {
            case 'i':
                svo_input = optarg;
                break;
            case 's':
                num_stereo = std::stoi(optarg);
                break;
            case 'm':
                num_mono = std::stoi(optarg);
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
            case 't':
                compression_stream_str = optarg;
                break;
            case 'p':
                enable_positional_tracking = true;
                break;
            case 'w':
                warmup = std::stof(optarg);
                break;
            case 'u':
                duration = std::stof(optarg);
                break;
            case 'k':
                output_report = optarg;
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

    setenv("ZED_SDK_BENCHMARK_MODE", output_report.c_str(), 1);

    // Discover all connected cameras (stereo + mono)
    struct DeviceInfo {
        unsigned int serial_number;
        bool is_mono;
    };
    std::vector<DeviceInfo> all_devices;
    int total_cameras = 0;

    if (svo_input.empty()) {
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
            all_devices.push_back({stereo_devs[s].serial_number, false});
        for (int m = 0; m < num_mono; m++)
            all_devices.push_back({mono_devs[m].serial_number, true});

        std::cout << "Detected " << avail_stereo << " stereo + " << avail_mono << " mono camera(s). Using " << num_stereo << " stereo + "
                  << num_mono << " mono (" << total_cameras << " total)." << std::endl;
    } else {
        // SVO input: treat as stereo
        all_devices.push_back({0, false});
        total_cameras = 1;
        num_stereo = 1;
        num_mono = 0;
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
            if (!svo_input.empty()) {
                init_one.input.setFromSVOFile(svo_input.c_str());
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
            std::cout << "[Cam " << i << "] Opened CameraOne (" << sl::toString(info.camera_model) << ")" << std::endl;

            // Recording (mono)
            if (!compression_rec_str.empty()) {
                sl::RecordingParameters rec_params;
                rec_params.compression_mode = rec_compression;
                std::string filename = "benchmark_output_" + std::to_string(dev.serial_number) + ".svo2";
                rec_params.video_filename = sl::String(filename.c_str());
                if (!encryption_key_str.empty())
                    rec_params.encryption_key = sl::String(encryption_key_str.c_str());
                if (cameras[i].mono->enableRecording(rec_params) != sl::ERROR_CODE::SUCCESS)
                    std::cerr << "[Cam " << i << "] Failed to enable recording" << std::endl;
                else
                    std::cout << "[Cam " << i << "] Recording enabled" << (encryption_key_str.empty() ? "" : " (encrypted)") << " to "
                              << filename << std::endl;
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
            if (!svo_input.empty()) {
                init_params.input.setFromSVOFile(svo_input.c_str());
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
            std::cout << "[Cam " << i << "] Opened Camera (" << sl::toString(cameras[i].stereo->getCameraInformation().camera_model) << ")"
                      << std::endl;

            // Recording (stereo)
            if (!compression_rec_str.empty()) {
                sl::RecordingParameters rec_params;
                rec_params.compression_mode = rec_compression;
                std::string filename = "benchmark_output_" + std::to_string(dev.serial_number) + ".svo2";
                rec_params.video_filename = sl::String(filename.c_str());
                if (!encryption_key_str.empty())
                    rec_params.encryption_key = sl::String(encryption_key_str.c_str());
                if (cameras[i].stereo->enableRecording(rec_params) != sl::ERROR_CODE::SUCCESS)
                    std::cerr << "[Cam " << i << "] Failed to enable recording" << std::endl;
                else
                    std::cout << "[Cam " << i << "] Recording enabled" << (encryption_key_str.empty() ? "" : " (encrypted)") << " to "
                              << filename << std::endl;
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
                        size_t xpos = od_custom_onnx_input_shape.find('x');
                        if (xpos != std::string::npos) {
                            int w = std::stoi(od_custom_onnx_input_shape.substr(0, xpos));
                            int h = std::stoi(od_custom_onnx_input_shape.substr(xpos + 1));
                            od_params.custom_onnx_dynamic_input_shape = sl::Resolution(w, h);
                        }
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

    signal(SIGINT, signal_handler);

    std::vector<int> frames_counts(total_cameras, 0);
    std::vector<size_t> imu_sample_counts(total_cameras, 0);
    std::vector<std::vector<double>> imu_per_frame_all(total_cameras);

    // Warmup
    if (warmup > 0.f) {
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
    }

    sl::resetBenchmarkTimer();
    auto start_time = std::chrono::high_resolution_clock::now();

    // Main benchmark threads
    std::vector<std::thread> threads;
    for (int i = 0; i < total_cameras; i++) {
        threads.emplace_back([&, i]() {
            int& frame_count = frames_counts[i];
            size_t& imu_count = imu_sample_counts[i];
            auto& imu_per_frame = imu_per_frame_all[i];
            sl::Mat image;

            if (cameras[i].is_mono) {
                // --- Mono camera loop ---
                sl::CameraOne* zed = cameras[i].mono;
                std::cout << "[Cam " << i << "] Started (mono)." << std::endl;
                while (is_running) {
                    if (zed->grab() <= sl::ERROR_CODE::SUCCESS) {
                        // Use Resolution(-1,-1) for optimal working resolution
                        zed->retrieveImage(image, sl::VIEW::LEFT, sl::MEM::CPU, sl::Resolution(-1, -1));
                        frame_count++;
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
                        std::this_thread::sleep_for(std::chrono::milliseconds(1));
                    }
                }
            } else {
                // --- Stereo camera loop ---
                sl::Camera* zed = cameras[i].stereo;
                sl::RuntimeParameters runtime_params;
                sl::Mat depth_mat;
                sl::Objects objects;
                sl::Bodies bodies;
                sl::Pose pose;
                sl::ObjectDetectionRuntimeParameters od_runtime_params;
                sl::BodyTrackingRuntimeParameters bt_runtime_params;

                std::cout << "[Cam " << i << "] Started (stereo)." << std::endl;
                while (is_running) {
                    if (zed->grab(runtime_params) <= sl::ERROR_CODE::SUCCESS) {
                        // Use Resolution(-1,-1) for optimal working resolution
                        zed->retrieveImage(image, sl::VIEW::LEFT, sl::MEM::CPU, sl::Resolution(-1, -1));

                        if (depth_mode != sl::DEPTH_MODE::NONE)
                            zed->retrieveMeasure(depth_mat, sl::MEASURE::XYZ, sl::MEM::CPU, sl::Resolution(-1, -1));
                        if (enable_positional_tracking)
                            zed->getPosition(pose, sl::REFERENCE_FRAME::WORLD);
                        if (!od_model_str.empty())
                            zed->retrieveObjects(objects, od_runtime_params);
                        if (!bt_model_str.empty())
                            zed->retrieveBodies(bodies, bt_runtime_params);

                        frame_count++;
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
                        std::this_thread::sleep_for(std::chrono::milliseconds(1));
                    }
                }
            }
        });
    }

    // Status display loop
    while (is_running) {
        for (int i = 0; i < total_cameras; i++) {
            printf("[Cam %d] FPS: %.2f | Frames: %d\033[K\n", i, cameras[i].getCurrentFPS(), frames_counts[i]);
        }
        fflush(stdout);

        std::this_thread::sleep_for(std::chrono::seconds(1));
        auto current_time = std::chrono::high_resolution_clock::now();
        std::chrono::duration<double> elapsed = current_time - start_time;
        if (duration > 0 && elapsed.count() > duration) {
            std::cout << "\nBenchmark finished (duration reached)." << std::endl;
            is_running = false;
            break;
        }
        if (is_running)
            printf("\033[%dA", total_cameras);
    }

    std::cout << "\nStopping..." << std::endl;
    for (auto& t : threads)
        if (t.joinable())
            t.join();

    auto end_time = std::chrono::high_resolution_clock::now();

    for (int i = 0; i < total_cameras; i++) {
        if (cameras[i].is_mono && cameras[i].mono) {
            if (!compression_rec_str.empty())
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
            if (!compression_rec_str.empty())
                cameras[i].stereo->disableRecording();
            if (!compression_stream_str.empty())
                cameras[i].stereo->disableStreaming();
        }
        cameras[i].close();
    }

    std::cout << "Benchmark Finished." << std::endl;

    // Append IMU data to SDK benchmark JSON
    if (monitor_imu) {
        double total_s = std::chrono::duration<double>(end_time - start_time).count();
        // Read existing JSON, strip trailing }, append IMU sections, re-close
        std::string json_content;
        {
            std::ifstream ifs(output_report);
            if (ifs.is_open()) {
                json_content.assign(std::istreambuf_iterator<char>(ifs), std::istreambuf_iterator<char>());
                ifs.close();
            }
        }
        if (!json_content.empty()) {
            // Strip trailing whitespace and closing brace
            while (!json_content.empty()
                   && (json_content.back() == ' ' || json_content.back() == '\n' || json_content.back() == '\r'
                       || json_content.back() == '\t'))
                json_content.pop_back();
            if (!json_content.empty() && json_content.back() == '}')
                json_content.pop_back();

            std::ostringstream oss;
            oss << std::fixed;
            for (int i = 0; i < total_cameras; i++) {
                if (imu_sample_counts[i] == 0)
                    continue;
                std::string sn = std::to_string(cameras[i].serial_number);
                double rate = (total_s > 0) ? imu_sample_counts[i] / total_s : 0.0;
                double avg_per_frame = 0.0;
                if (!imu_per_frame_all[i].empty()) {
                    double sum = 0;
                    for (auto v : imu_per_frame_all[i])
                        sum += v;
                    avg_per_frame = sum / imu_per_frame_all[i].size();
                }
                oss << ",\n  \"IMU_SN" << sn << "\": {"
                    << "\n    \"imu_sample_count\": " << imu_sample_counts[i] << ",\n    \"imu_rate_hz\": " << std::setprecision(2) << rate
                    << ",\n    \"imu_per_frame\": " << std::setprecision(4) << avg_per_frame << "\n  }";
            }
            json_content += oss.str() + "\n}\n";

            std::ofstream ofs(output_report, std::ios::trunc);
            if (ofs.is_open()) {
                ofs << json_content;
                ofs.close();
            }
        }
    }

    return 0;
}
