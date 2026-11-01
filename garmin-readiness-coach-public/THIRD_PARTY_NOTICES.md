# Third-party notices

Readiness Coach for Garmin is MIT-licensed (see [LICENSE](LICENSE)). It uses the components below,
which keep their own licenses. This repository doesn't bundle any of them. Python packages are
installed from PyPI by `pip install -r requirements.txt`, and the front-end libraries load from
jsDelivr at runtime. License texts ship with each package (for Python packages, in the installed
`*.dist-info/` folder). Versions are the ones the test suite was last run with. `requirements.txt`
sets minimum versions only.

## Python packages: direct dependencies (`requirements.txt`)

| Package | Version tested | License | Project |
|---|---|---|---|
| anthropic | 2.3.0 | Apache-2.0 (versions before 2.0.0 were MIT; see its LICENSE-MIT) | https://github.com/anthropics/anthropic-sdk-python |
| fastapi | 0.162.0 | MIT | https://github.com/fastapi/fastapi |
| fit-tool | 0.9.17 | BSD-3-Clause | https://github.com/shaonianche/python_fit_tool |
| fitdecode | 0.11.0 | MIT | https://github.com/polyvertex/fitdecode |
| httpx | 0.28.1 | BSD-3-Clause | https://github.com/encode/httpx |
| pdfplumber | 0.11.10 | MIT | https://github.com/jsvine/pdfplumber |
| Pillow | 13.0.0 | MIT-CMU (HPND) | https://python-pillow.org |
| pillow-heif | 1.8.1 | BSD-3-Clause (source); **binary wheels GPL-2.0**, see below | https://github.com/bigcat88/pillow_heif |
| python-dotenv | 1.2.4 | BSD-3-Clause | https://github.com/theskumar/python-dotenv |
| python-multipart | 0.0.32 | Apache-2.0 | https://github.com/Kludex/python-multipart |
| pywebview | 6.2.1 | BSD-3-Clause | https://github.com/r0x0r/pywebview |
| uvicorn | 0.54.0 | BSD-3-Clause | https://github.com/encode/uvicorn |

**pillow-heif binary wheels.** The prebuilt wheels on PyPI bundle native libraries: libheif and
libde265 (LGPL-3.0) and x265 (GPL-2.0). Their `LICENSES_bundled.txt` says the wheels as a whole are
therefore GPL-2.0. This project only *uses* pillow-heif (for reading iPhone HEIC photos) and doesn't
redistribute it. If you redistribute a bundle that includes those wheels (e.g. a packaged app with
the `.venv` inside), you must meet the GPL-2.0/LGPL-3.0 terms for them.

## Python packages: transitive dependencies (installed by the above, macOS)

| Package | Version tested | License |
|---|---|---|
| annotated-doc | 0.0.5 | MIT |
| annotated-types | 0.8.0 | MIT |
| anyio | 4.15.1 | MIT |
| bottle | 0.13.4 | MIT |
| certifi | 2026.10.23 | MPL-2.0 |
| cffi | 2.1.1 | MIT-0 |
| charset-normalizer | 3.5.2 | MIT |
| click | 8.5.0 | BSD-3-Clause |
| cryptography | 50.0.2 | Apache-2.0 OR BSD-3-Clause |
| docstring_parser | 0.18.0 | MIT |
| h11 | 0.16.0 | MIT |
| httpcore | 1.0.9 | BSD-3-Clause |
| httpcore2 | 2.16.0 | BSD-3-Clause |
| httpx2 | 2.16.0 | BSD-3-Clause |
| idna | 3.20 | BSD-3-Clause |
| jiter | 0.18.0 | MIT |
| opentelemetry-api | 1.45.2 | Apache-2.0 |
| pdfminer.six | 20260107 | MIT |
| proxy_tools | 0.1.0 | MIT |
| pycparser | 3.12 | BSD-3-Clause |
| pydantic | 2.14.2 | MIT |
| pydantic_core | 2.50.2 | MIT |
| pyobjc-core, pyobjc-framework-Cocoa, -Quartz, -Security, -UniformTypeIdentifiers, -WebKit | 13.0 | MIT |
| pypdfium2 | 5.14.0 | BSD-3-Clause and Apache-2.0 (bundles PDFium; see its `licenses/` folder) |
| sniffio | 1.3.1 | MIT OR Apache-2.0 |
| starlette | 1.10.0 | BSD-3-Clause |
| truststore | 0.10.5 | MIT |
| typing-inspection | 0.4.4 | MIT |
| typing_extensions | 4.16.0 | PSF-2.0 |

