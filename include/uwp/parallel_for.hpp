#pragma once

#include <algorithm>
#include <atomic>
#include <exception>
#include <mutex>
#include <thread>
#include <vector>

namespace uwp {

/// Automates the cutting of vectors to be processed in thread.
///
/// Uses **dynamic scheduling**: a shared atomic counter is initialized to 0
/// and each thread atomically fetches the next chunk to process until the
/// range is exhausted. This balances load when per-element cost varies
/// widely (typical for geometric work — some water polygons have many more
/// overlapping candidates than others), avoiding the tail effect of static
/// partitioning where a few slow chunks leave the rest of the threads idle
/// at the end.
///
/// The chunk size targets roughly 8 chunks per thread on average, which
/// gives fine-grained load balancing while keeping the atomic-counter
/// contention very low.
///
/// @param[in] worker Lambda function called for each chunk. Must have the
/// signature:
/// @code
/// void worker(size_t start, size_t stop);
/// @endcode
/// May be invoked multiple times per thread (once per chunk). The worker
/// must therefore be safe to call concurrently with other invocations on
/// disjoint `[start, stop)` ranges — which was already required by the
/// static-partitioning implementation.
/// @param[in] size Size of all vectors to be processed.
/// @param[in] num_threads Number of threads to use. 0 means
/// `std::thread::hardware_concurrency()`. 1 disables parallelism entirely
/// (useful for debugging).
/// @param[in] min_size If `size <= min_size`, the range is processed
/// sequentially on the calling thread. Default is 1.
/// @tparam Lambda Lambda function type.
template <typename Lambda>
void parallel_for(Lambda worker, size_t size, size_t num_threads,
                  size_t min_size = 1) {
  if (num_threads == 0) {
    num_threads = std::thread::hardware_concurrency();
    if (num_threads == 0) {
      num_threads = 1;
    }
  }

  // If only one thread is requested or size is small, execute directly.
  if (num_threads == 1 || size <= min_size) {
    worker(0, size);
    return;
  }

  // No point launching more threads than work items.
  num_threads = std::min<size_t>(num_threads, size);

  // Target ~8 chunks per thread for good load balancing without excessive
  // atomic contention. Clamp to at least 1.
  const size_t target_chunks = num_threads * 8;
  const size_t chunk_size = std::max<size_t>(1, size / target_chunks);

  std::atomic<size_t> next{0};
  std::exception_ptr exception = nullptr;
  std::mutex exception_mutex;
  std::vector<std::thread> threads;
  threads.reserve(num_threads);

  auto runner = [&]() {
    try {
      while (true) {
        const size_t start =
            next.fetch_add(chunk_size, std::memory_order_relaxed);
        if (start >= size) {
          return;
        }
        const size_t end = std::min(start + chunk_size, size);
        worker(start, end);
      }
    } catch (...) {
      // Capture the first exception; ignore subsequent ones (we'll rethrow
      // after all threads join).
      std::lock_guard<std::mutex> lock(exception_mutex);
      if (!exception) {
        exception = std::current_exception();
      }
    }
  };

  for (size_t ix = 0; ix < num_threads; ++ix) {
    threads.emplace_back(runner);
  }

  for (auto &thread : threads) {
    if (thread.joinable()) {
      thread.join();
    }
  }

  if (exception) {
    std::rethrow_exception(exception);
  }
}

}  // namespace uwp
