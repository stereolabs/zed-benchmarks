/**
 * glass_to_glass.cpp: G2G display + decoder built on AprilTag fiducials carrying a binary,
 * CRC-checked timestamp.
 *
 * The screen renders the host CLOCK_REALTIME time, in milliseconds, as TAG_COUNT AprilTag
 * columns: the value is packed into PAYLOAD_BYTES big-endian bytes followed by CRC_BYTES of
 * CRC-16/CCITT, and each byte is shown as a marker whose id is that byte (0..255). Each byte
 * is drawn as TIMESTAMP_ROWS identical rows, so the markers stack into TAG_COUNT vertical
 * columns. The decoder groups the markers into columns (left-to-right by image x), trusts a
 * column only when every marker in it agrees, reassembles the bytes, and accepts the frame
 * only when the CRC matches, so a misread or a rolling-shutter tear straddling a timestamp
 * change is rejected rather than reconstructed into a mixed-time value. The reconstructed
 * millisecond timestamp is compared against the SDK image capture time; the human-readable
 * decimal time is drawn beneath the markers for verification. See glass_to_glass.hpp.
 *
 * Compiled only when HAVE_G2G is defined (see cpp/CMakeLists.txt).
 */
#ifdef HAVE_G2G

    #include "glass_to_glass.hpp"

    #include <sl/Camera.hpp>

    #include <opencv2/imgcodecs.hpp> // cv::imwrite for G2G_DEBUG dumps
    #include <opencv2/imgproc.hpp>
    #include <opencv2/objdetect.hpp> // cv::aruco

    #include <GL/gl.h>
    #include <GLFW/glfw3.h>

    #include <algorithm>
    #include <array>
    #include <atomic>
    #include <chrono>
    #include <condition_variable>
    #include <cstddef>
    #include <cstdint>
    #include <cstdio>
    #include <cstdlib>
    #include <ctime>
    #include <exception>
    #include <iostream>
    #include <memory>
    #include <mutex>
    #include <string>
    #include <thread>
    #include <vector>

namespace g2g {

    namespace {

        // AprilTag dictionary shared by renderer and decoder. DICT_APRILTAG_36h11 keeps parity with
        // python/glass_to_glass.py. The timestamp is packed into bytes (see below), so ids 0..255 are
        // used: all valid 36h11 codes (the family has 587). The 36h11 family carries strong per-tag
        // error detection/correction; the application CRC on top guards byte assembly and tears.
        // Renderer and decoder MUST agree on this.
        const cv::aruco::Dictionary& g2g_dict() {
            static const cv::aruco::Dictionary d = cv::aruco::getPredefinedDictionary(cv::aruco::DICT_APRILTAG_36h11);
            return d;
        }

        constexpr int BORDER_BITS = 1;

        // Binary timestamp layout shared by renderer and decoder. Epoch milliseconds are packed into
        // PAYLOAD_BYTES big-endian bytes (48 bits: good well past year 10000), followed by CRC_BYTES
        // of CRC-16/CCITT over those payload bytes. Each byte is one marker (id == byte value, 0..255),
        // so the whole timestamp is TAG_COUNT markers per row.
        constexpr int PAYLOAD_BYTES = 6;
        constexpr int CRC_BYTES = 2;
        constexpr int TAG_COUNT = PAYLOAD_BYTES + CRC_BYTES; // markers per row (vertical columns)

        // Human-readable decimal width: epoch ms is 13 digits until ~year 2286. Only the readable text
        // drawn beneath the markers uses this; the machine read is the binary marker block above.
        constexpr int TIMESTAMP_DIGITS = 13;

        // Each byte is drawn as TIMESTAMP_ROWS identical rows, so markers stack into TAG_COUNT vertical
        // columns. The decoder trusts a column only when every marker in it reads the same byte: a
        // misread or a rolling-shutter tear straddling a timestamp change makes the affected column(s)
        // disagree, and the frame is dropped before the CRC even runs. The CRC is the second line of
        // defence, and the only one for a tear that happens to leave every column internally consistent.
        constexpr int TIMESTAMP_ROWS = 5;

        // Minimum markers a column must actually carry before it is trusted. The per-column agreement
        // check is otherwise vacuous: a column reduced to a single surviving marker "agrees" with itself,
        // so the TIMESTAMP_ROWS-fold redundancy silently degrades to one read. Requiring a majority of
        // the rows keeps the redundancy real while tolerating a few dropped/blurred markers; raise it
        // toward TIMESTAMP_ROWS for stricter rejection at the cost of a lower detection rate.
        constexpr int MIN_COL_MARKERS = 3; // 3 of 5

        // Largest image dimension the decoder works at. With only TAG_COUNT columns the markers are
        // larger than the old 13-digit scheme, but the same budget keeps each well above the ~30 px
        // ArUco floor after downscale.
        constexpr int DECODE_MAX_DIM = 1280;

        // CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF, no reflection, no final xor) over `len` bytes.
        // Identical to python/glass_to_glass.py: renderer and decoder MUST agree bit-for-bit.
        uint16_t crc16_ccitt(const uint8_t* data, size_t len) {
            uint16_t crc = 0xFFFF;
            for (size_t i = 0; i < len; ++i) {
                crc ^= static_cast<uint16_t>(data[i]) << 8;
                for (int b = 0; b < 8; ++b)
                    crc = (crc & 0x8000) ? static_cast<uint16_t>((crc << 1) ^ 0x1021) : static_cast<uint16_t>(crc << 1);
            }
            return crc;
        }

