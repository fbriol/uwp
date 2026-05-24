#!/usr/bin/env python3
"""On-demand HTTP server that streams patches to a Leaflet viewer.

Architecture:
  - Server: stdlib `http.server` (no new deps). At startup, loads the
    patches shapefile (+ optional coastline) into RAM via pyogrio and
    builds a `shapely.STRtree` spatial index. Per request, queries the
    tree for the current viewport bbox and returns GeoJSON.
  - Frontend: Leaflet with the canvas renderer. On every `moveend` it
    fires a debounced fetch of `/api/<layer>?bbox=lon,lat,lon,lat`,
    replacing the layer with the response.

Why this design and not embedded GeoJSON (visualize_interactive.py) or
PMTiles (the failed attempt):
  - Embedded GeoJSON loads the whole dataset into the browser. Past
    a few thousand polygons the browser slows down.
  - PMTiles bakes every zoom level into a static file. For QA on
    large irregular polygons this explodes the storage and silently
    drops features unless flags are disabled, which then explodes the
    file size even more.
  - This server keeps the data in the Python process and only ships
    what the viewport needs at any moment. Pan / zoom remain fluid
    regardless of the total dataset size: the client only ever sees
    O(features visible in viewport).

Usage:
    python script/visualize_server.py \\
        --patches  ~/Downloads/data/patches.shp \\
        --coastline data/corrected-water-polygons.shp \\
        --port 8000

Then open http://localhost:8000/ in Chrome / Firefox.
"""

from __future__ import annotations

import argparse
import colorsys
import http.server
import json
import logging
import pathlib
import socketserver
import sys
import threading
from urllib.parse import parse_qs, urlparse

import numpy as np  # noqa: TC002  — used in public method signatures
import pyogrio
import pyogrio.raw
import shapely

LOGGER = logging.getLogger(__name__)

ROOT = pathlib.Path(__file__).parent.parent
DATA_DIR = ROOT / 'data'

DEFAULT_PATCHES = DATA_DIR / 'patches.shp'
DEFAULT_COASTLINE = DATA_DIR / 'corrected-water-polygons.shp'

# Hard cap on features per response. A bbox returning more than this
# typically means the user is too zoomed out for QA to be meaningful;
# we return the first N rather than blowing up the browser.
MAX_FEATURES_PER_RESPONSE = 5000


# ---------------------------------------------------------------------------
# Colour helper
# ---------------------------------------------------------------------------


def _hex_color(fid: int) -> str:
    """Stable HSL-derived hex colour per feature id."""
    h = ((hash(fid) % 360 + 360) % 360) / 360.0
    r, g, b = colorsys.hls_to_rgb(h, 0.55, 0.7)
    return f'#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}'


# ---------------------------------------------------------------------------
# In-memory store with spatial index
# ---------------------------------------------------------------------------


