#pragma once

#include <chrono>
#include <ctime>
#include <iomanip>
#include <iostream>
#include <mutex>
#include <sstream>
#include <string>

namespace uwp {

/// Minimal thread-safe logger matching the format used by the Python
/// orchestrator (`update_water_polygons.py`):
///
///   2026-05-24 12:34:56,789 - INFO - message
///
/// so a combined log of the Python wrapper + the C++ binary reads as
/// one continuous timeline. Levels: INFO, WARN, ERROR. There is no
/// configuration knob — keep it dead simple; if we ever need filtering
/// the env-var hook can be added here.
///
/// Output goes to stderr (like Python's default) so it stays separate
/// from any structured data the program might one day write to stdout,
/// and is unbuffered enough that an SSH session sees progress live.
class Logger {
 public:
  enum class Level : uint8_t { kInfo, kWarn, kError };

  /// Log a single line. Thread-safe: a mutex serialises writes so
  /// concurrent workers don't interleave characters mid-line.
  static auto log(Level level, const std::string &message) -> void {
    const auto now = std::chrono::system_clock::now();
    const auto t = std::chrono::system_clock::to_time_t(now);
    const auto ms = std::chrono::duration_cast<std::chrono::milliseconds>(
                        now.time_since_epoch())
                        .count() %
                    1000;

    std::tm tm_buf{};
#ifdef _WIN32
    localtime_s(&tm_buf, &t);
#else
    localtime_r(&t, &tm_buf);
#endif

    char ts[32];
    std::strftime(ts, sizeof(ts), "%Y-%m-%d %H:%M:%S", &tm_buf);

    std::lock_guard<std::mutex> lock(mutex());
    std::cerr << ts << ',' << std::setw(3) << std::setfill('0') << ms << " - "
              << level_name(level) << " - " << message << std::endl;
  }

 private:
  static auto mutex() -> std::mutex & {
    static std::mutex m;
    return m;
  }

  static auto level_name(Level level) -> const char * {
    switch (level) {
      case Level::kInfo:
        return "INFO";
      case Level::kWarn:
        return "WARN";
      case Level::kError:
        return "ERROR";
    }
    return "INFO";
  }
};

/// Stream-style logging helper. Use as:
///   LOG_INFO() << "Loaded " << n << " polygons";
/// On destruction the accumulated text is shipped to the logger.
class LogLine {
 public:
  explicit LogLine(Logger::Level level) : level_(level) {}
  LogLine(const LogLine &) = delete;
  auto operator=(const LogLine &) -> LogLine & = delete;
  LogLine(LogLine &&) = delete;
  auto operator=(LogLine &&) -> LogLine & = delete;

  ~LogLine() { Logger::log(level_, stream_.str()); }

  template <typename T>
  auto operator<<(const T &value) -> LogLine & {
    stream_ << value;
    return *this;
  }

 private:
  Logger::Level level_;
  std::ostringstream stream_;
};

/// Scoped timer: logs an INFO line with the wall-clock duration when
/// it goes out of scope. Use to bracket a phase:
///
///   {
///     ScopedTimer t("merge_overlapping");
///     merge_overlapping(...);
///   }  // → "merge_overlapping done in 12.34 s"
class ScopedTimer {
 public:
  explicit ScopedTimer(std::string label)
      : label_(std::move(label)), start_(std::chrono::steady_clock::now()) {}
  ScopedTimer(const ScopedTimer &) = delete;
  auto operator=(const ScopedTimer &) -> ScopedTimer & = delete;
  ScopedTimer(ScopedTimer &&) = delete;
  auto operator=(ScopedTimer &&) -> ScopedTimer & = delete;

  ~ScopedTimer() {
    const auto elapsed =
        std::chrono::duration<double>(std::chrono::steady_clock::now() - start_)
            .count();
    LogLine(Logger::Level::kInfo) << label_ << " done in " << std::fixed
                                  << std::setprecision(2) << elapsed << " s";
  }

 private:
  std::string label_;
  std::chrono::steady_clock::time_point start_;
};

}  // namespace uwp

#define LOG_INFO() ::uwp::LogLine(::uwp::Logger::Level::kInfo)
#define LOG_WARN() ::uwp::LogLine(::uwp::Logger::Level::kWarn)
#define LOG_ERROR() ::uwp::LogLine(::uwp::Logger::Level::kError)