        // Pack epoch-ms into the TAG_COUNT marker ids: PAYLOAD_BYTES big-endian payload bytes then
        // CRC_BYTES of CRC-16 over them. Mirrored by the decoder (reassemble + verify).
        void encode_timestamp(uint64_t ms, int tags[TAG_COUNT]) {
            uint8_t payload[PAYLOAD_BYTES];
            for (int i = 0; i < PAYLOAD_BYTES; ++i)
                payload[i] = static_cast<uint8_t>((ms >> (8 * (PAYLOAD_BYTES - 1 - i))) & 0xFFu);
            const uint16_t crc = crc16_ccitt(payload, PAYLOAD_BYTES);
            for (int i = 0; i < PAYLOAD_BYTES; ++i)
                tags[i] = payload[i];
            tags[PAYLOAD_BYTES] = (crc >> 8) & 0xFF;
            tags[PAYLOAD_BYTES + 1] = crc & 0xFF;
        }

        // G2G_DEBUG=<dir>: print decoded ids and dump annotated detections for diagnosing locks.
        const char* g2g_debug_dir() {
            static const char* d = std::getenv("G2G_DEBUG");
            return d;
        }

        // Fill `grid` (side*side, 1 = black cell) with the marker bitmap for `id`. Returns side.
        int build_marker_grid(int id, std::vector<uint8_t>& grid) {
            const int side = g2g_dict().markerSize + 2 * BORDER_BITS;
            cv::Mat img;
            cv::aruco::generateImageMarker(g2g_dict(), id, side, img, BORDER_BITS);
            grid.assign(static_cast<size_t>(side) * side, 0);
            for (int y = 0; y < side; ++y)
                for (int x = 0; x < side; ++x)
                    grid[static_cast<size_t>(y) * side + x] = (img.at<uchar>(y, x) == 0) ? 1 : 0;
            return side;
        }

    } // namespace

    uint64_t realtime_now_ns() {
        struct timespec ts;
        clock_gettime(CLOCK_REALTIME, &ts);
        return static_cast<uint64_t>(ts.tv_sec) * 1000000000ull + static_cast<uint64_t>(ts.tv_nsec);
    }

    // ── Decoder ──────────────────────────────────────────────────────────────────
    struct Decoder::Impl {
        cv::aruco::ArucoDetector detector;
        unsigned long long implausible_time_count = 0; // decodes rejected by the plausibility window
        Impl()
            : detector(g2g_dict(), cv::aruco::DetectorParameters()) { }
    };

    Decoder::Decoder()
        : p_(new Impl()) { }
    Decoder::~Decoder() {
        delete p_;
    }

