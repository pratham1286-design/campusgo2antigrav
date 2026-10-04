// Public live-tracking page for a shared CampusGo ride. Uses textContent only.
(function () {
  const STATUS_TEXT = {
    queued: 'Waiting for a driver',
    matched: 'Driver assigned',
    arriving: 'Driver on the way to pickup',
    in_progress: 'Trip in progress'
  };
  const token = decodeURIComponent(window.location.pathname.split('/').filter(Boolean).pop() || '');
  const message = document.getElementById('track-message');
  let map = null;
  let driverMarker = null;
  let routeDrawn = false;
  let timer = null;

  function setText(id, value) {
    document.getElementById(id).textContent = value || '—';
  }

  function ensureMap(ride) {
    if (map || typeof L === 'undefined') return;
    map = L.map('track-map', { attributionControl: false }).setView([ride.pickup_lat, ride.pickup_lng], 14);
    L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 19 }).addTo(map);
    L.circleMarker([ride.pickup_lat, ride.pickup_lng], { radius: 8, color: '#10B981' }).addTo(map).bindTooltip('Pickup');
    L.circleMarker([ride.drop_lat, ride.drop_lng], { radius: 8, color: '#FF7C00' }).addTo(map).bindTooltip('Drop');
    map.fitBounds([[ride.pickup_lat, ride.pickup_lng], [ride.drop_lat, ride.drop_lng]], { padding: [30, 30] });
  }

  async function refresh() {
    try {
      const res = await fetch(`/api/rides/share/${encodeURIComponent(token)}`);
      const data = await res.json();
      if (!res.ok) {
        message.textContent = data.error || 'This tracking link has expired.';
        message.className = 'track-error';
        document.getElementById('track-details').hidden = true;
        clearInterval(timer);
        return;
      }
      const ride = data.ride;
      message.textContent = `Last updated ${new Date().toLocaleTimeString()}`;
      document.getElementById('track-details').hidden = false;
      setText('track-status', STATUS_TEXT[ride.status] || ride.status);
      setText('track-rider', ride.rider_name);
      setText('track-pickup', ride.pickup_name);
      setText('track-drop', ride.drop_name);
      setText('track-driver', ride.driver_name);
      setText('track-vehicle', [ride.vehicle_model, ride.vehicle_color, ride.vehicle_plate].filter(Boolean).join(' • '));

      ensureMap(ride);
      if (map && !routeDrawn && Array.isArray(ride.route_points) && ride.route_points.length > 1) {
        routeDrawn = true;
        const line = L.polyline(ride.route_points, { color: '#FF7C00', weight: 5, opacity: 0.85 }).addTo(map);
        map.fitBounds(line.getBounds(), { padding: [30, 30] });
      }
      if (map && ride.driver_lat && ride.driver_lng) {
        if (!driverMarker) {
          driverMarker = L.circleMarker([ride.driver_lat, ride.driver_lng], { radius: 10, color: '#111827', fillOpacity: 0.9 })
            .addTo(map).bindTooltip('Vehicle');
        } else {
          driverMarker.setLatLng([ride.driver_lat, ride.driver_lng]);
        }
      }
    } catch (err) {
      message.textContent = 'Could not reach CampusGo. Retrying...';
    }
  }

  refresh();
  timer = setInterval(refresh, 15000);
})();
