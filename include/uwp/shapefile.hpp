#pragma once

#include <list>
#include <memory>
#include <optional>
#include <string>
#include <utility>

#include "uwp/geometry.hpp"

namespace uwp {

class Shapefile {
 public:
  /// @brief An unique pointer to a polygon.
  using PolygonPtr = std::unique_ptr<Polygon>;

  /// @brief List of polygons in the shapefile.
  using PolygonList = std::vector<PolygonPtr>;

  /// @brief Pair of a bounding box and a pointer to a polygon.
  using PolygonIndex = std::pair<Box, const Polygon *>;

  /// @brief RTree index for the envelope of the polygons.
  using RTree =
      boost::geometry::index::rtree<PolygonIndex,
                                    boost::geometry::index::rstar<16>>;

  /// @brief Constructs a Shapefile object.
  Shapefile() = default;

  /// @brief Destructor for the Shapefile object.
  virtual ~Shapefile() = default;

  /// @brief Constructs a Shapefile object with the specified filename and
  /// bounding box.
  ///
  /// @param[in] filename The filename of the shapefile.
  /// @param[in] bbox The optional bounding box of the area to load. If not
  /// provided, the entire shapefile will be loaded.  inline Shapefile(
  Shapefile(const std::string &filename,
            const std::optional<Box> &bbox = std::nullopt) {
    load(filename, bbox);
  }

  /// @brief Saves the shapefile to the specified filename.
  ///
  /// @param[in] filename The filename to save the shapefile to.
  auto save(const std::string &filename) const -> void;

  /// @brief Loads the shapefile from the specified filename and bounding box.
  ///
  /// @param[in] filename The filename of the shapefile to load.
  /// @param[in] bbox The optional bounding box of the shapefile.
  auto load(const std::string &filename, const std::optional<Box> &bbox)
      -> void;

  /// Check if the shapefile is empty.
  /// @return true if the shapefile is empty, false otherwise.
  inline auto is_empty() const noexcept -> bool { return polygons_->empty(); }

  /// Get the number of polygons in the shapefile.
  /// @return The number of polygons.
  inline auto size() const noexcept -> size_t { return polygons_->size(); }

  /// Check if the RTree index is built.
  /// @return true if the RTree index is built, false otherwise.
  inline auto is_rtree_built() const noexcept -> bool {
    return rtree_ != nullptr;
  }

  /// Get the list of polygons in the shapefile.
  /// @return A shared pointer to the list of polygons.
  constexpr auto polygons() const noexcept
      -> const std::shared_ptr<PolygonList> & {
    return polygons_;
  }

  constexpr auto polygons() noexcept -> std::shared_ptr<PolygonList> & {
    return polygons_;
  }

  /// Get the RTree index.
  /// @return A shared pointer to the RTree index.
  constexpr auto rtree() const noexcept -> const std::shared_ptr<RTree> & {
    return rtree_;
  }

  /// @brief Build the RTree index for the shapefile.
  auto build_rtree_index() -> void;

  /// @brief Append an other shapefile to the shapefile.
  /// @param[in] other The other shapefile to append.
  inline auto append(const Shapefile &other) -> void {
    append(*other.polygons_);
    build_rtree_index();
  }

  inline auto append(const Polygon &polygon) -> void {
    polygons_->emplace_back(std::make_unique<Polygon>(polygon));
  }

  inline auto append(Polygon &&polygon) -> void {
    polygons_->emplace_back(std::make_unique<Polygon>(std::move(polygon)));
  }

  /// @brief Append a list of polygons to the shapefile.
  /// @param[in] polygons The list of polygons to append.
  inline auto append(const PolygonList &polygons) -> void {
    polygons_->reserve(polygons_->size() + polygons.size());
    for (auto &polygon : polygons) {
      append(*polygon);
    }
  }

 protected:
  /// List of polygons loaded from the shapefile.
  std::shared_ptr<PolygonList> polygons_{std::make_shared<PolygonList>()};

  /// RTree index for the envelope of the polygons.
  std::shared_ptr<RTree> rtree_{};

 private:
};

}  // namespace uwp