    DecodeResult Decoder::decode(const sl::Mat& image, uint64_t reference_now_ns) {
        const int w = static_cast<int>(image.getWidth());
        const int h = static_cast<int>(image.getHeight());
        if (w <= 0 || h <= 0)
            return {Status::NO_SIGNAL, 0};

        unsigned char* ptr = image.getPtr<sl::uchar1>(sl::MEM::CPU);
        if (!ptr)
            return {Status::NO_SIGNAL, 0};
        const size_t step = image.getStepBytes(sl::MEM::CPU);

        cv::Mat bgra(h, w, CV_8UC4, reinterpret_cast<void*>(ptr), step);
        cv::Mat gray;
        cv::cvtColor(bgra, gray, cv::COLOR_BGRA2GRAY);

        // Downscale for a light decode budget; markers stay above the ~30 px ArUco floor.
        double sc = static_cast<double>(DECODE_MAX_DIM) / std::max(w, h);
        if (sc < 1.0)
            cv::resize(gray, gray, cv::Size(), sc, sc, cv::INTER_AREA);

        std::vector<int> ids;
        std::vector<std::vector<cv::Point2f>> corners;
        p_->detector.detectMarkers(gray, corners, ids);

        // Debug dump (G2G_DEBUG=<dir>): annotate the actual outcome ON the dumped PNG so it is
        // self-explanatory: decoded timestamp + host delta when locked, or the reject reason (with
        // the offending values) when not. The write is deferred to finish(), so every return below
        // goes through finish(...) after setting dbg_note (and dbg_ms once a value is reconstructed);
        // when G2G_DEBUG is unset finish() is a no-op pass-through.
        const char* dbg = g2g_debug_dir();
        std::vector<int> column_values; // left-to-right; declared here so a partial dump can show it
        std::string dbg_note;
        uint64_t dbg_ms = 0, dbg_ref_ms = 0;
        bool dbg_locked = false;
        std::vector<cv::Rect> dbg_boxes; // regions to ring in red on a failed decode (decode-image coords)

        auto ms_to_clock = [](uint64_t ms) {
            const time_t secs = static_cast<time_t>(ms / 1000ull);
            struct tm tmv;
            localtime_r(&secs, &tmv);
            char b[32];
            snprintf(b, sizeof(b), "%02d:%02d:%02d.%03d", tmv.tm_hour, tmv.tm_min, tmv.tm_sec, static_cast<int>(ms % 1000ull));
            return std::string(b);
        };

        auto finish = [&](DecodeResult r) -> DecodeResult {
            if (!dbg)
                return r;
            std::cout << "[G2G] detected ids:";
            for (int id : ids)
                std::cout << ' ' << id;
            std::cout << "  -> " << (dbg_note.empty() ? "?" : dbg_note) << std::endl;

            static std::atomic<int> dump_n(0);
            if (dump_n.load(std::memory_order_relaxed) >= 120)
                return r;

            cv::Mat vis;
            cv::cvtColor(gray, vis, cv::COLOR_GRAY2BGR);

            const double fs = std::max(0.6, vis.cols / 1280.0); // font scale tracks image size
            const int thick = std::max(2, static_cast<int>(2 * fs + 0.5));

            // Ring whatever the failing path flagged as wrong, in red FIRST, so the marker outlines
            // and the text overlay below draw on top of it and stay legible.
            for (const cv::Rect& r : dbg_boxes)
                cv::rectangle(vis, r, cv::Scalar(0, 0, 255), std::max(2, static_cast<int>(3 * fs)));

            if (!ids.empty())
                cv::aruco::drawDetectedMarkers(vis, corners, ids);

            int line = 0;
            auto label = [&](const std::string& s, const cv::Scalar& color) {
                const cv::Point org(12, static_cast<int>((28 + line * 34) * fs));
                cv::putText(vis, s, org, cv::FONT_HERSHEY_SIMPLEX, fs, cv::Scalar(0, 0, 0), thick + 3, cv::LINE_AA); // outline
                cv::putText(vis, s, org, cv::FONT_HERSHEY_SIMPLEX, fs, color, thick, cv::LINE_AA);
                ++line;
            };

            if (dbg_locked) {
                const cv::Scalar green(0, 200, 0);
                const long long d = static_cast<long long>(dbg_ref_ms) - static_cast<long long>(dbg_ms);
                label("LOCKED  " + std::to_string(dbg_ms) + " ms  (" + ms_to_clock(dbg_ms) + ")", green);
                label("host-now lead: " + std::to_string(d) + " ms", green); // ~glass-to-glass latency
            } else {
                label("REJECT: " + dbg_note, cv::Scalar(0, 0, 255));
                if (dbg_ms)
                    label("decoded " + std::to_string(dbg_ms) + " ms  (" + ms_to_clock(dbg_ms) + ")", cv::Scalar(0, 0, 255));
            }
            std::string cols = "cols:";
            for (int v : column_values)
                cols += ' ' + std::to_string(v);
            cols += "   markers:" + std::to_string(ids.size());
            label(cols, cv::Scalar(0, 220, 220));

            char name[1024];
            snprintf(name, sizeof(name), "%s/g2g_aruco_%03d.png", dbg, dump_n.load(std::memory_order_relaxed));
            cv::imwrite(name, vis);
            dump_n.fetch_add(1, std::memory_order_relaxed);
            return r;
        };

        // Keep only byte markers (id 0..255), with their image center and on-image width. The
        // timestamp is drawn as TIMESTAMP_ROWS identical rows, so markers stack into vertical
        // columns; width sets the clustering scale below (robust to perspective).
        struct Marker {
            double x, y;              // center
            double w;                 // on-image width
            int v;                    //
            float bx0, by0, bx1, by1; // bounding box (decode-image coords) for the debug highlight
        };
        std::vector<Marker> markers;
        markers.reserve(ids.size());
        for (size_t i = 0; i < ids.size(); ++i) {
            if (ids[i] < 0 || ids[i] > 255 || corners[i].size() != 4)
                continue;
            double cx = 0.0, cy = 0.0;
            double minx = corners[i][0].x, maxx = corners[i][0].x;
            double miny = corners[i][0].y, maxy = corners[i][0].y;
            for (const cv::Point2f& pt : corners[i]) {
                cx += pt.x;
                cy += pt.y;
                minx = std::min(minx, static_cast<double>(pt.x));
                maxx = std::max(maxx, static_cast<double>(pt.x));
                miny = std::min(miny, static_cast<double>(pt.y));
                maxy = std::max(maxy, static_cast<double>(pt.y));
            }
            markers.push_back(
                {cx * 0.25,
                 cy * 0.25,
                 maxx - minx,
                 ids[i],
                 static_cast<float>(minx),
                 static_cast<float>(miny),
                 static_cast<float>(maxx),
                 static_cast<float>(maxy)}
            );
        }
        if (markers.empty()) {
            // Nothing usable as a byte marker, ring whatever ArUco *did* find so the dump shows
            // detection happened but every id fell outside 0..255 (or had bad corners).
            if (dbg)
                for (const std::vector<cv::Point2f>& c : corners) {
                    if (c.size() != 4)
                        continue;
                    float x0 = c[0].x, y0 = c[0].y, x1 = c[0].x, y1 = c[0].y;
                    for (const cv::Point2f& pt : c) {
                        x0 = std::min(x0, pt.x);
                        y0 = std::min(y0, pt.y);
                        x1 = std::max(x1, pt.x);
                        y1 = std::max(y1, pt.y);
                    }
                    dbg_boxes.emplace_back(
                        cv::Point(static_cast<int>(x0) - 4, static_cast<int>(y0) - 4),
                        cv::Point(static_cast<int>(x1) + 4, static_cast<int>(y1) + 4)
                    );
                }
            dbg_note = "no byte markers";
            return finish({Status::NO_SIGNAL, 0});
        }

        // Group markers into columns by image x. Within a column markers are vertically aligned
        // (near-equal x); adjacent columns sit a full pitch apart (> one marker width), so an
        // x-gap wider than ~0.6 marker widths starts a new column. The median width is a
        // perspective-robust scale.
        std::sort(markers.begin(), markers.end(), [](const Marker& a, const Marker& b) {
            return a.x < b.x;
        });
        std::vector<double> widths;
        widths.reserve(markers.size());
        for (const Marker& m : markers)
            widths.push_back(m.w);
        std::nth_element(widths.begin(), widths.begin() + widths.size() / 2, widths.end());
        const double col_gap = 0.6 * widths[widths.size() / 2];

        // Bounding box (with a little padding) around markers[a, b): used to ring a column, a single
        // offending marker, or the whole reconstructed block in red on the debug dump.
        auto col_box = [&](size_t a, size_t b) -> cv::Rect {
            float x0 = markers[a].bx0, y0 = markers[a].by0, x1 = markers[a].bx1, y1 = markers[a].by1;
            for (size_t k = a + 1; k < b; ++k) {
                x0 = std::min(x0, markers[k].bx0);
                y0 = std::min(y0, markers[k].by0);
                x1 = std::max(x1, markers[k].bx1);
                y1 = std::max(y1, markers[k].by1);
            }
            return cv::Rect(
                cv::Point(static_cast<int>(x0) - 4, static_cast<int>(y0) - 4),
                cv::Point(static_cast<int>(x1) + 4, static_cast<int>(y1) + 4)
            );
        };
        std::vector<cv::Rect> dbg_col_boxes; // per-column boxes, kept so a value-level reject can ring the whole block

        // A column is trusted only when every marker in it reads the same byte. A misread, or a
        // rolling-shutter tear straddling a timestamp change (top rows old, bottom rows new),
        // makes the affected column disagree, and the whole frame is dropped, never mixed.
        for (size_t i = 0; i < markers.size();) {
            const int v = markers[i].v;
            size_t j = i + 1;
            while (j < markers.size() && markers[j].x - markers[j - 1].x <= col_gap) {
                if (markers[j].v != v) {
                    if (dbg) {
                        size_t e = j; // walk to the column end so the whole cluster is ringed
                        while (e < markers.size() && markers[e].x - markers[e - 1].x <= col_gap)
                            ++e;
                        dbg_boxes.push_back(col_box(i, e));     // the disagreeing column
                        dbg_boxes.push_back(col_box(j, j + 1)); // the specific conflicting marker
                    }
                    dbg_note = "column disagreement (" + std::to_string(v) + " vs " + std::to_string(markers[j].v) + ")";
                    return finish({Status::NO_SIGNAL, 0});
                }
                ++j;
            }
            // The agreement above is only meaningful with enough rows: a sparse column (most of its
            // markers dropped, e.g. one side lost to a tear) would otherwise pass trivially. Require a
            // real majority of the rows so a torn column is far more likely to still hold a
            // disagreeing marker and be rejected.
            if (static_cast<int>(j - i) < MIN_COL_MARKERS) {
                if (dbg)
                    dbg_boxes.push_back(col_box(i, j));
                const size_t got = j - i;
                dbg_note = "sparse column (got only " + std::to_string(got) + (got > 1 ? " markers" : " marker") + " instead of minimum "
                    + std::to_string(MIN_COL_MARKERS) + ")";
                return finish({Status::NO_SIGNAL, 0});
            }
            if (dbg)
                dbg_col_boxes.push_back(col_box(i, j));
            column_values.push_back(v);
            i = j;
        }

        // Require the full timestamp: exactly TAG_COUNT consistent columns (payload + CRC).
        if (static_cast<int>(column_values.size()) != TAG_COUNT) {
            if (dbg)
                dbg_boxes = dbg_col_boxes; // ring every column found so the count is visible
            dbg_note = "wrong column count (" + std::to_string(column_values.size()) + " != " + std::to_string(TAG_COUNT) + ")";
            return finish({Status::NO_SIGNAL, 0});
        }

        // Reassemble the bytes and verify the CRC. column_values are the marker ids left-to-right:
        // PAYLOAD_BYTES big-endian payload bytes then CRC_BYTES of CRC-16. A misread that slipped past
        // per-tag 36h11 correction, or a tear that left every column internally consistent, fails the
        // CRC here and the frame is dropped.
        uint8_t payload[PAYLOAD_BYTES];
        for (int i = 0; i < PAYLOAD_BYTES; ++i)
            payload[i] = static_cast<uint8_t>(column_values[i]);
        const uint16_t crc_read = static_cast<uint16_t>((column_values[PAYLOAD_BYTES] << 8) | column_values[PAYLOAD_BYTES + 1]);
        if (crc16_ccitt(payload, PAYLOAD_BYTES) != crc_read) {
            if (dbg)
                dbg_boxes = dbg_col_boxes; // a misread byte we can't localize, ring the whole block
            dbg_note = "CRC mismatch";
            return finish({Status::NO_SIGNAL, 0});
        }

        uint64_t ms = 0;
        for (int i = 0; i < PAYLOAD_BYTES; ++i)
            ms = (ms << 8) | static_cast<uint64_t>(payload[i]);

        // Plausibility guard: the screen shows ~the reference "now", so a reconstruction far from
        // it is a misread, not a real frame. Live capture uses the host clock (reference_now_ns==0);
        // SVO playback passes the frame's reconstructed live "now" (the recorded screen time is not
        // the host's current time).
        const uint64_t now_ms = (reference_now_ns ? reference_now_ns : realtime_now_ns()) / 1000000ull;
        dbg_ms = ms;
        dbg_ref_ms = now_ms;
        // ms is uint64_t epoch-ms, so a zero/garbage value is already caught by the >10s-old test
        // below (now_ms - ms is huge); no separate "ms <= 0" check is needed.
        if (ms > now_ms + 1000ull || (now_ms > ms && now_ms - ms > 10000ull)) {
            if (dbg)
                dbg_boxes = dbg_col_boxes;
            dbg_note = "implausible time";
            ++p_->implausible_time_count;
            return finish({Status::NO_SIGNAL, 0});
        }

        dbg_locked = true;
        dbg_note = "LOCKED";
        return finish({Status::LOCKED, ms * 1000000ull});
    }

