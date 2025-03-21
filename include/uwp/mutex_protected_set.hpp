#pragma once

#include <mutex>
#include <set>

namespace uwp {

/// @brief A thread-safe set implementation.
/// @tparam T The type of the elements in the set.
/// @tparam Compare The type of the comparison function.
/// @tparam Allocator The type of the allocator.
template <typename T, typename Compare = std::less<T>,
          typename Allocator = std::allocator<T>>
class MutexProtectedSet : public std::set<T, Compare, Allocator> {
 private:
  /// @brief The mutex to protect the set.
  mutable std::mutex mutex_;

 public:
  using std::set<T, Compare, Allocator>::set;
  using typename std::set<T, Compare, Allocator>::iterator;

  /// @brief Checks if the set contains a value.
  /// @param value The value to check.
  /// @return True if the set contains the value, false otherwise.
  auto contains(const T& value) const -> bool {
    return std::set<T, Compare, Allocator>::contains(value);
  }

  /// @brief Inserts a value into the set.
  /// @param value The value to insert.
  /// @return A pair containing an iterator to the inserted value and a boolean
  /// indicating if the value was inserted.
  auto insert(const T& value) -> std::pair<iterator, bool> {
    std::lock_guard<std::mutex> lock(mutex_);
    return std::set<T, Compare, Allocator>::insert(value);
  }

  /// @brief Inserts a value into the set.
  /// @param value The value to insert.
  /// @return A pair containing an iterator to the inserted value and a boolean
  /// indicating if the value was inserted.
  auto insert(T&& value) -> std::pair<iterator, bool> {
    std::lock_guard<std::mutex> lock(mutex_);
    return std::set<T, Compare, Allocator>::insert(std::move(value));
  }

  /// @brief Inserts a new element into the container constructed in-place with
  /// the given args, if there is no element with the key in the container.
  /// @tparam Args The types of the arguments.
  /// @param args The arguments to forward to the constructor of the element.
  /// @return A pair containing an iterator to the inserted value and a boolean
  /// indicating if the value was inserted.
  template <typename... Args>
  auto emplace(Args&&... args) -> std::pair<iterator, bool> {
    std::lock_guard<std::mutex> lock(mutex_);
    return std::set<T, Compare, Allocator>::emplace(
        std::forward<Args>(args)...);
  }
};

}  // namespace uwp