class _Layer:
    """A shapefile loaded into memory with a precomputed STRtree."""

    __slots__ = ('bounds', 'colors', 'field_names', 'fields', 'geoms', 'tree')

    def __init__(self, path: pathlib.Path, label: str, *, colorize: bool):
        LOGGER.info('Loading %s: %s', label, path)
        info = pyogrio.read_info(str(path))
        crs = info.get('crs')
        if (
            crs is not None
            and 'WGS 84' not in str(crs)
            and '4326' not in str(crs)
        ):
            LOGGER.warning('%s CRS is %s — expected EPSG:4326', label, crs)
        meta, _fids, wkb_blobs, field_arrays = pyogrio.raw.read(
            str(path), return_fids=False
        )
        self.geoms = shapely.from_wkb(wkb_blobs)
        self.field_names = list(meta['fields'])[:6]
        self.fields = dict(zip(meta['fields'], field_arrays, strict=True))
        bounds_arr = shapely.total_bounds(self.geoms)
        self.bounds = (
            float(bounds_arr[0]),
            float(bounds_arr[1]),
            float(bounds_arr[2]),
            float(bounds_arr[3]),
        )
        self.tree = shapely.STRtree(self.geoms)
        # Pre-compute the per-feature colour once (cheap) rather than
        # hashing on every request.
        self.colors = (
            [_hex_color(i) for i in range(len(self.geoms))]
            if colorize
            else None
        )
        LOGGER.info(
            '  %s: %d features, %d field(s), bounds=%s',
            label,
            len(self.geoms),
            len(self.field_names),
            self.bounds,
        )

    def query(
        self,
        bbox: tuple[float, float, float, float],
        limit: int,
        simplify_divisor: float = 0.0,
    ) -> tuple[np.ndarray, np.ndarray]:
        """Return (geom_indices, geom_array) for features intersecting
        the bbox, capped at `limit`.

        If `simplify_divisor > 0`, geometries are simplified with a
        Douglas-Peucker tolerance equal to
        `max(bbox_width, bbox_height) / simplify_divisor`. That makes
        detail scale with viewport: at world view the tolerance is
        ~360/divisor degrees (coarse, tiny GeoJSON); zoomed onto a
        city the tolerance is sub-metre (full detail). Inspired by
        the way Mapshaper resamples on the fly.
        """
        box = shapely.box(*bbox)
        idx = self.tree.query(box, predicate='intersects')
        if len(idx) > limit:
            LOGGER.warning('%d features in bbox, capped to %d', len(idx), limit)
            idx = idx[:limit]
        geoms = self.geoms[idx]
        if simplify_divisor > 0 and len(geoms) > 0:
            extent = max(bbox[2] - bbox[0], bbox[3] - bbox[1])
            tol = extent / simplify_divisor
            geoms = shapely.simplify(
                geoms, tolerance=tol, preserve_topology=True
            )
        return idx, geoms

    def to_geojson(self, indices: np.ndarray, geoms: np.ndarray) -> dict:
        """Build a GeoJSON FeatureCollection from selected features."""
        if len(geoms) == 0:
            return {'type': 'FeatureCollection', 'features': []}
        geom_jsons = [json.loads(s) for s in shapely.to_geojson(geoms)]
        features = []
        for local_i, global_i in enumerate(indices):
            props: dict = {'_fid': int(global_i)}
            for name in self.field_names:
                val = self.fields[name][global_i]
                if hasattr(val, 'item'):
                    val = val.item()
                props[name] = val
            if self.colors is not None:
                props['_color'] = self.colors[int(global_i)]
            features.append(
                {
                    'type': 'Feature',
                    'id': int(global_i),
                    'geometry': geom_jsons[local_i],
                    'properties': props,
                }
            )
        return {'type': 'FeatureCollection', 'features': features}


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------


def _make_handler(
    layers: dict[str, _Layer],
    html: bytes,
    simplify_divisor: float,
) -> type[http.server.BaseHTTPRequestHandler]:
    """Return a request handler class with `layers` and `html` bound to
    it. http.server doesn't pass user state through cleanly, so we
    parametrise via a closure-style factory."""

    class _Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        # Quiet the default per-request stderr noise; use the project
        # logger so timestamps line up with the rest.
        def log_message(self, fmt: str, *args) -> None:
            LOGGER.info('%s - %s', self.address_string(), fmt % args)

        def do_GET(self) -> None:
            # The client (Leaflet via AbortController) frequently cuts
            # the TCP connection mid-response when the user pans / zooms
            # before the previous fetch completes. That leaves us writing
            # to a closed socket → BrokenPipeError / ConnectionResetError.
            # Catch it at the top of the request handler so the noisy
            # traceback doesn't pollute the log; the client doesn't want
            # the response anyway.
            try:
                parsed = urlparse(self.path)
                path = parsed.path
                if path in ('/', '/index.html'):
                    self._send_bytes(html, 'text/html; charset=utf-8')
                    return
                if path.startswith('/api/'):
                    layer_name = path[len('/api/') :]
                    self._serve_layer(layer_name, parsed.query)
                    return
                self.send_error(404, 'Not found')
            except (BrokenPipeError, ConnectionResetError):
                LOGGER.debug(
                    'Client %s aborted %s mid-response',
                    self.address_string(),
                    self.path,
                )

        def _serve_layer(self, name: str, query: str) -> None:
            layer = layers.get(name)
            if layer is None:
                self.send_error(404, f'Unknown layer: {name}')
                return
            qs = parse_qs(query)
            bbox_str = qs.get('bbox', [''])[0]
            try:
                bbox_parts = [float(x) for x in bbox_str.split(',')]
                if len(bbox_parts) != 4:
                    raise ValueError('bbox must have 4 floats')
                bbox = (
                    bbox_parts[0],
                    bbox_parts[1],
                    bbox_parts[2],
                    bbox_parts[3],
                )
            except ValueError as exc:
                self.send_error(400, f'Invalid bbox: {exc}')
                return
            limit = int(qs.get('limit', [MAX_FEATURES_PER_RESPONSE])[0])
            indices, geoms = layer.query(
                bbox,
                limit=limit,
                simplify_divisor=simplify_divisor,
            )
            data = layer.to_geojson(indices, geoms)
            payload = json.dumps(data).encode('utf-8')
            self._send_bytes(payload, 'application/json; charset=utf-8')

        def _send_bytes(self, payload: bytes, content_type: str) -> None:
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(payload)))
            # CORS so the viewer works even when opened via a different
            # origin (rare here, but harmless and useful for debug).
            self.send_header('Access-Control-Allow-Origin', '*')
            # Disable caching — the bbox query is the cache key on the
            # client side, server-side cache would just consume RAM.
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(payload)

    return _Handler