    unsigned long long Decoder::implausibleTimeCount() const {
        return p_->implausible_time_count;
    }

    // ── Async decoder ─────────────────────────────────────────────────────────────
    // Ownership of the frame buffers is transferred between threads by sl::Mat::swap (pointer-only,
    // no data copy), so after warm-up no per-frame allocation happens and no buffer is ever touched
    // by two threads at once:
    //   - `staging` is the grab thread's scratch: the deep copy lands here (outside the lock), then
    //     it is swapped into `mailbox` under the lock. Only the grab thread ever touches it.
    //   - `mailbox` is the single slot, always accessed under `m`.
    //   - `work` is the worker's frame under decode. Only the worker ever touches it.
    struct AsyncDecoder::Impl {
        Decoder decoder; // worker-owned
        Display* display = nullptr;
        int cam_id = 0; // which camera's feedback slot this worker updates on the display
        std::string ctx;

        std::thread th;
        std::mutex m;
        std::condition_variable cv;
        bool started = false;
        bool stop_flag = false;
        bool has_frame = false; // mailbox holds a frame the worker has not yet taken
        sl::Mat staging;        // grab-thread scratch for the deep copy
        sl::Mat mailbox;        // single slot, guarded by m
        long long mailbox_ns = 0;
        unsigned long long dropped_count = 0;
        unsigned long long implausible_count = 0; // LOCKED decodes with non-positive latency (bad clock offset)

