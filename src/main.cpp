#include <iostream>
#include <string>
#include <vector>

#include "uwp/shapefile.hpp"
#include "uwp/update.hpp"

int main(int argc, char* argv[]) {
  if (argc < 2) {
    std::cerr << "Usage: " << argv[0]
              << " water_polygon [-o water_polygon_output] region_polygon1 "
                 "[region_polygon2 ...]"
              << std::endl;
    return 1;
  }

  // First parameter is the water polygon file
  std::string water_file = argv[1];

  // Parse the arguments to find -o option and region files
  std::string output_file;
  std::vector<std::string> region_files;
  bool output_specified = false;

  for (int i = 2; i < argc; ++i) {
    std::string arg = argv[i];
    if ((arg == "-o" || arg == "-O") && i + 1 < argc) {
      output_file = argv[++i];
      output_specified = true;
    } else if (arg.length() > 1 && arg[0] == '-') {
      std::cerr << "Error: Unsupported option " << arg << std::endl;
      return 1;
    } else {
      region_files.push_back(arg);
    }
  }

  // Check that we have at least one region file
  if (region_files.empty()) {
    std::cerr << "Error: No region polygon files specified" << std::endl;
    return 1;
  }

  // Set default output name if not specified
  if (!output_specified) {
    output_file = water_file + "_updated.shp";
  }

  // Load the water shapefile
  auto water_shp = uwp::Shapefile(water_file);
  water_shp.build_rtree_index();

  // Process each region polygon
  for (const auto& region_file : region_files) {
    std::cout << "Processing region file: " << region_file << std::endl;

    auto area_shp = uwp::Shapefile(region_file);
    area_shp.build_rtree_index();

    auto overlap = uwp::select_overlap(*(water_shp.polygons()), area_shp);
    uwp::merge_overlapping(water_shp, overlap);
  }

  // Save the final result
  std::cout << "Saving result to: " << output_file << std::endl;
  water_shp.save(output_file);

  return 0;
}
