#pragma once

#include <algorithm>
#include <exception>
#include <thread>
#include <vector>

namespace uwp {

/// Automates the cutting of vectors to be processed in thread.
///
/// @param[in] worker Lambda function called in each thread launched. Lambda
/// function must have the following signature:
/// @code
/// void worker(size_t start, size_t stop);
/// @endcode
/// @param[in] size Size of all vectors to be processed
/// @param[in] num_threads The number of threads to use for the computation. If
/// 0 all CPUs are used. If 1 is given, no parallel computing code is used at
/// all, which is useful for debugging.
/// @param[in] min_size The minimum size of the vector to be processed in
/// parallel. If the size is less than this value, the vector is processed
/// sequentially. Default is 1.
/// @tparam Lambda Lambda function
template <typename Lambda>
void parallel_for(Lambda worker, size_t size, size_t num_threads,
                  size_t min_size = 1) {
  if (num_threads == 0) {
    num_threads = std::thread::hardware_concurrency();
  }

  // If only one thread is requested or size is small, execute directly
  if (num_threads == 1 || size <= min_size) {
    worker(0, size);
    return;
  }

  // List of threads responsible for parallelizing the calculation
  std::vector<std::thread> threads;
  std::exception_ptr exception = nullptr;

  // Access index to the vectors required for calculation
  size_t shift = size / num_threads;
  size_t remainder = size % num_threads;

  threads.reserve(num_threads);

  size_t start = 0;

  // Launch threads
  for (size_t ix = 0; ix < num_threads; ++ix) {
    size_t end = start + shift + (ix < remainder ? 1 : 0);

    // Capture worker by value or move if necessary.
    threads.emplace_back([worker, start, end, &exception]() mutable {
      try {
        worker(start, end);
      } catch (...) {
        // Capture the last exception encountered and store it.
        // This avoids handling concurrency issues between threads.
        // The exception will be rethrown after all threads have completed.
        exception = std::current_exception();
      }
    });

    start = end;
  }

  // Join threads
  for (auto &thread : threads) {
    if (thread.joinable()) {
      thread.join();
    }
  }

  // Rethrow the last exception caught
  if (exception) {
    std::rethrow_exception(exception);
  }
}

}  // namespace uwp