        sl::Mat work;                  // worker-owned frame being decoded
        std::vector<double> latencies; // worker-owned; no lock on the hot path

        void worker_loop() {
            for (;;) {
                long long current_ns = 0;
                {
                    std::unique_lock<std::mutex> lk(m);
                    cv.wait(lk, [this] {
                        return has_frame || stop_flag;
                    });
                    if (!has_frame) // woken to stop with an empty mailbox
                        break;
                    sl::Mat::swap(mailbox, work); // take ownership of the frame (pointer swap)
                    current_ns = mailbox_ns;
                    has_frame = false;
                }

                // A throw from decode()/display->update() must not escape the thread
                // function: that would call std::terminate() and abort the whole
                // benchmark. Log it and stop the worker cleanly instead.
                try {
                    const DecodeResult r = decoder.decode(work, (uint64_t)current_ns);

                    double lat_ms = 0.0;
                    if (r.status == Status::LOCKED) {
                        lat_ms = static_cast<double>(current_ns - static_cast<long long>(r.display_ns)) / 1.0e6;
                        // Non-positive latency = the on-screen time decoded after capture, i.e. the
                        // clock offset is wrong: drop it rather than biasing the stats.
                        if (lat_ms > 0.0)
                            latencies.push_back(lat_ms);
                        else
                            ++implausible_count;
                    }
                    if (display)
                        display->update(r.status, lat_ms, cam_id);
                } catch (const std::exception& e) {
                    std::cerr << "[G2G] async decoder worker stopped on exception: " << e.what() << std::endl;
                    break;
                } catch (...) {
                    std::cerr << "[G2G] async decoder worker stopped on unknown exception" << std::endl;
                    break;
                }
            }
        }
    };

    AsyncDecoder::AsyncDecoder()
        : p_(new Impl()) { }
    AsyncDecoder::~AsyncDecoder() {
        stop();
        delete p_;
    }

    void AsyncDecoder::start(Display* display, const std::string& profile_context, size_t latency_reserve, int cam_id) {
        p_->display = display;
        p_->cam_id = cam_id;
        p_->ctx = profile_context;
        if (latency_reserve)
            p_->latencies.reserve(latency_reserve);
        p_->stop_flag = false;
        p_->started = true;
        p_->th = std::thread([this] {
            p_->worker_loop();
        });
    }

    void AsyncDecoder::submit(const sl::Mat& image, long long current_ns) {
        // Deep-copy outside the lock (the SDK reuses `image` on the next grab); the lock then only
        // swaps the staging buffer into the mailbox and flips the flag: minimal hold time.
        if (image.copyTo(p_->staging, sl::COPY_TYPE::CPU_CPU) != sl::ERROR_CODE::SUCCESS)
            return;
        {
            std::lock_guard<std::mutex> lk(p_->m);
            if (p_->has_frame)
                ++p_->dropped_count; // overwriting a frame the worker never took
            sl::Mat::swap(p_->staging, p_->mailbox);
            p_->mailbox_ns = current_ns;
            p_->has_frame = true;
        }
        p_->cv.notify_one();
    }

    void AsyncDecoder::stop() {
        if (!p_->started)
            return;
        {
            std::lock_guard<std::mutex> lk(p_->m);
            p_->stop_flag = true;
        }
        p_->cv.notify_one();
        if (p_->th.joinable())
            p_->th.join();
        p_->started = false;
        std::cout << "[G2G] async decoder dropped " << p_->dropped_count << " frame(s) (worker busy)";
        if (p_->implausible_count)
            std::cout << "; rejected " << p_->implausible_count << " sample(s) with non-positive latency (bad clock offset)";
        const unsigned long long impl_time = p_->decoder.implausibleTimeCount();
        if (impl_time)
            std::cout << "; rejected " << impl_time << " decode(s) with an implausible timestamp (check the clock offset)";
        std::cout << std::endl;
    }

    unsigned long long AsyncDecoder::dropped() const {
        return p_->dropped_count;
    }

    std::vector<double> AsyncDecoder::takeLatencies() {
        return std::move(p_->latencies);
    }

    // ── Display ──────────────────────────────────────────────────────────────────
    namespace {

        void draw_rect(double x, double y, double w, double h) {
            glBegin(GL_QUADS);
            glVertex2d(x, y);
            glVertex2d(x + w, y);
            glVertex2d(x + w, y + h);
            glVertex2d(x, y + h);
            glEnd();
        }

        // Upload a marker bit grid (1 = black cell) as an RGBA texture once at startup: black-opaque
        // for code cells, fully transparent for the rest so the white background shows through (the
        // ArUco quiet zone). GL_NEAREST keeps the cell edges crisp when the small side×side bitmap is
        // scaled up to the on-screen marker, reproducing the old per-cell quad rendering exactly while
        // collapsing the ~side² immediate-mode quads per marker into a single textured quad, so almost
        // no CPU/GL work sits between sampling the clock and presenting the frame (see render_loop).
        GLuint upload_marker_texture(const std::vector<uint8_t>& grid, int side) {
            std::vector<uint8_t> rgba(static_cast<size_t>(side) * side * 4, 0); // all transparent
            for (size_t i = 0; i < grid.size(); ++i)
                if (grid[i])
                    rgba[i * 4 + 3] = 255; // RGB stay 0 (black); alpha marks the code cell
            GLuint tex = 0;
            glGenTextures(1, &tex);
            glBindTexture(GL_TEXTURE_2D, tex);
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
            glPixelStorei(GL_UNPACK_ALIGNMENT, 1);
            glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, side, side, 0, GL_RGBA, GL_UNSIGNED_BYTE, rgba.data());
            return tex;
        }

