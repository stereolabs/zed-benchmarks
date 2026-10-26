/**
 * glass_to_glass.hpp: Glass-to-glass (G2G) latency support for the ZED benchmarks.
 *
 * G2G measures full end-to-end latency (display + optics + sensor + SDK) by closing the
 * loop through a physical screen. The display renders the host CLOCK_REALTIME time, in
 * milliseconds, as a binary, CRC-checked block of AprilTag markers: the value is packed into
 * 6 big-endian payload bytes followed by a 2-byte CRC-16/CCITT, and each byte is shown as a
 * marker whose id is the byte value (0..255). Each byte is drawn as several identical rows, so
 * the markers stack into 8 vertical columns. The real decimal time is drawn beneath the block
 * for human verification. The camera films the screen; the decoder groups the markers into
 * columns (left-to-right by image x), reassembles the bytes, and accepts the frame only when
 * the CRC matches, then compares the millisecond timestamp against the SDK IMAGE capture time.
 * See cpp/README.md ("Glass-to-glass latency").
 *
 * AprilTag markers make the read robust and self-timed: markers are self-registering (own
 * homography + orientation), use local adaptive thresholding, carry the byte value directly
 * with built-in bit-error correction, and survive perspective, rotation, blur and low contrast.
 * Each byte is shown as a column of identical markers, so a camera frame whose rolling-shutter
 * readout straddles a timestamp change shows the old value in the rows above the tear and the
 * new value in the rows below; the changed byte's column then disagrees (and the CRC fails as a
 * backstop), so the decoder drops the frame rather than reconstructing a mixed-time value. A
 * decode is accepted only when every marker in each column agrees and the CRC checks out.
 *
 * This whole header is compiled only when HAVE_G2G is defined (GLFW + OpenGL + OpenCV
 * with the objdetect/aruco module were found at configure time). The benchmarks guard
 * every use with #ifdef HAVE_G2G, so they still build with only ZED + CUDA.
 *
 * The marker scheme (dictionary + binary/CRC layout) must stay in sync with
 * python/glass_to_glass.py: update both together.
 */
#pragma once

#ifdef HAVE_G2G

    #include <cstddef>
    #include <cstdint>
    #include <string>
    #include <vector>

// Forward declaration: keep OpenCV/GLFW/ZED includes out of this header (pimpl).
namespace sl {
    class Mat;
}

namespace g2g {

    enum class Status {
        NO_SIGNAL,
        LOCKED
    };

    // Host CLOCK_REALTIME, nanoseconds (the clock the display renders and the decoder reads back).
    uint64_t realtime_now_ns();

    // ── Decoder (OpenCV aruco, main module only) ─────────────────────────────────
    struct DecodeResult {
        Status status = Status::NO_SIGNAL;
        uint64_t display_ns = 0; // present time reconstructed from the on-screen digits (LOCKED only)
    };

    class Decoder {
    public:
        Decoder();
        ~Decoder();
        Decoder(const Decoder&) = delete;
        Decoder& operator=(const Decoder&) = delete;

        // image: SDK LEFT/CPU view (4-channel BGRA u8). Returns status + reconstructed present ns.
        // reference_now_ns: the "now" the decoded screen time is sanity-checked against (a decode far
        // from it is treated as a misread). 0 (default) uses the host CLOCK_REALTIME: correct for
        // live capture. On SVO playback the screen shows the recording time, not the host's current
        // time, so callers must pass the frame's reconstructed live "now" instead.
        DecodeResult decode(const sl::Mat& image, uint64_t reference_now_ns = 0);

        // Number of decodes rejected by the plausibility window (reconstructed time too far from the
        // reference "now"). A non-zero count on live capture usually means the clock offset is wrong,
        // which otherwise shows up only as a mysteriously low detection rate.
        unsigned long long implausibleTimeCount() const;

    private:
        struct Impl;
        Impl* p_;
    };

    // ── Display (GLFW + OpenGL) ──────────────────────────────────────────────────
    // Owns a fullscreen window + GL context on its own thread for the whole run, rendering the
    // current millisecond timestamp as several identical rows of per-digit AprilTag markers.
    class Display {
    public:
        Display();
        ~Display();
        Display(const Display&) = delete;
        Display& operator=(const Display&) = delete;

        bool start(); // spawn render thread; false on failure

        // Declare the cameras whose feedback this display will show, one label (serial) per camera,
        // in worker-index order. Drives the per-camera readout rows + status. Call BEFORE start()
        // (the render thread snapshots the count). If never called, the display defaults to a single
        // unlabeled camera (cam_id 0): the single-camera benchmark relies on this.
        void setCameras(const std::vector<std::string>& serials);

        // Feedback from the decode side, attributed to camera `cam_id` (its worker index; the index
        // into setCameras()). Out-of-range ids are ignored. Defaults to 0 for the single-camera case.
        void update(Status status, double latency_ms, int cam_id = 0);
        void stop();

    private:
        struct Impl;
        Impl* p_;
    };

    // ── Async decoder ─────────────────────────────────────────────────────────────
    // Runs the (expensive) Decoder on its own worker thread so it never inflates the grab loop
    // period. The grab thread submit()s a deep copy of each frame plus the sampled "now"
    // timestamp into a SINGLE-SLOT mailbox; if the worker is still busy with the previous frame,
    // the pending one is overwritten: dropped and counted, never an unbounded queue. The worker
    // owns the Decoder, computes the G2G latency on a LOCKED decode, accumulates it into its own
    // vector (no mutex on the hot path), and feeds the Display its per-frame feedback.
    class AsyncDecoder {
    public:
        AsyncDecoder();
        ~AsyncDecoder();
        AsyncDecoder(const AsyncDecoder&) = delete;
        AsyncDecoder& operator=(const AsyncDecoder&) = delete;

        // Spawn the worker. `display` (may be null) receives per-frame feedback attributed to
        // `cam_id` (this camera's worker index: see Display::setCameras); `profile_context` is a
        // scoped-profiler context (camera serial) reserved for future per-section timing (the
        // worker does not currently record any profiler section); `latency_reserve`
        // pre-reserves the latency vector (0 = none).
        void start(Display* display, const std::string& profile_context, size_t latency_reserve = 0, int cam_id = 0);

        // Grab-thread entry point. Deep-copies `image` (the SDK reuses it on the next grab) and
        // stores it with `current_ns` (the now-timestamp on the display clock) into the single-slot
        // mailbox, overwriting and counting any frame the worker has not yet taken. Non-blocking.
        void submit(const sl::Mat& image, long long current_ns);

        // Stop + join the worker (idempotent). Logs how many frames were dropped.
        void stop();

        // Number of frames dropped because the worker was busy. Final after stop().
        unsigned long long dropped() const;

        // Move out the accumulated per-frame G2G latencies (ms). Call after stop().
        std::vector<double> takeLatencies();

    private:
        struct Impl;
        Impl* p_;
    };

} // namespace g2g

#endif // HAVE_G2G
