let coverageMap, coverageLayer, candidateLayer, routeLayer, coverageRows = [];
const customerIcon = L.divIcon({
  className: "customer-marker",
  html: '<span></span>',
  iconSize: [24, 24],
  iconAnchor: [12, 24],
  popupAnchor: [0, -24]
});

function distanceKm(a, b) {
  const rad = Math.PI / 180, dLat = (b.latitude - a.lat) * rad, dLon = (b.longitude - a.lng) * rad;
  const x = Math.sin(dLat / 2) ** 2 + Math.cos(a.lat * rad) * Math.cos(b.latitude * rad) * Math.sin(dLon / 2) ** 2;
  return 6371 * 2 * Math.atan2(Math.sqrt(x), Math.sqrt(1 - x));
}

function parseCoordinates(value) {
  const text = value.trim();
  const pair = text.match(/(-?\d+(?:\.\d+)?)\s*[,;\s]\s*(-?\d+(?:\.\d+)?)/);
  const google = text.match(/@(-?\d+(?:\.\d+)?),\s*(-?\d+(?:\.\d+)?)/) ||
    text.match(/[?&](?:q|ll|query)=(-?\d+(?:\.\d+)?),\s*(-?\d+(?:\.\d+)?)/);
  const match = google || pair;
  if (!match) return null;
  const lat = Number(match[1]), lng = Number(match[2]);
  return lat >= -90 && lat <= 90 && lng >= -180 && lng <= 180 ? {lat, lng} : null;
}

async function showCustomerLocation(coords) {
  const zone = $("#coverage-zone").value;
  const candidates = coverageRows.filter(r => (!zone || String(r.zone_id) === zone) &&
    r.latitude != null && r.longitude != null);
  if (!candidates.length) throw new Error("Belum ada ODB berkoordinat pada filter zone ini.");
  const nearestByLine = candidates.map(r => ({odb: r, km: distanceKm(coords, r)}))
    .sort((a, b) => a.km - b.km).slice(0, 5);
  const roadRoutes = (await Promise.all(nearestByLine.map(async candidate => {
    try {
      const url = `https://router.project-osrm.org/route/v1/driving/${coords.lng},${coords.lat};${candidate.odb.longitude},${candidate.odb.latitude}?overview=full&geometries=geojson`;
      const response = await fetch(url);
      if (!response.ok) return null;
      const data = await response.json();
      const route = data.routes?.[0];
      return route?.geometry ? {...candidate, distance: route.distance, geometry: route.geometry} : null;
    } catch {
      return null;
    }
  }))).filter(Boolean).sort((a, b) => a.distance - b.distance);
  const nearest = roadRoutes[0] || nearestByLine[0];
  const roadDistance = roadRoutes[0]?.distance ?? null;
  candidateLayer.clearLayers();
  routeLayer.clearLayers();
  const distanceMeters = Math.round(roadDistance ?? nearest.km * 1000);
  const distanceLabel = roadDistance == null ? "jarak garis lurus" : "ODP Terdekat";
  const customer = L.marker([coords.lat, coords.lng], {title: "Calon customer", icon: customerIcon})
    .bindPopup(`<b>Calon customer</b><br>${distanceLabel}: ${distanceMeters.toLocaleString("id-ID")} meter`)
    .addTo(candidateLayer);
  L.circle([coords.lat, coords.lng], {radius: 30, color: "#dc3545", fillOpacity: .15}).addTo(candidateLayer);
  const target = [nearest.odb.latitude, nearest.odb.longitude];
  if (roadRoutes[0]) {
    L.geoJSON(nearest.geometry, {style: {color: "#dc3545", weight: 4, dashArray: "8 8"}}).addTo(routeLayer);
  } else {
    L.polyline([[coords.lat, coords.lng], target], {color: "#dc3545", weight: 4, dashArray: "8 8"}).addTo(routeLayer);
  }
  L.marker(target, {title: `ODB terdekat: ${nearest.odb.name}`})
    .bindPopup(`<b>ODB ${esc(nearest.odb.name)}</b><br>${esc(nearest.odb.zone) || "Tanpa zone"}<br>${distanceLabel}: ${distanceMeters.toLocaleString("id-ID")} meter`)
    .addTo(routeLayer);
  customer.openPopup();
  coverageMap.fitBounds([[coords.lat, coords.lng], target], {padding: [50, 50], maxZoom: 18});
  $("#customer-result").innerHTML = `<b>ODB terdekat: ${esc(nearest.odb.name)}</b> · ${distanceMeters.toLocaleString("id-ID")} meter (${distanceLabel}) · ${esc(nearest.odb.zone) || "Tanpa zone"}`;
}