        // Draw a pre-baked marker texture into the screen box (x0,y0,size) as one textured quad.
        // GL_TEXTURE_2D and the white modulation color are set once by the caller around the whole
        // marker batch, so the post-sample path is just one bind + one quad per marker.
        void draw_marker_tex(GLuint tex, double x0, double y0, double size) {
            glBindTexture(GL_TEXTURE_2D, tex);
            glBegin(GL_QUADS);
            glTexCoord2f(0.f, 0.f);
            glVertex2d(x0, y0);
            glTexCoord2f(1.f, 0.f);
            glVertex2d(x0 + size, y0);
            glTexCoord2f(1.f, 1.f);
            glVertex2d(x0 + size, y0 + size);
            glTexCoord2f(0.f, 1.f);
            glVertex2d(x0, y0 + size);
            glEnd();
        }

        // Real-text rendering: cv::putText draws the string (black, anti-aliased) into an RGBA
        // bitmap with a transparent background, which is uploaded as a GL texture and drawn as a
        // textured quad. This replaces the old 7-segment glyphs so the human-readable digits and
        // the latency readout appear as real text. The text is for human verification only: the
        // machine-read timestamp is the ArUco marker row above it. Each instance owns one texture
        // and re-uploads only when its string changes; it must be used on the render thread.
        struct GlText {
            GLuint tex = 0;
            int tw = 0, th = 0; // texture size in pixels
            std::string cached; // last rendered string (skip re-upload when unchanged)

            void render(const std::string& s) {
                if (tex && s == cached)
                    return;
                cached = s;
                const int font = cv::FONT_HERSHEY_SIMPLEX;
                const double scale = 4.0; // high-res master; the quad scales it to any draw size
                const int thick = 8;
                int baseline = 0;
                const cv::Size sz = cv::getTextSize(s.empty() ? " " : s, font, scale, thick, &baseline);
                const int pad = thick + 4;
                tw = sz.width + 2 * pad;
                th = sz.height + baseline + 2 * pad;
                cv::Mat canvas(th, tw, CV_8UC4, cv::Scalar(0, 0, 0, 0));
                cv::putText(canvas, s, cv::Point(pad, pad + sz.height), font, scale, cv::Scalar(0, 0, 0, 255), thick, cv::LINE_AA);
                if (!tex)
                    glGenTextures(1, &tex);
                glBindTexture(GL_TEXTURE_2D, tex);
                glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
                glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
                glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
                glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
                glPixelStorei(GL_UNPACK_ALIGNMENT, 1);
                // RGB are all 0 (black), so RGBA vs BGRA byte order is irrelevant; the alpha
                // channel carries the anti-aliased glyph mask.
                glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, tw, th, 0, GL_RGBA, GL_UNSIGNED_BYTE, canvas.data);
            }

            double width_for_height(double h) const {
                return th ? h * tw / th : 0.0;
            }

            // Draw with glyph-box height `h`, top-left at (x,y).
            void draw(double x, double y, double h) const {
                const double w = width_for_height(h);
                glEnable(GL_TEXTURE_2D);
                glBindTexture(GL_TEXTURE_2D, tex);
                glColor4f(1.f, 1.f, 1.f, 1.f);
                glBegin(GL_QUADS);
                glTexCoord2f(0.f, 0.f);
                glVertex2d(x, y);
                glTexCoord2f(1.f, 0.f);
                glVertex2d(x + w, y);
                glTexCoord2f(1.f, 1.f);
                glVertex2d(x + w, y + h);
                glTexCoord2f(0.f, 1.f);
                glVertex2d(x, y + h);
                glEnd();
                glDisable(GL_TEXTURE_2D);
            }

