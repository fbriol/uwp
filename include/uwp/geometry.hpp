#pragma once

#include <boost/geometry.hpp>

namespace bg = boost::geometry;

namespace uwp {

using Point = bg::model::point<double, 2, bg::cs::cartesian>;

using Box = bg::model::box<Point>;

using Polygon = bg::model::polygon<Point>;

using MultiPolygon = bg::model::multi_polygon<Polygon>;

using Ring = bg::model::ring<Point>;

using Segment = bg::model::segment<Point>;

using Line = bg::model::linestring<Point>;

}  // namespace uwp