class _ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


# ---------------------------------------------------------------------------
# HTML viewer
# ---------------------------------------------------------------------------


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>UWP server-streamed viewer</title>
<link rel="stylesheet"
  href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
<style>
  body, #map {{ margin: 0; height: 100vh; }}
  #panel {{
    position: absolute; top: 10px; right: 10px;
    background: white; padding: 10px 14px; border-radius: 6px;
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.15);
    font-family: system-ui, sans-serif; font-size: 13px;
    z-index: 1000;
  }}
  #panel label {{ display: block; margin: 4px 0; cursor: pointer; }}
  #panel h3 {{ margin: 0 0 6px; font-size: 13px; }}
  #status {{ font-size: 11px; color: #666; margin-top: 6px; }}
  .leaflet-popup-content pre {{
    font-size: 11px; margin: 0; max-width: 320px; overflow-x: auto;
  }}
</style>
</head>
<body>
<div id="map"></div>
<div id="panel">
  <h3>Layers</h3>
  <label><input type="checkbox" id="cb-osm" checked> OSM basemap</label>
  {coastline_checkbox}
  <label><input type="checkbox" id="cb-patches" checked>
    Added patches</label>
  <div id="status">ready</div>
</div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
  const bounds = {bounds_js};
  const map = L.map('map', {{ preferCanvas: true }}).fitBounds(bounds);

  const osm = L.tileLayer(
    'https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png',
    {{ attribution: '&copy; OpenStreetMap contributors', maxZoom: 19 }}
  ).addTo(map);

  // A canvas renderer shared by every vector layer keeps pan/zoom GPU
  // friendly even with thousands of polygons in the viewport.
  const renderer = L.canvas({{ padding: 0.5 }});

  function styleColored(feat) {{
    return {{
      color: '#000', weight: 0.4,
      fillColor: feat.properties._color || '#ffdd44',
      fillOpacity: 0.65,
    }};
  }}

  function styleCoast(_feat) {{
    return {{
      color: '#444', weight: 0.8, fillOpacity: 0,
    }};
  }}

  function makeLayer(style) {{
    return L.geoJSON(null, {{
      renderer: renderer,
      style: style,
      onEachFeature: (feat, lyr) => {{
        const props = Object.fromEntries(
          Object.entries(feat.properties || {{}})
            .filter(([k]) => !k.startsWith('_'))
        );
        lyr.bindPopup(
          '<pre>' + JSON.stringify(props, null, 2) + '</pre>'
        );
      }},
    }});
  }}

  const patchesLayer = makeLayer(styleColored).addTo(map);
  const coastLayer = {coastline_js};

  const status = document.getElementById('status');
  let lastBboxKey = null;
  let timer = null;
  let inflight = null;

  function fetchLayer(endpoint, layer, label) {{
    const b = map.getBounds();
    const bbox = [
      b.getWest(), b.getSouth(), b.getEast(), b.getNorth()
    ].map(v => v.toFixed(5)).join(',');
    const key = endpoint + ':' + bbox;
    if (key === lastBboxKey) return;  // viewport unchanged
    lastBboxKey = key;
    if (inflight) inflight.abort();
    inflight = new AbortController();
    status.textContent = `loading ${{label}}…`;
    fetch(endpoint + '?bbox=' + bbox, {{ signal: inflight.signal }})
      .then(r => r.json())
      .then(data => {{
        layer.clearLayers();
        layer.addData(data);
        const n = data.features.length;
        status.textContent = `${{label}}: ${{n}} feature${{n===1?'':'s'}}`;
      }})
      .catch(err => {{
        if (err.name !== 'AbortError')
          status.textContent = 'error: ' + err.message;
      }});
  }}

  function refreshAll() {{
    lastBboxKey = null;  // force re-fetch on every refresh
    if (document.getElementById('cb-patches').checked) {{
      fetchLayer('/api/patches', patchesLayer, 'patches');
    }}
    if (coastLayer && document.getElementById('cb-coastline').checked) {{
      fetchLayer('/api/coastline', coastLayer, 'coastline');
    }}
  }}

  // Debounce: wait for the user to stop panning before fetching.
  map.on('moveend', () => {{
    clearTimeout(timer);
    timer = setTimeout(refreshAll, 200);
  }});

  // Layer toggles
  document.getElementById('cb-osm').addEventListener('change', (e) => {{
    if (e.target.checked) osm.addTo(map);
    else map.removeLayer(osm);
  }});
  document.getElementById('cb-patches').addEventListener('change', (e) => {{
    if (e.target.checked) {{
      patchesLayer.addTo(map);
      refreshAll();
    }} else {{
      map.removeLayer(patchesLayer);
    }}
  }});
  if (coastLayer) {{
    document.getElementById('cb-coastline').addEventListener(
      'change', (e) => {{
        if (e.target.checked) {{
          coastLayer.addTo(map);
          refreshAll();
        }} else {{
          map.removeLayer(coastLayer);
        }}
      }}
    );
    coastLayer.addTo(map);
  }}

  // First fetch
  refreshAll();