            void destroy() {
                if (tex) {
                    glDeleteTextures(1, &tex);
                    tex = 0;
                }
            }
        };

    } // namespace

    // Per-camera decode feedback shown on screen. One instance per camera filming the display; all
    // fields are atomics written by that camera's decode thread and read by the render thread, so no
    // lock is needed. Non-copyable/movable (atomics), hence held by unique_ptr in a vector.
    struct CamFeedback {
        std::atomic<long long> latency_us {0};
        std::atomic<long long> last_success_ns {-1};       // steady_clock ns of the most recent LOCKED decode (-1 = never)
        std::atomic<unsigned long long> success_count {0}; // running total of successful detections
        std::string label;                                 // camera serial (or "" when unlabeled)
    };

    struct Display::Impl {
        std::thread th;
        std::atomic<bool> running {false};
        std::atomic<bool> init_done {false};
        std::atomic<bool> init_ok {false};

        // One feedback slot per camera (worker index order). Built in the Display ctor with a single
        // default slot so the single-camera benchmark works without calling setCameras(); setCameras()
        // rebuilds it. The render thread reads cams[] but never resizes it, and setCameras() must run
        // before start(), so the vector itself is stable while the render thread is alive.
        std::vector<std::unique_ptr<CamFeedback>> cams;

        int grid_side = 0;
        std::array<std::vector<uint8_t>, 256> byte_grids; // precomputed ArUco bitmaps for byte values 0..255
        std::array<GLuint, 256> marker_tex {};            // pre-baked byte-marker textures (built on the render thread)
        std::array<GlText, 10> digit_text;                // real-text glyphs for digits 0..9 (lazily uploaded)
        std::vector<GlText> readout_text;                 // one real-text readout row per camera (render-thread owned)

        void render_loop() {
            if (!glfwInit()) {
                init_ok = false;
                init_done = true;
                return;
            }
            glfwWindowHint(GLFW_DECORATED, GLFW_FALSE);
            GLFWmonitor* mon = glfwGetPrimaryMonitor();
            const GLFWvidmode* vm = mon ? glfwGetVideoMode(mon) : nullptr;
            int win_w = vm ? vm->width : 1280;
            int win_h = vm ? vm->height : 720;
            GLFWwindow* win = glfwCreateWindow(win_w, win_h, "ZED Glass-to-Glass", mon, nullptr);
            if (!win) {
                glfwTerminate();
                init_ok = false;
                init_done = true;
                return;
            }
            glfwMakeContextCurrent(win);
            // vsync OFF + ~1 kHz redraw (see loop bottom): the on-screen millisecond value is
            // re-sampled and repainted about every 1 ms, so the encoded time is never more than
            // ~1 ms stale. The timestamp is drawn as TIMESTAMP_ROWS identical rows; a screen tear
            // (or rolling-shutter readout) straddling a timestamp change leaves the rows above it
            // showing the old value and those below the new one, so the changed byte's column
            // disagrees and the decoder drops the frame: it can never produce a mixed timestamp.
            // The panel still emits photons only at its own refresh rate: a 1 ms refresh removes
            // encode staleness, not panel latency.
            glfwSwapInterval(0);
            glEnable(GL_BLEND); // alpha-blend the anti-aliased text textures over the white background
            glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA);
            // Pre-bake all per-frame textures now (GL context is current) so nothing on the
            // post-clock-sample path ever does a cv::putText or texture upload: every byte-marker
            // bitmap (0..255) becomes one textured quad each (vs ~side² immediate-mode quads), and the
            // readable-digit glyphs ('0'..'9') are uploaded once so their per-frame render() is a
            // pure cache hit instead of a first-appearance putText+upload during warmup.
            for (int b = 0; b < 256; ++b)
                marker_tex[b] = upload_marker_texture(byte_grids[b], grid_side);
            for (int d = 0; d < 10; ++d)
                digit_text[d].render(std::string(1, static_cast<char>('0' + d)));
            // One readout row per camera (cams[] is fixed by the time start() runs). Lazily uploaded
            // on first render() per row, like the digits.
            readout_text.resize(cams.size());
            init_ok = true;
            init_done = true;

            while (running) {
                const auto iter_start = std::chrono::steady_clock::now();
                int fbw, fbh;
                glfwGetFramebufferSize(win, &fbw, &fbh);
                glViewport(0, 0, fbw, fbh);
                glMatrixMode(GL_PROJECTION);
                glLoadIdentity();
                glOrtho(0, fbw, fbh, 0, -1, 1); // (0,0) top-left
                glMatrixMode(GL_MODELVIEW);
                glLoadIdentity();

                glClearColor(1.f, 1.f, 1.f, 1.f); // white background / quiet zone
                glClear(GL_COLOR_BUFFER_BIT);

                // Layout is fixed (TAG_COUNT marker columns up top, TIMESTAMP_DIGITS readable digits
                // below), so it needs no clock sample. TIMESTAMP_ROWS identical rows, one marker per
                // byte per row, leave a white quiet zone between markers (horizontally and vertically)
                // and a readable decimal row below the whole block for human verification.
                const int n_tags = TAG_COUNT;          // machine-read marker columns
                const int n_digits = TIMESTAMP_DIGITS; // human-readable decimal digits
                const double margin = 0.05 * fbw;
                const double pitch = (fbw - 2.0 * margin) / n_tags;         // marker column pitch
                const double digit_pitch = (fbw - 2.0 * margin) / n_digits; // readable digit pitch
                const double rows_top = 0.04 * fbh;                         // top of the marker block
                const double rows_region = 0.78 * fbh;                      // vertical band the rows share
                const double row_pitch = rows_region / TIMESTAMP_ROWS;
                // Square markers fit both the horizontal pitch and the per-row vertical band.
                const double msize = std::min(pitch * 0.74, row_pitch * 0.74);
                const double text_y = rows_top + rows_region + msize * 0.22; // below the block
                // Readable digit drawn large for legibility, clamped to fit below the block.
                double gh = msize * 0.9;
                const double avail = fbh * 0.97 - text_y;
                if (gh > avail)
                    gh = avail;

                // Draw everything that does NOT depend on the clock first, so the only work between
                // sampling the timestamp and presenting the frame is the marker block plus the small
                // human-only readable-digit quads: the encoded millisecond then matches the emitted
                // photons as closely as the panel allows.

                // Status feedback (bright colors, never confused with a marker cell). Hold the
                // border green as long as ANY camera locked within the last second; only fall back to
                // red after a full second with no success on any camera. This stops the border
                // flickering green/red when detections lock intermittently from frame to frame.
                const long long now_ns
                    = std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now().time_since_epoch()).count();
                bool any_locked_recently = false;
                for (const std::unique_ptr<CamFeedback>& c : cams) {
                    const long long last_ns = c->last_success_ns.load();
                    if (last_ns >= 0 && (now_ns - last_ns) <= 1000000000ll) {
                        any_locked_recently = true;
                        break;
                    }
                }
                if (any_locked_recently)
                    glColor3f(0.f, 0.8f, 0.f);
                else
                    glColor3f(0.9f, 0.f, 0.f);
                const double bt = 0.02 * fbh; // status border thickness
                draw_rect(0, 0, fbw, bt);
                draw_rect(0, fbh - bt, fbw, bt);
                draw_rect(0, 0, bt, fbh);
                draw_rect(fbw - bt, 0, bt, fbh);

                // Human-readable readout at the very top edge: all cameras on a single line, drawn
                // side by side, each showing that camera's latest latency once locked plus its running
                // detection count (always shown). Human-only: drawn before the clock sample so the
                // occasional cv::putText + texture re-upload never delays the markers. The single row
                // sits in the band ABOVE the marker block (markers start at rows_top = 0.04*fbh) and
                // never occludes a decode column. With many cameras the line can run past the right
                // edge; that only clips the readout, not the markers below.
                const size_t n_cams = cams.size();
                const double readout_band = rows_top * 0.92; // keep clear of the markers
                const double glyph_h = readout_band * 0.82;  // single row; leave a small gap below
                const double gap = 0.02 * fbw;               // horizontal space between cameras
                double rx = 0.04 * fbw;
                for (size_t ci = 0; ci < n_cams; ++ci) {
                    const CamFeedback& c = *cams[ci];
                    const long long last_ns = c.last_success_ns.load();
                    const unsigned long long count = c.success_count.load();
                    const std::string tag = "C" + std::to_string(ci) + (c.label.empty() ? "" : " [" + c.label + "]");
                    char buf[96];
                    if (last_ns >= 0)
                        snprintf(buf, sizeof(buf), "%s: %.1f ms   detections: %llu", tag.c_str(), c.latency_us.load() / 1000.0, count);
                    else
                        snprintf(buf, sizeof(buf), "%s: --   detections: %llu", tag.c_str(), count);
                    GlText& t = readout_text[ci];
                    t.render(buf);
                    t.draw(rx, 0.004 * fbh, glyph_h);
                    rx += t.width_for_height(glyph_h) + gap;
                }

                // Sample the host clock as late as possible, then pack that millisecond into the
                // TAG_COUNT byte markers (payload + CRC) and render them with the pre-baked marker
                // textures (one textured quad each: see upload_marker_texture).
                const uint64_t present_ns = realtime_now_ns();
                const uint64_t present_ms = present_ns / 1000000ull;
                int tags[TAG_COUNT];
                encode_timestamp(present_ms, tags);

                // The machine-read marker block: one batch with texturing/color set once, so the
                // post-sample path is just TAG_COUNT×TIMESTAMP_ROWS binds+quads and the swap.
                glEnable(GL_TEXTURE_2D);
                glColor4f(1.f, 1.f, 1.f, 1.f);
                for (int i = 0; i < n_tags; ++i) {
                    const int b = tags[i];
                    const double mx = margin + i * pitch + (pitch - msize) * 0.5;
                    for (int row = 0; row < TIMESTAMP_ROWS; ++row) {
                        const double my = rows_top + row * row_pitch + (row_pitch - msize) * 0.5;
                        draw_marker_tex(marker_tex[b], mx, my, msize);
                    }
                }
                glDisable(GL_TEXTURE_2D);

                // Readable decimal timestamp beneath the block (human verification only: all 10 glyphs
                // were pre-uploaded at startup, so render() is a pure cache hit here).
                char dec[32];
                snprintf(dec, sizeof(dec), "%0*llu", TIMESTAMP_DIGITS, static_cast<unsigned long long>(present_ms));
                for (int i = 0; i < n_digits; ++i) {
                    const int d = dec[i] - '0';
                    const double mx = margin + i * digit_pitch;
                    GlText& t = digit_text[d];
                    t.render(std::string(1, dec[i]));
                    t.draw(mx + digit_pitch * 0.5 - t.width_for_height(gh) * 0.5, text_y, gh);
                }

                glfwSwapBuffers(win);
                glfwPollEvents();
                if (glfwWindowShouldClose(win))
                    running = false;

                // Pace to ~1 kHz so the displayed millisecond advances every ~1 ms.
                std::this_thread::sleep_until(iter_start + std::chrono::milliseconds(1));
            }

            for (GlText& t : digit_text)
                t.destroy();
            for (GlText& t : readout_text)
                t.destroy();
            glDeleteTextures(256, marker_tex.data());
            glfwDestroyWindow(win);
            glfwTerminate();
        }
    };

    Display::Display()
        : p_(new Impl) {
        for (int b = 0; b < 256; ++b)
            p_->grid_side = build_marker_grid(b, p_->byte_grids[b]);
        // Default to a single unlabeled camera so the single-camera benchmark works without calling
        // setCameras(); multi-camera callers replace this via setCameras() before start().
        p_->cams.emplace_back(new CamFeedback());
    }
    Display::~Display() {
        stop();
        delete p_;
    }

    bool Display::start() {
        p_->running = true;
        p_->th = std::thread([this] {
            p_->render_loop();
        });
        while (!p_->init_done.load())
            std::this_thread::sleep_for(std::chrono::milliseconds(5));
        if (!p_->init_ok.load()) {
            if (p_->th.joinable())
                p_->th.join();
            p_->running = false;
            return false;
        }
        return true;
    }

    void Display::setCameras(const std::vector<std::string>& serials) {
        // Called once before start() (render thread not yet reading cams[]). Rebuild one feedback
        // slot per camera, in worker-index order, carrying its serial label.
        p_->cams.clear();
        if (serials.empty()) {
            p_->cams.emplace_back(new CamFeedback()); // never leave it empty
        } else {
            for (const std::string& sn : serials) {
                p_->cams.emplace_back(new CamFeedback());
                p_->cams.back()->label = sn;
            }
        }
    }

    void Display::update(Status status, double latency_ms, int cam_id) {
        if (status != Status::LOCKED)
            return;
        if (cam_id < 0 || static_cast<size_t>(cam_id) >= p_->cams.size())
            return; // unknown camera id: ignore rather than scribble out of bounds
        CamFeedback& c = *p_->cams[cam_id];
        c.latency_us = static_cast<long long>(latency_ms * 1000.0);
        c.last_success_ns
            = std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now().time_since_epoch()).count();
        c.success_count.fetch_add(1);
    }

    void Display::stop() {
        if (p_->running.exchange(false)) {
            if (p_->th.joinable())
                p_->th.join();
        }
    }

} // namespace g2g

#endif // HAVE_G2G
