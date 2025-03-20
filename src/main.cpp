#include <iostream>

#include "uwp/shapefile.hpp"
#include "uwp/update.hpp"

int main(int argc, char *argv[]) {
  if (argc != 4) {
    std::cerr << "Usage: " << argv[0]
              << " water_polygon region_polygon water_polygon_output"
              << std::endl;
    return 1;
  }

  auto water_shp = uwp::Shapefile(argv[1]);
  auto area_shp = uwp::Shapefile(argv[2]);

  water_shp.build_rtree_index();
  area_shp.build_rtree_index();

  auto overlap = uwp::select_overlap(*(water_shp.polygons()), area_shp);
  uwp::merge_overlapping(water_shp, overlap);
  water_shp.save(argv[3]);

  return 0;
}