</script>
</body>
</html>
"""


def _build_html(
    patches_bounds: tuple[float, float, float, float],
    has_coastline: bool,
) -> bytes:
    minx, miny, maxx, maxy = patches_bounds
    bounds_js = f'[[{miny}, {minx}], [{maxy}, {maxx}]]'
    coastline_checkbox = (
        '<label><input type="checkbox" id="cb-coastline" checked> '
        'Revised coastline</label>'
        if has_coastline
        else ''
    )
    coastline_js = (
        'makeLayer(styleCoast).addTo(map)' if has_coastline else 'null'
    )
    return _HTML_TEMPLATE.format(
        bounds_js=bounds_js,
        coastline_checkbox=coastline_checkbox,
        coastline_js=coastline_js,
    ).encode('utf-8')


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        '--patches',
        type=pathlib.Path,
        default=DEFAULT_PATCHES,
        help='Patches shapefile (uwp --patches-output).',
    )
    parser.add_argument(
        '--coastline',
        type=pathlib.Path,
        default=DEFAULT_COASTLINE,
        help='Optional revised coastline drawn as a context layer. '
        'Pass "" to skip.',
    )
    parser.add_argument(
        '--host',
        default='localhost',
        help='Bind address. Use 0.0.0.0 to expose on the network.',
    )
    parser.add_argument(
        '--port',
        type=int,
        default=8000,
        help='Listen port.',
    )
    parser.add_argument(
        '--max-features',
        type=int,
        default=MAX_FEATURES_PER_RESPONSE,
        help='Per-request feature cap (avoids browser meltdown when '
        'the user zooms way out).',
    )
    parser.add_argument(
        '--simplify-divisor',
        type=float,
        default=4000.0,
        help='Adaptive simplification: tolerance per request is '
        'max(bbox_width, bbox_height) / SIMPLIFY_DIVISOR. Higher = '
        'more detail kept. 0 disables (raw geometry). Default 4000 '
        '≈ 1 px of detail on a typical 4000-wide map.',
    )
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        force=True,
    )
    args = _parse_args()

    if not args.patches.exists():
        LOGGER.error('Patches not found: %s', args.patches)
        sys.exit(1)

    # Pre-load + index everything before opening the socket so the
    # first browser request hits a warm cache.
    layers: dict[str, _Layer] = {
        'patches': _Layer(args.patches, 'patches', colorize=True),
    }
    # Treat empty / "." / non-files as "no coastline" rather than
    # crashing on a directory read (Path("") → Path(".") gotcha).
    if args.coastline is not None and args.coastline.is_file():
        layers['coastline'] = _Layer(
            args.coastline,
            'coastline',
            colorize=False,
        )
    elif args.coastline is not None and str(args.coastline) not in ('', '.'):
        LOGGER.warning('Coastline not found / not a file: %s', args.coastline)

    global MAX_FEATURES_PER_RESPONSE  # noqa: PLW0603
    MAX_FEATURES_PER_RESPONSE = args.max_features

    html = _build_html(
        layers['patches'].bounds,
        has_coastline='coastline' in layers,
    )
    handler_cls = _make_handler(
        layers,
        html,
        args.simplify_divisor,
    )
    server = _ThreadedHTTPServer((args.host, args.port), handler_cls)
    LOGGER.info(
        'Serving at http://%s:%d/ (Ctrl-C to stop). '
        'Layers: %s. Max features/request: %d. '
        'Simplify divisor: %g.',
        args.host,
        args.port,
        ', '.join(sorted(layers)),
        args.max_features,
        args.simplify_divisor,
    )
    # Run in a thread so KeyboardInterrupt propagates cleanly on macOS.
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        t.join()
    except KeyboardInterrupt:
        LOGGER.info('Stopping…')
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    main()
