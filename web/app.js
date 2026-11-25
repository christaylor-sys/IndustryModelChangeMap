require([
  "esri/Map",
  "esri/views/MapView",
  "esri/layers/FeatureLayer",
  "esri/Graphic",
  "esri/widgets/Home",
  "esri/widgets/Expand"
// Change Map -> EsriMap:
], function (EsriMap, MapView, FeatureLayer, Graphic, Home, Expand) {


  // Esri Living Atlas Generalized US Counties service (contains 5-digit FIPS field)
  const LIVING_ATLAS_COUNTIES_URL =
    "https://services.arcgis.com/P3ePLMYs2RVChkJx/arcgis/rest/services/USA_Counties_Generalized_Boundaries/FeatureServer/0";

  // Legacy FIPS fallback mapping (see pipeline/schema.py LEGACY_FIPS)
  const LEGACY_FIPS_REMAP = {
    "46113": "46102", // Shannon County, SD -> Oglala Lakota County, SD
    "51515": "51019"  // Bedford City, VA -> Bedford County, VA
  };

  // Application State
  let manifest = null;
  let geography = {};        // fips -> [county, state_code]
  let sliceCache = new Map();// file -> { fips: [34 numbers] }
  let countyGeometries = new Map(); // fips -> polygon geometry
  let mapView = null;
  let choroplethLayer = null;
  let epChart = null;
  let selectedFips = null;
  let currentSliceData = {};

  // DOM Elements
  const statusText = document.getElementById("statusText");
  const perilSelect = document.getElementById("perilSelect");
  const lobSelect = document.getElementById("lobSelect");
  const metricSelect = document.getElementById("metricSelect");
  const rpSelect = document.getElementById("rpSelect");
  const modeSelect = document.getElementById("modeSelect");
  const stateFilter = document.getElementById("stateFilter");
  const countySearch = document.getElementById("countySearch");

  // Formatters
  const fmtUSD = (val) => {
    if (val === null || val === undefined || Number.isNaN(val)) return "—";
    const abs = Math.abs(val);
    const sign = val < 0 ? "-" : "";
    if (abs >= 1e9) return `${sign}$${(abs / 1e9).toFixed(2)}B`;
    if (abs >= 1e6) return `${sign}$${(abs / 1e6).toFixed(2)}M`;
    if (abs >= 1e3) return `${sign}$${(abs / 1e3).toFixed(1)}K`;
    return `${sign}$${Math.round(abs).toLocaleString("en-US")}`;
  };

  const fmtPct = (pct) => {
    if (pct === null || pct === undefined || Number.isNaN(pct)) return "N/A";
    const sign = pct > 0 ? "+" : "";
    return `${sign}${(pct * 100).toFixed(1)}%`;
  };

  // Safe Gzip / JSON loader (handles both raw gzip streams and host-decompressed streams)
  async function fetchSliceJson(url) {
    if (sliceCache.has(url)) return sliceCache.get(url);

    const res = await fetch(url);
    if (!res.ok) throw new Error(`Failed to fetch ${url} (${res.status})`);
    const buf = await res.arrayBuffer();
    const bytes = new Uint8Array(buf);

    let text;
    // Check Gzip magic number (0x1f, 0x8b)
    if (bytes.length >= 2 && bytes[0] === 0x1f && bytes[1] === 0x8b) {
      const ds = new DecompressionStream("gzip");
      const decompressedStream = new Blob([bytes]).stream().pipeThrough(ds);
      text = await new Response(decompressedStream).text();
    } else {
      text = new TextDecoder("utf-8").decode(bytes);
    }

    const parsed = JSON.parse(text);
    sliceCache.set(url, parsed);
    return parsed;
  }

  // Metric index lookup inside the 34-number slice array
  function getMetricIndices(metric, rp) {
    const oldVer = manifest.versions.old;
    const newVer = manifest.versions.new;
    const suffix = metric === "AAL" ? "AAL" : `${metric}_${rp}`;
    return {
      oldIdx: manifest.layout.indexOf(`${oldVer}_${suffix}`),
      newIdx: manifest.layout.indexOf(`${newVer}_${suffix}`),
      oldAalIdx: manifest.layout.indexOf(`${oldVer}_AAL`),
      newAalIdx: manifest.layout.indexOf(`${newVer}_AAL`),
      suffix
    };
  }

  // Populate Dropdowns from manifest.json & geography.json
  function initControls() {
    const oldVer = manifest.versions.old;
    const newVer = manifest.versions.new;
    document.getElementById("versionTitle").textContent = `${oldVer} vs ${newVer}`;

    // Return Periods
    rpSelect.innerHTML = manifest.return_periods
      .map((rp) => `<option value="${rp}" ${rp === 100 ? "selected" : ""}>1-in-${rp} yr</option>`)
      .join("");

    // Distinct Model+Peril combinations
    const perilMap = new Map();
    manifest.slices.forEach((s) => {
      const key = `${s.model_code}|${s.peril}`;
      if (!perilMap.has(key)) {
        const label =
          s.model_code === manifest.all_code
            ? "All Models / Perils (ALL)"
            : `${s.peril} — ${s.model_name} (Model ${s.model_code})`;
        perilMap.set(key, { key, model_code: s.model_code, peril: s.peril, label });
      }
    });

    const perilOptions = Array.from(perilMap.values()).sort((a, b) =>
      a.model_code === "ALL" ? -1 : b.model_code === "ALL" ? 1 : a.label.localeCompare(b.label)
    );

    perilSelect.innerHTML = perilOptions
      .map((p) => `<option value="${p.key}">${p.label}</option>`)
      .join("");

    // States filter
    const states = Array.from(
      new Set(Object.values(geography).map((g) => g[1]).filter(Boolean))
    ).sort();
    stateFilter.innerHTML =
      `<option value="ALL">All US States (${states.length})</option>` +
      states.map((st) => `<option value="${st}">${st}</option>`).join("");

    syncLobDropdown();

    // Event Listeners
    perilSelect.addEventListener("change", () => {
      syncLobDropdown();
      refreshVisualization();
    });
    lobSelect.addEventListener("change", refreshVisualization);
    metricSelect.addEventListener("change", () => {
      rpSelect.disabled = metricSelect.value === "AAL";
      refreshVisualization();
    });
    rpSelect.addEventListener("change", refreshVisualization);
    modeSelect.addEventListener("change", refreshVisualization);
    stateFilter.addEventListener("change", refreshVisualization);
    countySearch.addEventListener("input", () => renderMoversTable(currentTableRows));
  }

  function syncLobDropdown() {
    const [modelCode, peril] = perilSelect.value.split("|");
    const matchingLobs = manifest.slices
      .filter((s) => s.model_code === modelCode && s.peril === peril)
      .sort((a, b) => (a.lob_code === "ALL" ? -1 : b.lob_code === "ALL" ? 1 : a.lob_code.localeCompare(b.lob_code)));

    const prevLob = lobSelect.value;
    lobSelect.innerHTML = matchingLobs
      .map((l) => {
        const desc = l.lob_description || `LOB ${l.lob_code}`;
        const label = l.lob_code === "ALL" ? "All Lines of Business (ALL)" : `${desc} (${l.lob_code})`;
        return `<option value="${l.lob_code}">${label}</option>`;
      })
      .join("");

    if (matchingLobs.some((l) => l.lob_code === prevLob)) {
      lobSelect.value = prevLob;
    }
  }

  // Fetch US County geometries once from Esri Living Atlas
  async function loadLivingAtlasGeometries() {
    statusText.textContent = "Fetching US County polygons from ArcGIS Living Atlas...";
    const sourceLayer = new FeatureLayer({
      url: LIVING_ATLAS_COUNTIES_URL,
      outFields: ["FIPS", "NAME", "STATE_ABBR"]
    });

    await sourceLayer.load();
    const query = sourceLayer.createQuery();
    query.where = "1=1";
    query.returnGeometry = true;
    query.outFields = ["FIPS"];
    query.maxRecordCountFactor = 5;

    const result = await sourceLayer.queryFeatures(query);
    result.features.forEach((feat) => {
      const fips = feat.attributes.FIPS;
      if (fips && feat.geometry) {
        countyGeometries.set(fips, feat.geometry);
      }
    });
  }

  // Build / Update Client-Side ArcGIS FeatureLayer Renderer
  function buildRenderer(mode, maxAbsValue) {
    if (mode === "pct_diff" || mode === "abs_diff") {
      const cap = mode === "pct_diff" ? 50 : Math.max(maxAbsValue, 1000);
      const field = mode === "pct_diff" ? "pct_diff_100" : "abs_diff";

      // Update Legend UI
      document.getElementById("legendTitle").textContent =
        mode === "pct_diff" ? "% Change (V13 vs V12)" : "$ Change (V13 − V12)";
      document.getElementById("legendBar").style.background =
        "linear-gradient(90deg, #2563eb 0%, #93c5fd 35%, #f8fafc 50%, #fca5a5 65%, #dc2626 100%)";
      document.getElementById("legendLabels").innerHTML =
        mode === "pct_diff"
          ? `<span>≤ -50%</span><span>0%</span><span>≥ +50%</span>`
          : `<span>${fmtUSD(-cap)}</span><span>$0</span><span>+${fmtUSD(cap)}</span>`;

      return {
        type: "simple",
        symbol: {
          type: "simple-fill",
          outline: { color: [15, 23, 42, 0.35], width: 0.4 }
        },
        visualVariables: [
          {
            type: "color",
            field: field,
            stops: [
              { value: -cap, color: "#2563eb" },
              { value: -cap * 0.25, color: "#93c5fd" },
              { value: 0, color: "#f1f5f9" },
              { value: cap * 0.25, color: "#fca5a5" },
              { value: cap, color: "#dc2626" }
            ]
          }
        ]
      };
    } else {
      const cap = Math.max(maxAbsValue, 1000);
      const field = mode === "new" ? "val_new" : "val_old";
      const verLabel = mode === "new" ? manifest.versions.new : manifest.versions.old;

      document.getElementById("legendTitle").textContent = `${verLabel} Loss ($)`;
      document.getElementById("legendBar").style.background =
        "linear-gradient(90deg, #fef9c3 0%, #facc15 35%, #f97316 70%, #b91c1c 100%)";
      document.getElementById("legendLabels").innerHTML =
        `<span>$0</span><span>${fmtUSD(cap * 0.5)}</span><span>≥ ${fmtUSD(cap)}</span>`;

      return {
        type: "simple",
        symbol: {
          type: "simple-fill",
          outline: { color: [15, 23, 42, 0.35], width: 0.4 }
        },
        visualVariables: [
          {
            type: "color",
            field: field,
            stops: [
              { value: 0, color: "#fef9c3" },
              { value: cap * 0.25, color: "#facc15" },
              { value: cap * 0.6, color: "#f97316" },
              { value: cap, color: "#b91c1c" }
            ]
          }
        ]
      };
    }
  }

  let currentTableRows = [];

  async function refreshVisualization() {
    const [modelCode, peril] = perilSelect.value.split("|");
    const lobCode = lobSelect.value;
    const metric = metricSelect.value;
    const rp = Number(rpSelect.value);
    const mode = modeSelect.value;
    const state = stateFilter.value;

    const sliceMeta = manifest.slices.find(
      (s) => s.model_code === modelCode && s.peril === peril && s.lob_code === lobCode
    );
    if (!sliceMeta) return;

    statusText.textContent = `Loading ${sliceMeta.file}...`;
    currentSliceData = await fetchSliceJson(`./data/${sliceMeta.file}`);

    const { oldIdx, newIdx, oldAalIdx, newAalIdx, suffix } = getMetricIndices(metric, rp);

    const graphics = [];
    const tableRows = [];
    let sumOldAal = 0;
    let sumNewAal = 0;
    let absValuesForScale = [];
    let objectIdCounter = 1;

    for (const [rawFips, values] of Object.entries(currentSliceData)) {
      const meta = geography[rawFips] || ["Unknown County", ""];
      const countyName = meta[0];
      const stateCode = meta[1];

      if (state !== "ALL" && stateCode !== state) continue;

      const valOld = Number(values[oldIdx] || 0);
      const valNew = Number(values[newIdx] || 0);
      const absDiff = valNew - valOld;
      const pctDiff = valOld > 0 ? absDiff / valOld : null;

      const aalOld = Number(values[oldAalIdx] || 0);
      const aalNew = Number(values[newAalIdx] || 0);
      sumOldAal += aalOld;
      sumNewAal += aalNew;

      if (mode === "abs_diff") absValuesForScale.push(Math.abs(absDiff));
      else if (mode === "new") absValuesForScale.push(valNew);
      else if (mode === "old") absValuesForScale.push(valOld);

      tableRows.push({
        fips: rawFips,
        county: `${countyName}, ${stateCode}`,
        valOld,
        valNew,
        absDiff,
        pctDiff
      });

      const geomFips = countyGeometries.has(rawFips)
        ? rawFips
        : LEGACY_FIPS_REMAP[rawFips] || rawFips;
      const geometry = countyGeometries.get(geomFips);

      if (geometry) {
        graphics.push(
          new Graphic({
            geometry,
            attributes: {
              ObjectID: objectIdCounter++,
              fips: rawFips,
              county_name: `${countyName}, ${stateCode}`,
              metric_label: suffix,
              val_old: valOld,
              val_new: valNew,
              abs_diff: absDiff,
              pct_diff_100: pctDiff !== null ? pctDiff * 100 : 0,
              fmt_old: fmtUSD(valOld),
              fmt_new: fmtUSD(valNew),
              fmt_diff: (absDiff >= 0 ? "+" : "") + fmtUSD(absDiff),
              fmt_pct: fmtPct(pctDiff)
            }
          })
        );
      }
    }

    // Compute 95th percentile for robust color scaling
    absValuesForScale.sort((a, b) => a - b);
    const p95 =
      absValuesForScale.length > 0
        ? absValuesForScale[Math.floor(absValuesForScale.length * 0.95)]
        : 100000;

    const renderer = buildRenderer(mode, p95);

    if (!choroplethLayer) {
      choroplethLayer = new FeatureLayer({
        source: graphics,
        objectIdField: "ObjectID",
        fields: [
          { name: "ObjectID", type: "oid" },
          { name: "fips", type: "string" },
          { name: "county_name", type: "string" },
          { name: "metric_label", type: "string" },
          { name: "val_old", type: "double" },
          { name: "val_new", type: "double" },
          { name: "abs_diff", type: "double" },
          { name: "pct_diff_100", type: "double" },
          { name: "fmt_old", type: "string" },
          { name: "fmt_new", type: "string" },
          { name: "fmt_diff", type: "string" },
          { name: "fmt_pct", type: "string" }
        ],
        renderer: renderer,
        popupTemplate: {
          title: "{county_name} (FIPS {fips})",
          content: [
            {
              type: "fields",
              fieldInfos: [
                { fieldName: "metric_label", label: "Metric" },
                { fieldName: "fmt_old", label: `${manifest.versions.old} Loss` },
                { fieldName: "fmt_new", label: `${manifest.versions.new} Loss` },
                { fieldName: "fmt_diff", label: "$ Change" },
                { fieldName: "fmt_pct", label: "% Change" }
              ]
            }
          ]
        }
      });
      mapView.map.add(choroplethLayer);
    } else {
      const existing = await choroplethLayer.queryFeatures();
      await choroplethLayer.applyEdits({
        deleteFeatures: existing.features,
        addFeatures: graphics
      });
      choroplethLayer.renderer = renderer;
    }

    // Update KPI Cards
    const netAalDiff = sumNewAal - sumOldAal;
    const netAalPct = sumOldAal > 0 ? netAalDiff / sumOldAal : null;
    document.getElementById("countyCountBadge").textContent = `${tableRows.length.toLocaleString()} counties`;
    document.getElementById("kpiOldVal").textContent = fmtUSD(sumOldAal);
    document.getElementById("kpiNewVal").textContent = fmtUSD(sumNewAal);
    document.getElementById("kpiDiffVal").textContent = (netAalDiff >= 0 ? "+" : "") + fmtUSD(netAalDiff);
    document.getElementById("kpiPctVal").textContent = fmtPct(netAalPct);

    // Update Top Movers Table
    currentTableRows = tableRows.sort((a, b) => Math.abs(b.absDiff) - Math.abs(a.absDiff));
    renderMoversTable(currentTableRows);

    // Refresh Selected County Chart if one is active
    if (selectedFips && currentSliceData[selectedFips]) {
      inspectCounty(selectedFips);
    } else if (currentTableRows.length > 0) {
      inspectCounty(currentTableRows[0].fips);
    }

    statusText.textContent = `Showing ${graphics.length.toLocaleString()} mapped counties (${sliceMeta.peril} / LOB ${sliceMeta.lob_code})`;
  }

  function renderMoversTable(rows) {
    const q = countySearch.value.trim().toLowerCase();
    const filtered = q
      ? rows.filter((r) => r.county.toLowerCase().includes(q) || r.fips.includes(q))
      : rows;

    const top = filtered.slice(0, 75);
    const tbody = document.getElementById("moversBody");
    tbody.innerHTML = top
      .map((r) => {
        const cls = r.absDiff >= 0 ? "pos-change" : "neg-change";
        return `
          <tr data-fips="${r.fips}">
            <td><strong>${r.county}</strong><br/><small style="color:#94a3b8">${r.fips}</small></td>
            <td class="num">${fmtUSD(r.valOld)}</td>
            <td class="num">${fmtUSD(r.valNew)}</td>
            <td class="num ${cls}">${r.absDiff >= 0 ? "+" : ""}${fmtUSD(r.absDiff)}</td>
            <td class="num ${cls}">${fmtPct(r.pctDiff)}</td>
          </tr>
        `;
      })
      .join("");

    tbody.querySelectorAll("tr").forEach((tr) => {
      tr.addEventListener("click", () => {
        const fips = tr.getAttribute("data-fips");
        inspectCounty(fips, true);
      });
    });
  }

  // Render V12 vs V13 EP Curve Chart for a selected County
  function inspectCounty(fips, zoomTo = false) {
    selectedFips = fips;
    const values = currentSliceData[fips];
    if (!values) return;

    const meta = geography[fips] || ["Unknown County", ""];
    document.getElementById("countyTitle").textContent = `${meta[0]}, ${meta[1]}`;
    document.getElementById("countyFipsBadge").textContent = `FIPS ${fips}`;

    const metric = metricSelect.value;
    const rp = Number(rpSelect.value);
    const { oldIdx, newIdx, oldAalIdx, newAalIdx, suffix } = getMetricIndices(metric, rp);

    const vOld = values[oldIdx] || 0;
    const vNew = values[newIdx] || 0;
    const aalOld = values[oldAalIdx] || 0;
    const aalNew = values[newAalIdx] || 0;

    document.getElementById("countyMetrics").classList.remove("hidden");
    document.getElementById("inspMetricLabel").textContent = suffix;
    document.getElementById("inspMetricComparison").textContent =
      `${fmtUSD(vOld)} → ${fmtUSD(vNew)} (${fmtPct(vOld > 0 ? (vNew - vOld) / vOld : null)})`;
    document.getElementById("inspAalComparison").textContent =
      `${fmtUSD(aalOld)} → ${fmtUSD(aalNew)} (${fmtPct(aalOld > 0 ? (aalNew - aalOld) / aalOld : null)})`;

    // Extract OEP or AEP curve across all 8 return periods
    const curveType = metric === "AEP" ? "AEP" : "OEP";
    const rps = manifest.return_periods;
    const oldCurve = rps.map((r) => values[manifest.layout.indexOf(`${manifest.versions.old}_${curveType}_${r}`)] || 0);
    const newCurve = rps.map((r) => values[manifest.layout.indexOf(`${manifest.versions.new}_${curveType}_${r}`)] || 0);

    const ctx = document.getElementById("epChart").getContext("2d");
    if (epChart) epChart.destroy();

    epChart = new Chart(ctx, {
      type: "line",
      data: {
        labels: rps.map((r) => `${r}yr`),
        datasets: [
          {
            label: `${manifest.versions.old} ${curveType}`,
            data: oldCurve,
            borderColor: "#94a3b8",
            backgroundColor: "rgba(148, 163, 184, 0.15)",
            borderDash: [4, 4],
            tension: 0.25
          },
          {
            label: `${manifest.versions.new} ${curveType}`,
            data: newCurve,
            borderColor: "#38bdf8",
            backgroundColor: "rgba(56, 189, 248, 0.2)",
            fill: true,
            tension: 0.25
          }
        ]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { labels: { color: "#cbd5e1", font: { size: 11 } } },
          tooltip: {
            callbacks: {
              label: (ctx) => `${ctx.dataset.label}: ${fmtUSD(ctx.parsed.y)}`
            }
          }
        },
        scales: {
          x: { ticks: { color: "#94a3b8", font: { size: 10 } }, grid: { color: "rgba(51,65,85,0.4)" } },
          y: {
            ticks: {
              color: "#94a3b8",
              font: { size: 10 },
              callback: (v) => fmtUSD(v)
            },
            grid: { color: "rgba(51,65,85,0.4)" }
          }
        }
      }
    });

    if (zoomTo) {
      const geom = countyGeometries.get(fips) || countyGeometries.get(LEGACY_FIPS_REMAP[fips]);
      if (geom && mapView) {
        mapView.goTo({ target: geom.extent.expand(2.2) }, { duration: 600 });
      }
    }
  }

  // Initialise Map & Load Data
  async function init() {
    try {
      // Change new Map(...) -> new EsriMap(...):
const map = new EsriMap({ basemap: "dark-gray-vector" });
      mapView = new MapView({
        container: "viewDiv",
        map: map,
        center: [-96, 38.5],
        zoom: 4
      });

      mapView.ui.add(new Home({ view: mapView }), "top-left");

      // Load manifest.json and geography.json in parallel with Living Atlas geometries
      const [manifestRes, geoRes] = await Promise.all([
        fetch("./data/manifest.json"),
        fetch("./data/geography.json"),
        loadLivingAtlasGeometries()
      ]);

      manifest = await manifestRes.json();
      geography = await geoRes.json();

      initControls();
      await refreshVisualization();

      // Click handler on map counties
      mapView.on("click", async (event) => {
        const hit = await mapView.hitTest(event);
        const result = hit.results.find((r) => r.graphic && r.graphic.layer === choroplethLayer);
        if (result) {
          const fips = result.graphic.attributes.fips;
          inspectCounty(fips, false);
        }
      });
    } catch (err) {
      console.error(err);
      statusText.textContent = `Error: ${err.message}`;
    }
  }

  init();
});