Pillow's wheels also bundle native image libraries (libjpeg-turbo, libpng, libtiff, libwebp, libavif,
openjpeg, freetype, harfbuzz, lcms2, zlib-ng, brotli, xz, libxcb), each under its own permissive
license. They're listed in Pillow's license file.

## Front-end libraries (loaded from jsDelivr with Subresource Integrity)

| Library | Version | License | Files | Project |
|---|---|---|---|---|
| Chart.js | 4.4.1 | MIT | `dist/chart.umd.js` | https://www.chartjs.org/ · https://github.com/chartjs/Chart.js |
| Leaflet | 1.9.4 | BSD-2-Clause | `dist/leaflet.js`, `dist/leaflet.css` (+ its images) | https://leafletjs.com/ · https://github.com/Leaflet/Leaflet |

The SRI hashes in `static/index.html` were computed from the files in the official npm packages, and
the jsDelivr files were checked to be byte-identical. Copyright © Chart.js Contributors; Copyright ©
2010-2023 Volodymyr Agafonkin, © 2010-2011 CloudMade.

## Map tiles and map data

Activity maps (Leaflet) show basemap tiles from **CARTO** (`basemaps.cartocdn.com`, "Positron" and
"Dark Matter" styles), drawn from **OpenStreetMap** data. Tiles are only requested when you open a
map. Each map shows the required attribution: "© OpenStreetMap contributors © CARTO".

- **OpenStreetMap data** is © OpenStreetMap contributors, available under the
  [Open Database License (ODbL)](https://opendatacommons.org/licenses/odbl/). See
  https://www.openstreetmap.org/copyright. Attribution must stay visible.
- **CARTO basemaps** are subject to CARTO's
  [Basemap Terms](https://carto.com/legal/basemap-terms/), including a fair-use limit and the
  attribution requirement. CARTO describes free use for personal and other non-commercial projects
  and may require an API key or a commercial agreement beyond that. You're responsible for using the
  tiles within CARTO's current terms. Don't remove the attribution, and don't use the tile URLs
  for bulk download, offline caching or a hosted service.

## Online services the app can call (your own accounts and keys)

These aren't bundled components, but using them is subject to their own terms:

- **Garmin Connect Personal API**: [Personal API FAQ and terms](https://developer.garmin.com/gc-developer-program/personal/personal-faq/).
  For an individual's own data only, not for commercial applications serving multiple users.
- **Anthropic API** (optional): Anthropic's [Commercial Terms and Usage Policy](https://www.anthropic.com/legal).
- **Open-Meteo** (optional weather): [terms](https://open-meteo.com/en/terms). Free for
  non-commercial use, data under CC BY 4.0 ("Weather data by Open-Meteo.com").
- **Intervals.icu** (optional): [Intervals.icu terms](https://intervals.icu/).

## Trademarks

Garmin, Garmin Connect and Body Battery are trademarks of Garmin Ltd. or its subsidiaries. Claude
and Anthropic are trademarks of Anthropic PBC. BodySpec is a trademark of BodySpec, Inc. Chart.js,
Leaflet, OpenStreetMap, CARTO, Open-Meteo and Intervals.icu belong to their respective owners. This
project isn't affiliated with or endorsed by any of them.
