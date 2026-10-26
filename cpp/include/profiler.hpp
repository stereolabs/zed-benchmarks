//
// profiler.hpp
// ZED Benchmarks: internal, self-contained scoped-duration profiler.
//
//   start()      stores the current std::chrono timestamp for a (section, context).
//   stop()       compares against it and appends the elapsed time (ms) as a sample.
//   addSample()  appends an externally measured duration (ms) as a sample, for spans that a
//                start()/stop() pair cannot bracket (e.g. a latency measured from a camera timestamp).
//   computeStats recomputes the descriptive stats of a section's samples.
//   dumpStats()  recomputes the stats if new samples arrived, then writes them to a file.
//
// All durations are in milliseconds. The dump format is:
//
//   {
//     "profile_stats": {
//       "<context>": {
//         "<section>": { "mean","median","stddev","min","max","p99","count" }
//       }
//     }
//   }
//
// Usage:
//   bench::Profiler::enable();
//   bench::Profiler::start("section");   // context defaults to "default"
//   ... work ...
//   bench::Profiler::stop("section");
//   bench::Profiler::dumpStats("report.profiler.json");
//
// Thread-safety: all state is guarded by a single mutex. The multi-camera benchmark uses one
// thread per camera and a distinct section name per camera, so (section, context) keys never
// collide across threads. A stop() with no matching start() is ignored.
//

#pragma once

#include "json.hpp" // sl::json (classical https://github.com/nlohmann/json)

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <fstream>
#include <iostream>
#include <map>
#include <mutex>
#include <numeric>
#include <string>
#include <vector>

namespace bench {

    class Profiler final {
    public:
        using clock = std::chrono::high_resolution_clock;

        // Arm recording. Until enable() is called, start()/stop() are no-ops.
        static void enable() {
            enabled_.store(true, std::memory_order_relaxed);
        }

        // Store the start timestamp of a (section, context). In-flight starts are keyed globally
        // (not per-thread), so two threads calling start() with the same (name, context) will
        // clobber each other's timestamp. The convention in this codebase is that each camera/thread
        // uses a unique context (camera serial or index), so keys never collide. Do not reuse the
        // same (name, context) pair across concurrent threads.
        static void start(const std::string& name, const std::string& context = "default") {
            if (!enabled_.load(std::memory_order_relaxed))
                return;
            const auto now = clock::now(); // sampled before the lock
            std::lock_guard<std::mutex> lk(mutex_);
            active_[context][name] = now;
        }

        // Append the elapsed time (ms) since the matching start(). A stop() with no matching
        // start() is silently ignored.
        static void stop(const std::string& name, const std::string& context = "default") {
            if (!enabled_.load(std::memory_order_relaxed))
                return;
            const auto now = clock::now(); // sampled before the lock
            std::lock_guard<std::mutex> lk(mutex_);

            auto ctx = active_.find(context);
            if (ctx == active_.end())
                return;
            auto it = ctx->second.find(name);
            if (it == ctx->second.end())
                return;

            const std::chrono::duration<double, std::milli> ms = now - it->second;
            ctx->second.erase(it);
            samples_[context][name].push_back(ms.count());
            dirty_ = true;
        }

        static void addSample(const std::string& name, double ms, const std::string& context = "default") {
            if (!enabled_.load(std::memory_order_relaxed))
                return;
            std::lock_guard<std::mutex> lk(mutex_);
            samples_[context][name].push_back(ms);
            dirty_ = true;
        }

        // Recompute the stats if new samples arrived since the last dump, then write the
        // profile_stats JSON the benchmark report builders consume.
        static void dumpStats(const std::string& filename) {
            sl::json out = sl::json::object();
            {
                std::lock_guard<std::mutex> lk(mutex_);
                if (dirty_) {
                    stats_ = sl::json::object();
                    for (const auto& ctx : samples_) {
                        sl::json sections = sl::json::object();
                        for (const auto& sec : ctx.second)
                            if (!sec.second.empty())
                                sections[sec.first] = computeStats(sec.second);
                        stats_[ctx.first] = sections;
                    }
                    dirty_ = false;
                }
                out["profile_stats"] = stats_;
            }

            std::ofstream f(filename, std::ios::trunc);
            if (f.is_open())
                f << out.dump(4) << "\n";
            else
                std::cerr << "[Profiler] Could not write " << filename << std::endl;
        }

        Profiler() = delete;
        ~Profiler() = delete;
        Profiler(const Profiler&) = delete;
        Profiler& operator=(const Profiler&) = delete;

    private:
        // Descriptive stats over a non-empty sample vector (durations in ms). Keys match exactly
        // what the report builders read back.
        static sl::json computeStats(std::vector<double> v) {
            const std::size_t n = v.size();
            std::sort(v.begin(), v.end());

            const double mean = std::accumulate(v.begin(), v.end(), 0.0) / static_cast<double>(n);
            const double median = (n % 2 == 0) ? (v[n / 2 - 1] + v[n / 2]) / 2.0 : v[n / 2];

            double sq = 0.0;
            for (double x : v)
                sq += (x - mean) * (x - mean);
            const double stddev = std::sqrt(sq / static_cast<double>(n)); // population stddev (/n)

            // p99, nearest-rank: smallest value at or above the 99th percentile position.
            std::size_t idx = static_cast<std::size_t>(std::ceil(0.99 * static_cast<double>(n)));
            if (idx > 0)
                --idx;
            if (idx >= n)
                idx = n - 1;

            return sl::json {
                {"mean",   mean     },
                {"median", median   },
                {"stddev", stddev   },
                {"min",    v.front()},
                {"max",    v.back() },
                {"p99",    v[idx]   },
                {"count",  n        },
            };
        }

        inline static std::atomic<bool> enabled_ {false};
        inline static std::mutex mutex_;

        // In-flight start() timestamps, grouped by context then section.
        inline static std::map<std::string, std::map<std::string, clock::time_point>> active_;

        // Completed durations (ms), grouped by context then section. std::map keeps the JSON
        // output stably ordered.
        inline static std::map<std::string, std::map<std::string, std::vector<double>>> samples_;

        // Cached profile_stats, recomputed by dumpStats() only when new samples have arrived.
        inline static sl::json stats_ = sl::json::object();
        inline static bool dirty_ = false;
    };

} // namespace bench
