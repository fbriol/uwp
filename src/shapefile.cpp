#include "uwp/shapefile.hpp"

#include <shapefil.h>

namespace uwp {

auto Shapefile::build_rtree_index() -> void {
  std::vector<PolygonIndex> ptr;
  ptr.reserve(polygons_->size());
  std::transform(polygons_->begin(), polygons_->end(), std::back_inserter(ptr),
                 [](std::unique_ptr<Polygon> &p) {
                   return std::make_pair(
                       boost::geometry::return_envelope<Box>(*p), p.get());
                 });
  rtree_.reset(new RTree(ptr));
}

// The SHPObjectPtr type is a unique_ptr for SHPObject with a custom
// deleter that calls SHPDestroyObject.
using SHPObjectPtr = std::unique_ptr<SHPObject, decltype(&SHPDestroyObject)>;

// Creates a new shapefile with the specified arguments.
//
// This function is a wrapper around the SHPCreateLL function from the
// shapelib library. When the shapefile is created, the function checks if the
// handle is null and throws a runtime error if it is.
template <typename... Args>
auto shp_create(Args... args) {
  SAHooks sHooks;
  SASetupDefaultHooks(&sHooks);
  sHooks.Error = [](const char * /*message*/) {};
  auto handle = SHPCreateLL(args..., &sHooks);
  if (handle == nullptr) {
    throw std::runtime_error("Failed to create shapefile");
  }
  SHPClose(handle);
}

// Opens an existing shapefile with the specified arguments.
//
// This function is a wrapper around the SHPOpenLL function from the shapelib
// library. When the shapefile is opened, the function checks if the handle is
// null and throws a runtime error if it is. The result is returned as a
// unique_ptr with a custom deleter that calls SHPClose.
template <typename... Args>
auto shp_open(Args... args) {
  SAHooks sHooks;
  SASetupDefaultHooks(&sHooks);
  sHooks.Error = [](const char * /*message*/) {};
  return std::unique_ptr<SHPInfo, decltype(&SHPClose)>(
      SHPOpenLL(args..., &sHooks), SHPClose);
}

// Creates a new dbf file with the specified arguments.
//
// This function is a wrapper around the DBFCreate function from the shapelib
// library. When the dbf file is created, the function checks if the handle is
// null and throws a runtime error if it is.
template <typename... Args>
auto dbf_create(Args... args) {
  auto handle = DBFCreate(args...);
  if (handle == nullptr) {
    throw std::runtime_error("Failed to create dbf file");
  }
  DBFClose(handle);
}

// Opens an existing dbf file with the specified arguments.
//
// This function is a wrapper around the DBFOpenLL function from the shapelib
// library. When the dbf file is opened, the function checks if the handle is
// null and throws a runtime error if it is. The result is returned as a
// unique_ptr with a custom deleter that calls DBFClose.
template <typename... Args>
auto dbf_open(Args... args) {
  SAHooks sHooks;
  SASetupDefaultHooks(&sHooks);
  sHooks.Error = [](const char * /*message*/) {};
  return std::unique_ptr<DBFInfo, decltype(&DBFClose)>(
      DBFOpenLL(args..., &sHooks), DBFClose);
}

// Creates a new shapefile object with the specified arguments.
//
// This function is a wrapper around the SHPCreateObject function from the
// shapelib library. When the shapefile object is created, the function checks
// if the handle is null and throws a runtime error if it is. The result is
// returned as a unique_ptr with a custom deleter that calls SHPDestroyObject.
template <typename... Args>
auto shp_create_object(Args... args) {
  auto handle = SHPCreateObject(args...);
  if (handle == nullptr) {
    throw std::runtime_error("Failed to create shapefile object");
  }
  return SHPObjectPtr(handle, SHPDestroyObject);
}

// Reads a shapefile object with the specified arguments.
//
// This function is a wrapper around the SHPReadObject function from the
// shapelib library. When the shapefile object is read, the function checks if
// the handle is null and throws a runtime error if it is. The result is
// returned as a unique_ptr with a custom deleter that calls SHPDestroyObject.
template <typename... Args>
auto shp_read_object(Args... args) {
  auto handle = SHPReadObject(args...);
  if (handle == nullptr) {
    throw std::runtime_error("Failed to read shapefile object");
  }
  return SHPObjectPtr(handle, SHPDestroyObject);
}

// Extracts the vertices and constructs the polygon.
//
// @param[in] shape The shape to read.
// @return The polygon constructed from the shape.
auto read_polygon(SHPObjectPtr &shape) -> Polygon {
  const auto *x = shape->padfX;
  const auto *y = shape->padfY;

  auto polygon = Polygon();
  auto &inners = polygon.inners();
  inners.reserve(shape->nParts - 1);

  // shapelib is a C library, so we need to use a raw pointer arithmetic here
  // NOLINTBEGIN(cppcoreguidelines-pro-bounds-pointer-arithmetic)
  for (int lx = 0; lx < shape->nParts; ++lx) {
    int end = (lx == shape->nParts - 1) ? shape->nVertices
                                        : shape->panPartStart[lx + 1];
    if (lx == 0) {
      polygon.outer().reserve(end - shape->panPartStart[lx]);
      for (int jx = shape->panPartStart[lx]; jx < end; ++jx) {
        boost::geometry::append(polygon, Point(*x++, *y++));
      }
    } else {
      auto inner = Ring();
      inner.reserve(end - shape->panPartStart[lx]);
      for (int jx = shape->panPartStart[lx]; jx < end; ++jx) {
        boost::geometry::append(inner, Point(*x++, *y++));
      }
      inners.emplace_back(std::move(inner));
    }
  }
  // NOLINTEND(cppcoreguidelines-pro-bounds-pointer-arithmetic)

  return polygon;
}

// Manages the bounding box check and adds the polygon to the list if
// it fits the criteria.
//
// @param[in] polygon The polygon to handle.
// @param[in] bbox The optional bounding box of the shapefile.
void handle_polygon(const Polygon &polygon, const std::optional<Box> &bbox,
                    std::shared_ptr<Shapefile::PolygonList> &polygon_list) {
  if (bbox.has_value()) {
    auto intersection = std::deque<Polygon>();
    boost::geometry::intersection(polygon, bbox.value(), intersection);
    if (!intersection.empty()) {
      for (auto &&item : intersection) {
        polygon_list->emplace_back(std::make_unique<Polygon>(std::move(item)));
      }
    }
  } else {
    polygon_list->emplace_back(std::make_unique<Polygon>(polygon));
  }
}

// Handles reading the shape and determining if it should be added to
// the polygon list.
//
// @param[in] shape The shape to process.
// @param[in] bbox The optional bounding box of the shapefile.
inline void process_shape(
    SHPObjectPtr &shape, const std::optional<Box> &bbox,
    std::shared_ptr<Shapefile::PolygonList> &polygon_list) {
  auto polygon = read_polygon(shape);
  handle_polygon(polygon, bbox, polygon_list);
}

auto Shapefile::load(const std::string &filename,
                     const std::optional<Box> &bbox) -> void {
  auto handle = shp_open(filename.c_str(), "rb");
  if (handle == nullptr) {
    throw std::runtime_error("Failed to open shapefile: '" + filename + "'");
  }

  int shape_types = 0;
  int entities = 0;
  std::array<double, 4> min_bound{};
  std::array<double, 4> max_bound{};

  polygons_ = std::make_shared<PolygonList>();

  SHPGetInfo(handle.get(), &entities, &shape_types, min_bound.data(),
             max_bound.data());

  // shapelib is a C library, so we need to use a raw pointer arithmetic here
  // NOLINTBEGIN(cppcoreguidelines-pro-bounds-pointer-arithmetic)
  for (int ix = 0; ix < entities; ++ix) {
    auto shape = shp_read_object(handle.get(), ix);
    if (shape->nParts > 0 && shape->panPartStart[0] != 0) {
      throw std::runtime_error("unable to read shape " + std::to_string(ix));
    }
    if (shape->nSHPType == SHPT_POLYGON && shape->nVertices != 0) {
      process_shape(shape, bbox, polygons_);
    }
  }
  // NOLINTEND(cppcoreguidelines-pro-bounds-pointer-arithmetic)
}

auto Shapefile::save(const std::string &filename) const -> void {
  shp_create(filename.c_str(), SHPT_POLYGON);
  dbf_create(filename.c_str());
  auto handle = shp_open(filename.c_str(), "rb+");
  if (handle == nullptr) {
    throw std::runtime_error("Failed to open shapefile: '" + filename + "'");
  }
  auto dbf_handle = dbf_open(filename.c_str(), "rb+");
  if (dbf_handle == nullptr) {
    throw std::runtime_error("Failed to open dbf file: '" + filename + "'");
  }

  if (DBFAddField(dbf_handle.get(), "FID", FTDouble, 11, 0) == -1) {
    throw std::runtime_error("Failed to add field to shapefile");
  }

  for (const auto &polygon : *polygons_) {
    const auto &outer = polygon->outer();
    const auto &inners = polygon->inners();
    auto x = std::vector<double>(outer.size());
    auto y = std::vector<double>(outer.size());
    auto pan_starts = std::vector<int>();
    auto pan_types = std::vector<int>();
    pan_starts.reserve(inners.size() + 1);
    pan_starts.push_back(0);
    pan_types.reserve(inners.size() + 1);
    pan_types.push_back(SHPP_OUTERRING);
    if (!inners.empty()) {
      pan_starts.push_back(static_cast<int>(outer.size()));
    }
    for (size_t ix = 0; ix < outer.size(); ++ix) {
      x[ix] = outer[ix].get<0>();
      y[ix] = outer[ix].get<1>();
    }
    size_t current_pos = outer.size();
    for (size_t ix = 0; ix < inners.size(); ++ix) {
      pan_types.push_back(SHPP_INNERRING);
      if (ix > 0) {
        pan_starts.push_back(static_cast<int>(current_pos));
      }

      const auto &inner = inners[ix];
      x.resize(current_pos + inner.size());
      y.resize(current_pos + inner.size());
      for (size_t jx = 0; jx < inner.size(); ++jx) {
        x[current_pos + jx] = inner[jx].get<0>();
        y[current_pos + jx] = inner[jx].get<1>();
      }
      current_pos += inner.size();
    }
    auto obj = shp_create_object(
        SHPT_POLYGON, -1, static_cast<int>(pan_starts.size()),
        pan_starts.data(), pan_types.data(), static_cast<int>(x.size()),
        x.data(), y.data(), nullptr, nullptr);
    auto shape_id = SHPWriteObject(handle.get(), -1, obj.get());
    if (shape_id == -1) {
      throw std::runtime_error("Failed to write shapefile object");
    }
    if (DBFWriteDoubleAttribute(dbf_handle.get(), shape_id, 0,
                                static_cast<double>(shape_id)) == -1) {
      throw std::runtime_error("Failed to write shapefile object");
    }
  }
}

}  // namespace uwp
