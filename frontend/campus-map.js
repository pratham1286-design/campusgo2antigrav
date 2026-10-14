// The base map, drawn in the browser from CampusGo's own map files: no tile server, API key or
// usage limit. area.pmtiles covers the whole service area in overview detail (data zoom <= 13,
// shown up to map zoom 14); streets.pmtiles adds street detail (data zoom 14, shown from 15 up).
// protomaps-leaflet draws one data zoom below the map zoom, hence the off-by-one.
// The OpenStreetMap credit is required by the data licence and must stay visible.
window.addCampusBasemap = function addCampusBasemap(map) {
  const credit = '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a>'
    + ' &middot; <a href="https://protomaps.com" target="_blank" rel="noopener">Protomaps</a>';
  const style = { flavor: 'light', lang: 'en', noWrap: true };
  protomapsL.leafletLayer(Object.assign({ url: '/maps/area.pmtiles', maxDataZoom: 13, maxZoom: 14, attribution: credit }, style))
    .addTo(map);
  protomapsL.leafletLayer(Object.assign({ url: '/maps/streets.pmtiles', maxDataZoom: 14, minZoom: 15, maxZoom: 19, attribution: '' }, style))
    .addTo(map);
  map.setMinZoom(7);
  map.setMaxZoom(19);
  map.attributionControl.setPrefix(false);
};