function renderCoverage() {
  const zone = $("#coverage-zone").value;
  const rows = coverageRows.filter(r => !zone || String(r.zone_id) === zone);
  $("#coverage-count").textContent = `${rows.length} ODB dengan koordinat`;
  coverageLayer.clearLayers();
  const points = [];
  rows.filter(r => r.latitude != null && r.longitude != null).forEach(r => {
    const point = [r.latitude, r.longitude]; points.push(point);
    L.marker(point).bindPopup(`<b>${esc(r.name)}</b><br>${esc(r.zone) || "Tanpa zone"}<br>ONU: ${r.onus}`).addTo(coverageLayer);
  });
  if (points.length) coverageMap.fitBounds(points, {padding: [24, 24], maxZoom: 16});
}

(async function initCoverage() {
  const mapElement = $("#coverage-map");
  if (typeof L === "undefined") {
    mapElement.innerHTML = '<div class="coverage-status err">Library peta tidak dapat dimuat. Periksa koneksi internet atau konfigurasi CDN Leaflet.</div>';
    return;
  }
  coverageMap = L.map("coverage-map").setView([-2.5, 118], 5);
  const street = L.tileLayer(
    "https://tile.openstreetmap.de/{z}/{x}/{y}.png",
    {maxZoom: 19, maxNativeZoom: 19, attribution: "&copy; OpenStreetMap contributors"}
  );
  const imagery = L.tileLayer(
    "https://a.tile.openstreetmap.fr/osmfr/{z}/{x}/{y}.png",
    {maxZoom: 20, maxNativeZoom: 20, attribution: "&copy; OpenStreetMap contributors, OSM France"}
  );
  street.addTo(coverageMap);
  L.control.layers({"Street map": street, "Satellite": imagery}, null, {collapsed: false}).addTo(coverageMap);
  coverageLayer = L.layerGroup().addTo(coverageMap);
  candidateLayer = L.layerGroup().addTo(coverageMap);
  routeLayer = L.layerGroup().addTo(coverageMap);
  setTimeout(() => coverageMap.invalidateSize(), 0);
  const [odbs, zones] = await Promise.all([api("/api/odbs"), api("/api/zones")]);
  coverageRows = odbs;
  $("#coverage-zone").innerHTML += zones.map(z => `<option value="${z.id}">${esc(z.name)}</option>`).join("");
  $("#coverage-zone").onchange = renderCoverage;
  $("#check-customer").onclick = async () => {
    try {
      const coords = parseCoordinates($("#customer-location").value);
      if (!coords) throw new Error("Koordinat tidak valid. Gunakan latitude, longitude atau link Google Maps.");
      await showCustomerLocation(coords);
    } catch (err) {
      $("#customer-result").textContent = err.message;
      $("#customer-result").className = "coverage-result err small";
    }
  };
  $("#my-location").onclick = () => {
    if (!navigator.geolocation) { $("#customer-result").textContent = "Browser tidak mendukung lokasi."; return; }
    navigator.geolocation.getCurrentPosition(async position => {
      const coords = {lat: position.coords.latitude, lng: position.coords.longitude};
      $("#customer-location").value = `${coords.lat.toFixed(6)}, ${coords.lng.toFixed(6)}`;
      await showCustomerLocation(coords);
    }, () => { $("#customer-result").textContent = "Lokasi tidak dapat diakses."; });
  };
  renderCoverage();
  setTimeout(() => coverageMap.invalidateSize(), 100);
})().catch(err => {
  $("#coverage-map").innerHTML = `<div class="coverage-status err">Coverage gagal dimuat: ${esc(err.message)}</div>`;
  toast(err.message, true);
});
