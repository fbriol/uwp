#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

#include "uwp/shapefile.hpp"
#include "uwp/update.hpp"

int main(int argc, char* argv[]) {
  if (argc < 2) {
    std::cerr << "Usage: " << argv[0]
              << " water_polygon [-o water_polygon_output]"
                 " [--max-inland-km N]"
                 " region_polygon1 [region_polygon2 ...]"
              << std::endl;
    return 1;
  }

  // First parameter is the water polygon file
  std::string water_file = argv[1];

  // Parse the arguments to find -o option and region files
  std::string output_file;
  std::vector<std::string> region_files;
  bool output_specified = false;

  // Inland-distance cap: regional water polygons extending more than this many
  // kilometres beyond the matched coastal water polygon's envelope are clipped
  // before merging. 0 disables the cap (default — preserves the original
  // behaviour). Useful to prevent river polygons modelled as one giant feature
  // from dragging the coastline hundreds of km inland.
  double max_inland_km = 0.0;

  for (int i = 2; i < argc; ++i) {
    std::string arg = argv[i];
    if ((arg == "-o" || arg == "-O") && i + 1 < argc) {
      output_file = argv[++i];
      output_specified = true;
    } else if (arg == "--max-inland-km" && i + 1 < argc) {
      try {
        max_inland_km = std::stod(argv[++i]);
      } catch (const std::exception& exc) {
        std::cerr << "Error: invalid --max-inland-km value: " << exc.what()
                  << std::endl;
        return 1;
      }
      if (max_inland_km < 0.0) {
        std::cerr << "Error: --max-inland-km must be >= 0" << std::endl;
        return 1;
      }
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

  if (max_inland_km > 0.0) {
    std::cout << "Inland-distance cap: " << max_inland_km << " km" << std::endl;
  }

  // Load the water shapefile
  auto water_shp = uwp::Shapefile(water_file);
  water_shp.build_rtree_index();

  // Process each region polygon. The region set is much smaller than the
  // water set, so we drive the loop by region polygons and query the water
  // R-tree built above — no region R-tree is needed.
  for (const auto& region_file : region_files) {
    std::cout << "Processing region file: " << region_file << std::endl;

    auto area_shp = uwp::Shapefile(region_file);

    auto overlap =
        uwp::select_overlap(water_shp, *(area_shp.polygons()), max_inland_km);
    uwp::merge_overlapping(water_shp, overlap);
  }

  // Save the final result
  std::cout << "Saving result to: " << output_file << std::endl;
  water_shp.save(output_file);

  return 0;
}
