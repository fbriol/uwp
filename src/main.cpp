#include <exception>
#include <iostream>
#include <string>
#include <vector>

#include "uwp/logging.hpp"
#include "uwp/shapefile.hpp"
#include "uwp/update.hpp"

namespace {

void print_usage(const char* argv0) {
  std::cerr << "Usage: " << argv0
            << " water_polygon [-o water_polygon_output]"
               " [--max-inland-km N] [--patches-output patches.shp]"
               " region_polygon1 [region_polygon2 ...]"
            << std::endl;
}

}  // namespace

int main(int argc, char* argv[]) {
  if (argc < 2) {
    print_usage(argv[0]);
    return 1;
  }

  // First parameter is the water polygon file
  std::string water_file = argv[1];

  // Parse the arguments to find -o option and region files
  std::string output_file;
  std::string patches_output_file;
  std::vector<std::string> region_files;
  bool output_specified = false;
  bool patches_output_specified = false;

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
        LOG_ERROR() << "invalid --max-inland-km value: " << exc.what();
        return 1;
      }
      if (max_inland_km < 0.0) {
        LOG_ERROR() << "--max-inland-km must be >= 0";
        return 1;
      }
    } else if (arg == "--patches-output" && i + 1 < argc) {
      patches_output_file = argv[++i];
      patches_output_specified = true;
    } else if (arg.length() > 1 && arg[0] == '-') {
      LOG_ERROR() << "unsupported option " << arg;
      return 1;
    } else {
      region_files.push_back(arg);
    }
  }

  // Check that we have at least one region file
  if (region_files.empty()) {
    LOG_ERROR() << "no region polygon files specified";
    return 1;
  }

  // Set default output name if not specified
  if (!output_specified) {
    output_file = water_file + "_updated.shp";
  }

  try {
    uwp::ScopedTimer total_timer("Total run");

    LOG_INFO() << "uwp starting (" << region_files.size() << " region file"
               << (region_files.size() > 1 ? "s" : "") << ")";
    LOG_INFO() << "  water input    : " << water_file;
    LOG_INFO() << "  output         : " << output_file;
    if (max_inland_km > 0.0) {
      LOG_INFO() << "  max-inland-km  : " << max_inland_km << " km";
    } else {
      LOG_INFO() << "  max-inland-km  : disabled";
    }
    if (patches_output_specified) {
      LOG_INFO() << "  patches output : " << patches_output_file;
    }

    // Load the water shapefile + build its spatial index.
    uwp::Shapefile water_shp;
    {
      uwp::ScopedTimer t("Loading water shapefile");
      water_shp.load(water_file, std::nullopt);
    }
    LOG_INFO() << "Water shapefile: " << water_shp.size() << " polygons";

    {
      uwp::ScopedTimer t("Building water R-tree");
      water_shp.build_rtree_index();
    }

    // Accumulator for the patches across every processed region. Stays empty
    // (and is never saved) unless --patches-output was passed.
    auto patches_shp = uwp::Shapefile();
    uwp::Shapefile* patches_ptr =
        patches_output_specified ? &patches_shp : nullptr;

    // Process each region polygon. The region set is much smaller than the
    // water set, so we drive the loop by region polygons and query the water
    // R-tree built above — no region R-tree is needed.
    const size_t total_regions = region_files.size();
    const size_t water_before = water_shp.size();
    for (size_t r = 0; r < total_regions; ++r) {
      const auto& region_file = region_files[r];
      LOG_INFO() << "[" << (r + 1) << "/" << total_regions
                 << "] Processing region: " << region_file;
      uwp::ScopedTimer region_timer("[" + std::to_string(r + 1) + "/" +
                                    std::to_string(total_regions) +
                                    "] Region " + region_file);

      uwp::Shapefile area_shp;
      {
        uwp::ScopedTimer t("  loading region shapefile");
        area_shp.load(region_file, std::nullopt);
      }
      LOG_INFO() << "  region polygons : " << area_shp.size();

      std::vector<std::pair<size_t, std::vector<uwp::Polygon>>> overlap;
      {
        uwp::ScopedTimer t("  select_overlap");
        overlap = uwp::select_overlap(water_shp, *(area_shp.polygons()),
                                      max_inland_km);
      }
      LOG_INFO() << "  matched coast polygons : " << overlap.size();

      // Matched area polygons get unioned with their host coast polygon
      // (or pushed as extras if disconnected after union). Unmatched
      // area polygons are intentionally ignored — earlier attempts to
      // include them as standalone patches (distance- or cascading-based)
      // introduced more artefacts (inland lakes, far-inland river
      // segments) than the holes they tried to fill.
      const size_t before = water_shp.size();
      const size_t patches_before = patches_ptr ? patches_ptr->size() : 0;
      {
        uwp::ScopedTimer t("  merge_overlapping");
        uwp::merge_overlapping(water_shp, overlap, patches_ptr);
      }
      LOG_INFO() << "  extra polygons appended : "
                 << (water_shp.size() - before);
      if (patches_ptr != nullptr) {
        LOG_INFO() << "  patches recorded        : "
                   << (patches_ptr->size() - patches_before);
      }
    }

    LOG_INFO() << "All regions processed: water polygons " << water_before
               << " → " << water_shp.size() << " (+"
               << (water_shp.size() - water_before) << ")";

    {
      uwp::ScopedTimer t("Saving water shapefile");
      LOG_INFO() << "Saving water polygons (" << water_shp.size()
                 << ") to: " << output_file;
      water_shp.save(output_file);
    }

    if (patches_output_specified) {
      uwp::ScopedTimer t("Saving patches shapefile");
      LOG_INFO() << "Saving patches (" << patches_shp.size()
                 << " polygons) to: " << patches_output_file;
      patches_shp.save(patches_output_file);
    }
  } catch (const std::exception& exc) {
    LOG_ERROR() << "fatal: " << exc.what();
    return 1;
  }

  return 0;
}
