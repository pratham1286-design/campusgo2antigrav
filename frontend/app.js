/**
 * CampusGo - LPU Community Transit Application Controller
 * Handles Leaflet map, 3 vehicle services, server-validated wallet,
 * peak-time zone queueing, teacher priority, persistent SOS, and driver earnings.
 */

// Global Application State
const AppState = {
  currentUser: null,
  personas: [],
  landmarks: {},
  selectedScope: 'campus_hop', // 'campus_hop' | 'citylink'
  selectedService: 'bike',     // 'bike' | 'scooty' | 'car'
  pickupKey: 'uni_mall',
  dropKey: 'block_34',
  quotes: null,
  activeRide: null,
  driverInterval: null,
  homeMap: null,
  activeMap: null,
  homeMarkers: {},
  activeMarkers: {},
  activePolyline: null,
  mapAvailable: false
};

// --- Initialization ---
document.addEventListener('DOMContentLoaded', async () => {
  initNavigation();
  initModals();
  initMaps();
  initializeCustomLocationDropdowns();
  await loadLandmarks();
  await loadPersonas();
  initEventHandlers();
  await checkActiveRide();
});

window.initializeCustomLocationDropdowns = initializeCustomLocationDropdowns;
window.syncCustomLocationDropdowns = syncCustomLocationDropdowns;

// --- Map Initialization (Centered at LPU Punjab) ---
function initMaps() {
  const LPU_COORDS = [31.2536, 75.7037]; // LPU GT Road Campus Center

  if (typeof L === 'undefined') {
    renderMapFallback('campus-map', 'Campus map preview');
    renderMapFallback('active-tracking-map', 'Live tracking map');
    return;
  }

  AppState.mapAvailable = true;

  // 1. Home Screen Half-Screen Map (100% Free OpenStreetMap - No API key needed)
  AppState.homeMap = L.map('campus-map', {
    zoomControl: false,
    attributionControl: false
  }).setView(LPU_COORDS, 16);

  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19,
    attribution: '&copy; OpenStreetMap contributors'
  }).addTo(AppState.homeMap);

  L.control.zoom({ position: 'bottomright' }).addTo(AppState.homeMap);

  // 2. Active Ride Live Tracking Map (100% Free OpenStreetMap - No API key needed)
  AppState.activeMap = L.map('active-tracking-map', {
    zoomControl: false,
    attributionControl: false
  }).setView(LPU_COORDS, 16);

  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19,
    attribution: '&copy; OpenStreetMap contributors'
  }).addTo(AppState.activeMap);
}

function renderMapFallback(elementId, label) {
  const mapElement = document.getElementById(elementId);
  if (!mapElement || mapElement.querySelector('.map-fallback')) return;

  mapElement.innerHTML = `
    <div class="map-fallback" role="img" aria-label="${label}">
      <div class="map-fallback-road road-horizontal"></div>
      <div class="map-fallback-road road-vertical"></div>
      <div class="map-fallback-area area-academic">Academic Blocks</div>
      <div class="map-fallback-area area-mall">Uni-Mall</div>
      <div class="map-fallback-area area-hostels">Hostels</div>
      <div class="map-fallback-pin pin-campus">LPU</div>
      <div class="map-fallback-caption">${label} • LPU Campus</div>
    </div>
  `;
}

// --- Load Landmarks from API ---
async function loadLandmarks() {
  try {
    const res = await fetch('/api/campus/landmarks');
    const data = await res.json();
    AppState.landmarks = data.landmarks;
    populateLocationDropdowns();
    populatePlannedRouteDropdowns();
    renderMapLandmarks();
  } catch (err) {
    console.error('Failed to load landmarks:', err);
  }
}

// --- Populate Dropdowns based on Scope ---
function syncCustomLocationDropdowns() {
  document.querySelectorAll('.location-dropdown').forEach((select) => {
    const wrapper = select.parentElement?.querySelector('.custom-location-picker');
    const selectedOption = Array.from(select.options).find(option => option.value === select.value);
    const label = selectedOption ? selectedOption.textContent : 'Select a location';

    if (wrapper) {
      const triggerValue = wrapper.querySelector('.custom-location-value');
      if (triggerValue) triggerValue.textContent = label;

      wrapper.querySelectorAll('.custom-location-option').forEach((optionButton) => {
        optionButton.classList.toggle('active', optionButton.dataset.value === select.value);
      });
    }
  });
}

function initializeCustomLocationDropdowns() {
  if (!document || !document.querySelectorAll) return;

  document.querySelectorAll('.location-dropdown').forEach((select) => {
    if (select.dataset.customized === 'true') return;

    const wrapper = document.createElement('div');
    wrapper.className = 'custom-location-picker';
    wrapper.style.zIndex = select.id === 'pickup-select' || select.id === 'route-origin-select' ? '80' : '70';

    const trigger = document.createElement('button');
    trigger.type = 'button';
    trigger.className = 'custom-location-trigger';
    trigger.innerHTML = '<span class="custom-location-value"></span><span class="custom-location-chevron">▾</span>';

    const menu = document.createElement('div');
    menu.className = 'custom-location-menu';

    const buildMenu = () => {
      menu.innerHTML = '';
      Array.from(select.options).forEach((option) => {
        const optionButton = document.createElement('button');
        optionButton.type = 'button';
        optionButton.className = 'custom-location-option';
        optionButton.dataset.value = option.value;
        optionButton.textContent = option.textContent;
        optionButton.classList.toggle('active', option.value === select.value);

        optionButton.addEventListener('click', (event) => {
          event.preventDefault();
          event.stopPropagation();
          select.value = option.value;
          syncCustomLocationDropdowns();
          menu.classList.remove('open');
          trigger.classList.remove('open');
          select.dispatchEvent(new Event('change', { bubbles: true }));
        });

        menu.appendChild(optionButton);
      });

      const selectedOption = Array.from(select.options).find(option => option.value === select.value);
      trigger.querySelector('.custom-location-value').textContent = selectedOption ? selectedOption.textContent : 'Select a location';
    };

    const positionMenu = () => {
      const triggerRect = trigger.getBoundingClientRect();
      const spaceBelow = window.innerHeight - triggerRect.bottom - 20;
      const spaceAbove = triggerRect.top - 20;

      if (spaceBelow < 220 && spaceAbove > 220) {
        menu.style.top = 'auto';
        menu.style.bottom = 'calc(100% + 8px)';
      } else {
        menu.style.top = 'calc(100% + 8px)';
        menu.style.bottom = 'auto';
      }

      const maxMenuHeight = Math.min(280, Math.max(180, spaceBelow > 220 ? spaceBelow : spaceAbove));
      menu.style.maxHeight = `${maxMenuHeight}px`;
    };

    trigger.addEventListener('click', (event) => {
      event.preventDefault();
      event.stopPropagation();

      const shouldOpen = !menu.classList.contains('open');
      document.querySelectorAll('.custom-location-menu').forEach((openMenu) => {
        openMenu.classList.remove('open');
        const otherTrigger = openMenu.parentElement?.querySelector('.custom-location-trigger');
        if (otherTrigger) otherTrigger.classList.remove('open');
      });

      menu.classList.toggle('open', shouldOpen);
      trigger.classList.toggle('open', shouldOpen);
      if (shouldOpen) positionMenu();
    });

    document.addEventListener('click', (event) => {
      if (!wrapper.contains(event.target)) {
        menu.classList.remove('open');
        trigger.classList.remove('open');
      }
    });

    if (select.parentElement) {
      select.parentElement.insertBefore(wrapper, select);
    }
    wrapper.appendChild(trigger);
    wrapper.appendChild(menu);
    select.style.display = 'none';
    select.dataset.customized = 'true';

    buildMenu();
  });
}

function populateLocationDropdowns() {
  document.querySelectorAll('.custom-location-picker').forEach((menuWrapper) => menuWrapper.remove());
  document.querySelectorAll('.location-dropdown').forEach((select) => {
    select.dataset.customized = 'false';
    select.style.display = 'none';
  });

  const pickupSelect = document.getElementById('pickup-select');
  const dropSelect = document.getElementById('drop-select');

  pickupSelect.innerHTML = '';
  dropSelect.innerHTML = '';

  const isHop = AppState.selectedScope === 'campus_hop';

  Object.entries(AppState.landmarks).forEach(([key, loc]) => {
    const isCity = loc.zone.startsWith('CityLink');
    if ((isHop && !isCity) || (!isHop)) {
      const optP = document.createElement('option');
      optP.value = key;
      optP.textContent = `${loc.name} (${loc.zone})`;
      pickupSelect.appendChild(optP);

      const optD = document.createElement('option');
      optD.value = key;
      optD.textContent = `${loc.name} (${loc.zone})`;
      dropSelect.appendChild(optD);
    }
  });

  pickupSelect.value = AppState.pickupKey || 'uni_mall';
  if (isHop) {
    dropSelect.value = AppState.dropKey || 'block_34';
  } else {
    dropSelect.value = 'jalandhar_bus_stand';
    AppState.dropKey = 'jalandhar_bus_stand';
  }

  setTimeout(() => {
    initializeCustomLocationDropdowns();
    syncCustomLocationDropdowns();
  }, 0);
  fetchFareQuotes();
}

function populatePlannedRouteDropdowns() {
  const originSelect = document.getElementById('route-origin-select');
  const destinationSelect = document.getElementById('route-dest-select');
  if (!originSelect || !destinationSelect || !Object.keys(AppState.landmarks).length) return;

  const currentOrigin = originSelect.value;
  const currentDestination = destinationSelect.value;
  originSelect.innerHTML = '';
  destinationSelect.innerHTML = '';

  Object.entries(AppState.landmarks).forEach(([key, location]) => {
    const option = document.createElement('option');
    option.value = location.name;
    option.textContent = `${location.name} (${location.zone})`;

    if (location.zone.startsWith('CityLink')) {
      destinationSelect.appendChild(option);
    } else {
      originSelect.appendChild(option);
    }
  });

  if (Array.from(originSelect.options).some(option => option.value === currentOrigin)) {
    originSelect.value = currentOrigin;
  }
  if (Array.from(destinationSelect.options).some(option => option.value === currentDestination)) {
    destinationSelect.value = currentDestination;
  }

  originSelect.dataset.customized = 'false';
  destinationSelect.dataset.customized = 'false';
  originSelect.style.display = 'none';
  destinationSelect.style.display = 'none';
  document.querySelectorAll('#modal-post-route .custom-location-picker').forEach((picker) => picker.remove());
  initializeCustomLocationDropdowns();
  syncCustomLocationDropdowns();
}

// --- Render Map Pins & Drivers on Home Map ---
function renderMapLandmarks() {
  if (!AppState.homeMap) {
    renderMapFallback('campus-map', 'Campus map preview');
    return;
  }

  // Clear existing markers
  Object.values(AppState.homeMarkers).forEach(m => AppState.homeMap.removeLayer(m));
  AppState.homeMarkers = {};

  // Custom Icon Helpers
  const createPinIcon = (color, label) => L.divIcon({
    className: 'custom-map-pin',
    html: `<div style="background:${color}; width:24px; height:24px; border-radius:50%; border:2px solid white; box-shadow:0 2px 6px rgba(0,0,0,0.3); display:flex; align-items:center; justify-content:center; color:white; font-size:10px; font-weight:bold;">${label}</div>`,
    iconSize: [24, 24],
    iconAnchor: [12, 12]
  });

  // Add Pickup Pin
  const p = AppState.landmarks[AppState.pickupKey];
  if (p) {
    AppState.homeMarkers.pickup = L.marker([p.lat, p.lng], {
      icon: createPinIcon('#10B981', 'P')
    }).addTo(AppState.homeMap).bindPopup(`<b>Pickup:</b> ${p.name}`);
  }

  // Add Drop Pin
  const d = AppState.landmarks[AppState.dropKey];
  if (d) {
    AppState.homeMarkers.drop = L.marker([d.lat, d.lng], {
      icon: createPinIcon('#FF7C00', 'D')
    }).addTo(AppState.homeMap).bindPopup(`<b>Destination:</b> ${d.name}`);
  }

  // Add active simulated drivers near LPU
  const mockDrivers = [
    { name: 'Simran (Scooty)', lat: 31.2530, lng: 75.7042, type: 'scooty' },
    { name: 'Vikram (Bike)', lat: 31.2558, lng: 75.7048, type: 'bike' },
    { name: 'Harpreet (Car)', lat: 31.2515, lng: 75.7075, type: 'car' }
  ];

  mockDrivers.forEach((dr, idx) => {
    const iconChar = dr.type === 'bike' ? '🏍' : dr.type === 'scooty' ? '🛵' : '🚗';
    const drMarker = L.marker([dr.lat, dr.lng], {
      icon: L.divIcon({
        className: 'driver-car-pin',
        html: `<div style="background:#111827; width:28px; height:28px; border-radius:50%; border:2px solid #FF7C00; display:flex; align-items:center; justify-content:center; font-size:14px; box-shadow:0 3px 8px rgba(0,0,0,0.35);">${iconChar}</div>`,
        iconSize: [28, 28],
        iconAnchor: [14, 14]
      })
    }).addTo(AppState.homeMap).bindPopup(`<b>${dr.name}</b><br>Available`);
    AppState.homeMarkers[`driver_${idx}`] = drMarker;
  });
}

// --- Fetch 100% Server-Side Fare Quotes ---
async function fetchFareQuotes() {
  if (!AppState.currentUser) return;

  const pickupKey = document.getElementById('pickup-select').value;
  const dropKey = document.getElementById('drop-select').value;
  AppState.pickupKey = pickupKey;
  AppState.dropKey = dropKey;

  renderMapLandmarks();

  try {
    const res = await fetch('/api/rides/quote', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        user_id: AppState.currentUser.id,
        pickup_key: pickupKey,
        drop_key: dropKey,
        scope: AppState.selectedScope
      })
    });

    const data = await res.json();
    if (!res.ok) {
      console.warn('Quote error:', data.error);
      return;
    }

    AppState.quotes = data.quotes;
    updateServicesDisplay(data);
  } catch (err) {
    console.error('Failed to fetch fare quotes:', err);
  }
}

// --- Update Exactly 3 Vehicle Service Cards ---
function updateServicesDisplay(data) {
  const quotes = data.quotes;
  const walletBal = data.wallet_balance;

  // Pricing Mode Pill
  const modePill = document.getElementById('pricing-mode-tag');
  if (AppState.selectedScope === 'campus_hop') {
    modePill.textContent = 'Flat Campus Hop';
    modePill.style.backgroundColor = 'var(--primary-orange-light)';
  } else {
    modePill.textContent = 'Dynamic CityLink';
    modePill.style.backgroundColor = '#FEF3C7';
  }

  // Update Services: Bike, Scooty, Car
  ['bike', 'scooty', 'car'].forEach(srv => {
    const q = quotes[srv];
    if (!q) return;

    document.getElementById(`fare-${srv}`).textContent = q.total_fare.toFixed(0);
    document.getElementById(`eta-${srv}`).textContent = `~${q.estimated_minutes} min`;
    document.getElementById(`avail-${srv}`).textContent = `${q.available_drivers} driver${q.available_drivers === 1 ? '' : 's'} near`;
  });

  // Selected Service summary text on Primary CTA
  const selectedQuote = quotes[AppState.selectedService];
  if (selectedQuote) {
    const sName = AppState.selectedService.toUpperCase();
    const sType = AppState.selectedScope === 'campus_hop' ? 'Flat' : 'Est.';
    document.getElementById('selected-service-summary').textContent = `${sName} • ${sType} ₹${selectedQuote.total_fare.toFixed(0)}`;

    // Wallet warning check
    const warningBanner = document.getElementById('wallet-warning-banner');
    if (!selectedQuote.has_sufficient_balance) {
      warningBanner.classList.remove('hidden');
      document.getElementById('banner-deficit-amount').textContent = selectedQuote.deficit.toFixed(0);
    } else {
      warningBanner.classList.add('hidden');
    }
  }
}

// --- Load Personas & Auth ---
async function loadPersonas() {
  try {
    const res = await fetch('/api/auth/personas');
    const data = await res.json();
    AppState.personas = data.personas;

    // Render persona selector in profile
    const listEl = document.getElementById('persona-selector-list');
    listEl.innerHTML = '';

    AppState.personas.forEach(p => {
      const chip = document.createElement('div');
      chip.className = `persona-chip ${AppState.currentUser && AppState.currentUser.id === p.id ? 'active' : ''}`;
      chip.innerHTML = `
        <img src="${p.avatar_url || 'https://images.unsplash.com/photo-1539571696357-5a69c17a67c6?w=120'}" alt="${p.name}">
        <div>
          <span class="chip-name">${p.name} ${p.is_teacher_priority ? '⭐' : ''}</span>
          <span class="chip-role">${p.user_type.toUpperCase()} • ${p.role.toUpperCase()} (₹${p.wallet_balance})</span>
        </div>
      `;
      chip.onclick = () => switchPersona(p.id);
      listEl.appendChild(chip);
    });

    // Default to Aarav Mehta (Student Rider) or Raman Sharma (Teacher)
    if (!AppState.currentUser) {
      await switchPersona('usr_student_aarav');
    }
  } catch (err) {
    console.error('Failed to load personas:', err);
  }
}

// --- Switch Persona Handler ---
async function switchPersona(userId) {
  try {
    const res = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ user_id: userId })
    });

    const data = await res.json();
    AppState.currentUser = data.user;
    updateUserUI();
    await loadEmergencyContacts();
    await loadDriverEarnings();
    fetchFareQuotes();
    await checkActiveRide();
  } catch (err) {
    console.error('Failed to login persona:', err);
  }
}

// --- Update UI with Current User Context ---
function updateUserUI() {
  const u = AppState.currentUser;
  if (!u) return;

  // Header
  document.getElementById('header-wallet-amount').textContent = u.wallet_balance.toFixed(2);
  document.getElementById('header-avatar').src = u.avatar_url || 'https://images.unsplash.com/photo-1539571696357-5a69c17a67c6?w=120';

  const badgeEl = document.getElementById('header-badge');
  if (u.is_teacher_priority) {
    badgeEl.classList.remove('hidden');
    document.getElementById('header-badge-text').textContent = 'Faculty Priority';
  } else {
    badgeEl.classList.add('hidden');
  }

  // Profile Card
  document.getElementById('profile-card-name').textContent = u.name;
  document.getElementById('profile-card-avatar').src = u.avatar_url;
  document.getElementById('profile-card-department').textContent = u.department || 'Lovely Professional University';
  document.getElementById('profile-card-id').textContent = `LPU ID: ${u.lpu_id}`;

  // Role Buttons
  document.querySelectorAll('.role-pill-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.role === u.role);
  });

  // Driver Section
  const driverSec = document.getElementById('driver-dashboard-section');
  if (u.role === 'driver' || u.role === 'both') {
    driverSec.classList.remove('hidden');
    if (u.vehicle) {
      document.getElementById('veh-model-plate').textContent = `${u.vehicle.model} • ${u.vehicle.plate_number}`;
      document.getElementById('veh-specs').textContent = `${u.vehicle.color} • ${u.vehicle.has_helmet ? 'Helmet Included' : ''} ${u.vehicle.has_ac ? 'AC Equipped' : ''}`;
    }
  } else {
    driverSec.classList.add('hidden');
  }
}

// --- Mandatory Emergency Contacts ---
async function loadEmergencyContacts() {
  if (!AppState.currentUser) return;
  try {
    const res = await fetch(`/api/user/emergency-contacts?user_id=${AppState.currentUser.id}`);
    const data = await res.json();
    const container = document.getElementById('emergency-contacts-list');
    container.innerHTML = '';

    data.contacts.forEach(c => {
      const el = document.createElement('div');
      el.className = 'contact-item';
      el.innerHTML = `
        <div>
          <div class="contact-name">${c.name} ${c.is_primary ? '🛡️ (Primary)' : ''}</div>
          <div class="contact-rel">${c.relationship}</div>
        </div>
        <div class="contact-phone">${c.phone}</div>
      `;
      container.appendChild(el);
    });
  } catch (err) {
    console.error('Failed to load emergency contacts:', err);
  }
}

// --- Check Active Ride / During-Ride Session on Startup / Persona Switch ---
async function checkActiveRide() {
  if (!AppState.currentUser) return;
  try {
    // 1. Check for active scheduled carpool during-ride cockpit
    const sessRes = await fetch(`/api/user/active-session?user_id=${AppState.currentUser.id}`);
    const sessData = await sessRes.json();

    if (sessData.active_session && sessData.session_type === 'scheduled_route') {
      const route = sessData.active_session;
      if (route.status === 'in_progress') {
        await openDuringRideCockpit(route.route_id);
        return;
      }
    }

    // 2. Check for on-demand active ride
    const res = await fetch(`/api/rides/active?user_id=${AppState.currentUser.id}`);
    const data = await res.json();

    if (data.active_ride) {
      AppState.activeRide = data.active_ride;
      if (data.active_ride.status === 'queued') {
        renderQueueStatus(data.active_ride);
      } else {
        renderActiveRide(data.active_ride);
        switchView('view-active-ride');
      }
    } else {
      AppState.activeRide = null;
      AppState.activeCockpitRouteId = null;
      document.getElementById('queue-status-card').classList.add('hidden');
      document.getElementById('nav-queue-dot').classList.add('hidden');
    }
  } catch (err) {
    console.error('Failed to check active ride:', err);
  }
}

// --- Book Ride Handler ---
async function handleBookRide() {
  if (!AppState.currentUser) return;

  const quote = AppState.quotes ? AppState.quotes[AppState.selectedService] : null;
  if (!quote) return;

  // Pre-check wallet balance on client before submitting
  if (!quote.has_sufficient_balance) {
    openModal('modal-topup');
    document.getElementById('topup-custom-input').value = Math.ceil(quote.deficit + 20);
    return;
  }

  const btn = document.getElementById('find-ride-btn');
  btn.disabled = true;
  document.getElementById('find-ride-text').textContent = 'Matching...';

  try {
    const res = await fetch('/api/rides/book', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        user_id: AppState.currentUser.id,
        pickup_key: AppState.pickupKey,
        drop_key: AppState.dropKey,
        service_type: AppState.selectedService,
        scope: AppState.selectedScope
      })
    });

    const data = await res.json();
    btn.disabled = false;
    document.getElementById('find-ride-text').textContent = 'Find Ride';

    if (!res.ok) {
      if (res.status === 402) { // Insufficient Balance
        openModal('modal-topup');
        document.getElementById('topup-custom-input').value = Math.ceil(data.deficit + 20);
      } else {
        alert(data.error || 'Failed to book ride');
      }
      return;
    }

    if (data.status === 'queued') {
      alert(`Peak-time rush: You are queued at position #${data.queue_position}.${data.is_priority ? ' (Teacher Priority Applied ⭐)' : ''}`);
      await checkActiveRide();
      switchView('view-activity');
    } else {
      await checkActiveRide();
      switchView('view-active-ride');
    }
  } catch (err) {
    console.error('Book ride failed:', err);
    btn.disabled = false;
    document.getElementById('find-ride-text').textContent = 'Find Ride';
  }
}

// --- Render Queue Status in Activity Tab ---
function renderQueueStatus(ride) {
  const card = document.getElementById('queue-status-card');
  card.classList.remove('hidden');
  document.getElementById('nav-queue-dot').classList.remove('hidden');

  document.getElementById('queue-pos-badge').textContent = `Position #${ride.queue_position || 1}`;
  const prioNote = document.getElementById('queue-priority-note');
  if (ride.is_priority) {
    prioNote.classList.remove('hidden');
  } else {
    prioNote.classList.add('hidden');
  }
}

async function loadActivityHistory() {
  if (!AppState.currentUser) return;

  const container = document.getElementById('transactions-list');
  try {
    const res = await fetch(`/api/wallet?user_id=${AppState.currentUser.id}`);
    const data = await res.json();
    container.innerHTML = '';

    if (!data.transactions || data.transactions.length === 0) {
      container.innerHTML = '<p class="empty-state">No transactions or completed rides yet.</p>';
      return;
    }

    data.transactions.forEach(transaction => {
      const item = document.createElement('div');
      item.className = 'tx-item';
      const amount = Number(transaction.amount || 0);
      const date = new Date(transaction.created_at * 1000).toLocaleDateString();
      item.innerHTML = `
        <div>
          <span class="tx-desc"></span>
          <span class="tx-date">${date}</span>
        </div>
        <strong class="tx-amount ${amount < 0 ? 'negative' : ''}">${amount < 0 ? '-' : '+'}₹${Math.abs(amount).toFixed(2)}</strong>
      `;
      item.querySelector('.tx-desc').textContent = transaction.description || transaction.type;
      container.appendChild(item);
    });
  } catch (err) {
    container.innerHTML = '<p class="empty-state">Activity is temporarily unavailable.</p>';
    console.error('Failed to load activity history:', err);
  }
}

// --- Render Active Ride Tracking Screen ---
function renderActiveRide(ride) {
  document.getElementById('active-trip-fare').textContent = ride.fare.toFixed(2);
  document.getElementById('active-pickup-name').textContent = ride.pickup_name;
  document.getElementById('active-drop-name').textContent = ride.drop_name;
  document.getElementById('cockpit-route-title').textContent = `${ride.pickup_name} ➔ ${ride.drop_name}`;

  const isArriving = ride.status === 'arriving' || ride.status === 'matched';
  document.getElementById('cockpit-live-status-text').textContent = isArriving ? 'DRIVER ARRIVING' : 'RIDE IN PROGRESS';
  document.getElementById('cockpit-telemetry-sub').textContent = isArriving ? 'Driver is heading to your campus pickup point' : 'En route to destination safely';

  // Driver details
  document.getElementById('active-driver-name').textContent = ride.driver_name || 'Assigned Driver';
  if (ride.driver_avatar) {
    document.getElementById('active-driver-avatar').src = ride.driver_avatar;
  }
  document.getElementById('active-vehicle-model').textContent = ride.vehicle_model || 'Vehicle';
  document.getElementById('active-vehicle-plate').textContent = ride.vehicle_plate || 'PB08-XX';

  // Manifest (single rider)
  const manifestBox = document.getElementById('cockpit-passenger-list');
  manifestBox.innerHTML = `
    <span class="passenger-chip">
      <img src="${ride.rider_avatar || 'https://images.unsplash.com/photo-1539571696357-5a69c17a67c6?w=120'}">
      <span class="passenger-chip-name">${ride.rider_name || 'Rider'} (Primary)</span>
    </span>
  `;
  document.getElementById('manifest-count-badge').textContent = '1 Passenger Onboard';

  // Active Map Pins & Polyline
  if (AppState.activeMap) {
    setTimeout(() => AppState.activeMap.invalidateSize(), 200);

    Object.values(AppState.activeMarkers).forEach(m => AppState.activeMap.removeLayer(m));
    AppState.activeMarkers = {};
    if (AppState.activePolyline) {
      AppState.activeMap.removeLayer(AppState.activePolyline);
    }

    const pCoords = [ride.pickup_lat, ride.pickup_lng];
    const dCoords = [ride.drop_lat, ride.drop_lng];
    const drCoords = [ride.driver_live_lat || ride.pickup_lat, ride.driver_live_lng || ride.pickup_lng];

    AppState.activeMarkers.pickup = L.marker(pCoords, {
      icon: L.divIcon({
        className: 'active-pin',
        html: `<div style="background:#10B981; width:22px; height:22px; border-radius:50%; border:2px solid white; display:flex; align-items:center; justify-content:center; color:white; font-size:10px; font-weight:bold;">P</div>`,
        iconSize: [22, 22]
      })
    }).addTo(AppState.activeMap);

    AppState.activeMarkers.drop = L.marker(dCoords, {
      icon: L.divIcon({
        className: 'active-pin',
        html: `<div style="background:#FF7C00; width:22px; height:22px; border-radius:50%; border:2px solid white; display:flex; align-items:center; justify-content:center; color:white; font-size:10px; font-weight:bold;">D</div>`,
        iconSize: [22, 22]
      })
    }).addTo(AppState.activeMap);

    const iconChar = ride.service_type === 'car' ? '🚗' : '🏍️';
    AppState.activeMarkers.driver = L.marker(drCoords, {
      icon: L.divIcon({
        className: 'driver-live-marker',
        html: `<div style="background:#111827; width:32px; height:32px; border-radius:50%; border:3px solid #FF7C00; display:flex; align-items:center; justify-content:center; font-size:16px; box-shadow:0 0 12px rgba(255,124,0,0.5);">${iconChar}</div>`,
        iconSize: [32, 32],
        iconAnchor: [16, 16]
      })
    }).addTo(AppState.activeMap);

    AppState.activePolyline = L.polyline([pCoords, dCoords], {
      color: '#FF7C00',
      weight: 5,
      opacity: 0.8,
      dashArray: '8, 8'
    }).addTo(AppState.activeMap);

    AppState.activeMap.fitBounds([pCoords, dCoords], { padding: [40, 40] });
  }
}

// --- Advance Telemetry Simulation (Scheduled Cockpit OR On-Demand) ---
async function advanceTelemetryStep() {
  if (AppState.activeCockpitRouteId) {
    try {
      const res = await fetch(`/api/routes/${AppState.activeCockpitRouteId}/telemetry-step`, { method: 'POST' });
      const data = await res.json();
      if (data.current_lat && AppState.activeMarkers.car) {
        AppState.activeMarkers.car.setLatLng([data.current_lat, data.current_lng]);
        document.getElementById('cockpit-telemetry-sub').textContent = `${data.distance_remaining_km} km remaining • Moving towards destination`;
      }
    } catch (err) {
      console.error('Failed to advance route telemetry:', err);
    }
    return;
  }

  if (AppState.activeRide) {
    try {
      const res = await fetch(`/api/rides/${AppState.activeRide.id}/telemetry-step`, { method: 'POST' });
      const data = await res.json();
      if (data.status) {
        AppState.activeRide.status = data.status;
        if (AppState.activeMarkers.driver && data.current_lat) {
          AppState.activeMarkers.driver.setLatLng([data.current_lat, data.current_lng]);
        }
        const isArriving = data.status === 'arriving';
        document.getElementById('cockpit-live-status-text').textContent = isArriving ? 'DRIVER ARRIVING' : 'RIDE IN PROGRESS';
        document.getElementById('cockpit-telemetry-sub').textContent = `Distance remaining: ${data.distance_remaining_km} km`;
      }
    } catch (err) {
      console.error('Failed to advance telemetry:', err);
    }
  }
}

// --- Complete Ride & Clean Dashboard Reset ---
async function handleCompleteRide() {
  // Case 1: Scheduled Carpool Cockpit
  if (AppState.activeCockpitRouteId) {
    try {
      const res = await fetch(`/api/routes/${AppState.activeCockpitRouteId}/complete`, { method: 'POST' });
      const data = await res.json();
      if (data.success) {
        alert(`🏁 Carpool trip completed! Host payout of ₹${data.driver_payout} disbursed.\n\nDashboard is now reset to clean idle state for all passengers.`);
        
        // Reset state
        AppState.activeCockpitRouteId = null;
        openModal('modal-rating');
        switchView('view-ride');

        // Refresh user context & scheduled routes
        await switchPersona(AppState.currentUser.id);
        await loadCityLinkRoutes();
      }
    } catch (err) {
      console.error('Failed to complete scheduled route:', err);
    }
    return;
  }

  // Case 2: On-demand Ride
  if (AppState.activeRide) {
    try {
      const res = await fetch(`/api/rides/${AppState.activeRide.id}/complete`, { method: 'POST' });
      const data = await res.json();
      if (data.success) {
        openModal('modal-rating');
        if (AppState.currentUser) {
          AppState.currentUser.wallet_balance = data.rider_balance_after;
          updateUserUI();
        }
        AppState.activeRide = null;
        switchView('view-ride');
        fetchFareQuotes();
      }
    } catch (err) {
      console.error('Failed to complete ride:', err);
    }
  }
}

// --- Submit Rating ---
async function submitRating() {
  if (!AppState.activeRide) return;
  try {
    await fetch(`/api/rides/${AppState.activeRide.id}/rate`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        reviewer_id: AppState.currentUser.id,
        rating: 5,
        tags: 'Safe Ride, Punctual, Verified LPU',
        comment: 'Great campus ride!'
      })
    });

    closeModal('modal-rating');
    AppState.activeRide = null;
    switchView('view-ride');
    fetchFareQuotes();
  } catch (err) {
    console.error('Failed to submit rating:', err);
  }
}

// --- Trigger Persistent SOS with Multi-Channel Alert ---
async function handleTriggerSOS() {
  if (!AppState.currentUser) return;
  const p = AppState.landmarks[AppState.pickupKey] || { lat: 31.2536, lng: 75.7037, name: 'LPU Campus' };

  try {
    const res = await fetch('/api/sos/trigger', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        user_id: AppState.currentUser.id,
        ride_id: AppState.activeRide ? AppState.activeRide.id : null,
        lat: p.lat,
        lng: p.lng,
        location_name: p.name
      })
    });

    const data = await res.json();
    if (data.success) {
      document.getElementById('sos-display-location').textContent = p.name;
      document.getElementById('sos-contacts-count').textContent = `${data.contacts_notified.length} Contacts Dispatched`;
      
      // Render real SMS message payload
      if (data.sos_message) {
        document.getElementById('sos-sms-text').textContent = data.sos_message;
      }
      
      // Render delivery records
      const listEl = document.getElementById('sos-dispatched-recipients');
      listEl.innerHTML = '';
      data.contacts_notified.forEach(c => {
        const row = document.createElement('div');
        row.className = 'sos-recipient-item';
        row.innerHTML = `
          <div>
            <strong>${c.contact_name}</strong> (${c.relationship}) • <span style="font-family:var(--font-mono)">${c.contact_phone}</span>
          </div>
          <span class="check">✓ ${c.delivery.status}</span>
        `;
        listEl.appendChild(row);
      });

      openModal('modal-sos');
    }
  } catch (err) {
    console.error('Failed to trigger SOS:', err);
  }
}

// --- Share Trip Link ---
function handleShareTrip() {
  if (!AppState.activeRide) return;
  const token = AppState.activeRide.share_token || 'share_demo123';
  const url = `${window.location.origin}/api/rides/share/${token}`;
  document.getElementById('share-link-input').value = url;
  openModal('modal-share-trip');
}

// --- Enhanced Payment Handlers: UPI QR Code & Razorpay ---
let currentUpiRef = null;

async function fetchUpiQr(amount) {
  if (!AppState.currentUser) return;
  const container = document.getElementById('upi-qr-container');
  container.innerHTML = '<div class="qr-loading-spinner">Generating UPI QR...</div>';
  document.getElementById('qr-pay-amount-label').textContent = amount;
  document.getElementById('rzp-pay-amount-label').textContent = amount;

  try {
    const res = await fetch('/api/payments/upi/create-qr', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        user_id: AppState.currentUser.id,
        amount: amount
      })
    });

    const data = await res.json();
    if (data.success) {
      currentUpiRef = data.reference_id;
      container.innerHTML = data.svg_qr;
      document.getElementById('upi-vpa-text').textContent = data.vpa;
      const intentBtn = document.getElementById('open-upi-intent-btn');
      intentBtn.href = data.upi_uri;
    } else {
      container.innerHTML = `<span style="color:red; font-size:0.7rem;">Error: ${data.error}</span>`;
    }
  } catch (err) {
    console.error('Failed to generate UPI QR:', err);
    container.innerHTML = '<span style="color:red; font-size:0.7rem;">Failed to load QR</span>';
  }
}

async function handleConfirmUpiPayment() {
  if (!AppState.currentUser) return;
  const amount = parseFloat(document.getElementById('topup-custom-input').value) || 100;

  try {
    const res = await fetch('/api/payments/upi/confirm', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        user_id: AppState.currentUser.id,
        amount: amount,
        reference_id: currentUpiRef || `UPI_${Date.now()}`
      })
    });

    const data = await res.json();
    if (data.success) {
      AppState.currentUser.wallet_balance = data.new_balance;
      updateUserUI();
      closeModal('modal-topup');
      fetchFareQuotes();
      alert(`Payment of ₹${amount} confirmed via UPI! New balance: ₹${data.new_balance.toFixed(2)}`);
    } else {
      alert(data.error || 'Payment confirmation failed');
    }
  } catch (err) {
    console.error('UPI payment error:', err);
  }
}

async function handleRazorpayCheckout() {
  if (!AppState.currentUser) return;
  const amount = parseFloat(document.getElementById('topup-custom-input').value) || 100;

  try {
    // 1. Create order
    const orderRes = await fetch('/api/payments/razorpay/create-order', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        user_id: AppState.currentUser.id,
        amount: amount
      })
    });
    const orderData = await orderRes.json();

    if (!orderData.success) {
      alert(orderData.error || 'Failed to initialize Razorpay order');
      return;
    }

    // 2. Verify payment (simulated / test mode)
    const verifyRes = await fetch('/api/payments/razorpay/verify', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        user_id: AppState.currentUser.id,
        order_id: orderData.order_id,
        payment_id: `pay_${Date.now()}`,
        signature: 'demo_signature_valid',
        amount: amount
      })
    });

    const verifyData = await verifyRes.json();
    if (verifyData.success) {
      AppState.currentUser.wallet_balance = verifyData.new_balance;
      updateUserUI();
      closeModal('modal-topup');
      fetchFareQuotes();
      alert(`Razorpay checkout verified! ₹${amount} credited. New balance: ₹${verifyData.new_balance.toFixed(2)}`);
    } else {
      alert(verifyData.error || 'Verification failed');
    }
  } catch (err) {
    console.error('Razorpay payment error:', err);
  }
}

// --- Driver Dashboard & CityLink Carpooling ---
async function loadDriverEarnings() {
  if (!AppState.currentUser) return;
  try {
    const res = await fetch(`/api/driver/earnings?driver_id=${AppState.currentUser.id}`);
    const data = await res.json();
    document.getElementById('driver-total-earned').textContent = data.total_earnings.toFixed(2);
    document.getElementById('driver-total-trips').textContent = data.total_trips;
    document.getElementById('driver-rating-val').textContent = data.avg_rating.toFixed(1);
  } catch (err) {
    console.error('Failed to load driver earnings:', err);
  }
}

// --- Scheduled Carpool Routes & Automatic Pinning Dashboard ---
async function loadCityLinkRoutes() {
  if (!AppState.currentUser) return;
  try {
    const res = await fetch(`/api/routes/scheduled?user_id=${AppState.currentUser.id}`);
    const data = await res.json();
    const routes = data.routes || [];

    const pinnedContainer = document.getElementById('pinned-routes-container');
    const openContainer = document.getElementById('citylink-routes-container');
    pinnedContainer.innerHTML = '';
    openContainer.innerHTML = '';

    const pinnedRoutes = routes.filter(r => r.status === 'pinned' || r.status === 'in_progress' || r.available_seats === 0);
    const openRoutes = routes.filter(r => r.status === 'open' && r.available_seats > 0);

    // 1. Render Pinned & Full Routes
    if (pinnedRoutes.length === 0) {
      pinnedContainer.innerHTML = '<p style="font-size:0.75rem; color:var(--text-muted); padding:8px 0;">No pinned routes currently. Scheduled rides automatically pin here once all seats fill up!</p>';
    } else {
      pinnedRoutes.forEach(r => {
        const card = document.createElement('div');
        card.className = 'pinned-route-card';
        
        let passengerChips = r.passengers.map(p => `
          <span class="passenger-chip">
            <img src="${p.passenger_avatar || 'https://images.unsplash.com/photo-1539571696357-5a69c17a67c6?w=120'}">
            <span class="passenger-chip-name">${p.passenger_name}</span>
            <span class="passenger-chip-seats">(${p.seats} seat${p.seats > 1 ? 's' : ''})</span>
          </span>
        `).join('');

        let actionButtonHtml = '';
        if (r.status === 'in_progress') {
          actionButtonHtml = `<button class="btn-start-cockpit" onclick="openDuringRideCockpit('${r.id}')">🟢 Live Trip Active • Enter Cockpit</button>`;
        } else if (r.is_host) {
          actionButtonHtml = `<button class="btn-start-cockpit" onclick="startConfirmedRoute('${r.id}')">🚀 Start Confirmed Ride (Launch Cockpit)</button>`;
        } else if (r.has_joined) {
          actionButtonHtml = `<button class="btn-view-cockpit" onclick="openDuringRideCockpit('${r.id}')">👀 View Confirmed Ride Cockpit</button>`;
        } else {
          actionButtonHtml = `<div style="font-size:0.75rem; color:#B45309; font-weight:700; text-align:center; padding-top:6px;">🔒 Ride is fully booked</div>`;
        }

        card.innerHTML = `
          <div class="pinned-banner-header">
            <span class="pinned-pill">📌 PINNED & FULL</span>
            <span class="pinned-seats-full">All ${r.total_seats} Seats Booked</span>
          </div>
          <div class="route-card-header">
            <div>
              <div class="route-dest-title">${r.origin || 'LPU'} ➔ ${r.destination}</div>
              <div style="font-size:0.75rem; color:var(--text-secondary);">Departure: <strong>${r.departure_time}</strong></div>
            </div>
            <div class="route-price-tag">₹${r.price_per_seat.toFixed(0)} <span style="font-size:0.6rem; color:var(--text-muted);">/seat</span></div>
          </div>
          <div class="route-driver-row">
            <img class="route-driver-avatar" src="${r.driver_avatar || 'https://images.unsplash.com/photo-1500648767791-00dcc994a43e?w=120'}">
            <span class="route-driver-text"><strong>${r.driver_name}</strong> • ${r.vehicle_model || 'Car'} (${r.vehicle_plate || 'PB08'})</span>
          </div>
          <div style="margin-top:8px;">
            <div style="font-size:0.68rem; font-weight:800; color:var(--text-secondary); margin-bottom:4px;">PASSENGERS ONBOARD:</div>
            <div class="passenger-chips-row">${passengerChips || '<span style="font-size:0.7rem; color:var(--text-muted);">Reserved</span>'}</div>
          </div>
          ${actionButtonHtml}
        `;
        pinnedContainer.appendChild(card);
      });
    }

    // 2. Render Open Scheduled Routes (Passengers can select and book seats)
    if (openRoutes.length === 0) {
      openContainer.innerHTML = '<p style="text-align:center; color:var(--text-muted); font-size:0.8rem; padding:16px;">No open carpools at the moment. Use "+ Plan a Route" to offer seats!</p>';
    } else {
      openRoutes.forEach(r => {
        const card = document.createElement('div');
        card.className = 'route-card';
        
        let passengerChips = r.passengers.length > 0 ? r.passengers.map(p => `
          <span class="passenger-chip">
            <img src="${p.passenger_avatar || 'https://images.unsplash.com/photo-1539571696357-5a69c17a67c6?w=120'}">
            <span class="passenger-chip-name">${p.passenger_name}</span>
          </span>
        `).join('') : '<span style="font-size:0.68rem; color:var(--text-muted);">No passengers yet</span>';

        card.innerHTML = `
          <div class="route-card-header">
            <div>
              <div class="route-dest-title">${r.origin || 'LPU'} ➔ ${r.destination}</div>
              <div style="font-size:0.75rem; color:var(--text-secondary);">Departure: <strong>${r.departure_time}</strong></div>
            </div>
            <div class="route-price-tag">₹${r.price_per_seat.toFixed(0)} <span style="font-size:0.6rem; color:var(--text-muted);">/seat</span></div>
          </div>
          <div class="route-driver-row">
            <img class="route-driver-avatar" src="${r.driver_avatar || 'https://images.unsplash.com/photo-1500648767791-00dcc994a43e?w=120'}">
            <span class="route-driver-text"><strong>${r.driver_name}</strong> • ${r.vehicle_model || 'Vehicle'}</span>
          </div>
          <div style="margin: 6px 0;">
            <div style="font-size:0.65rem; font-weight:700; color:var(--text-muted);">JOINED PASSENGERS:</div>
            <div class="passenger-chips-row" style="margin-top:2px;">${passengerChips}</div>
          </div>
          <div class="route-card-footer">
            <span class="route-seats-pill">${r.available_seats} of ${r.total_seats} seats remaining</span>
            ${r.is_host ? '<span style="font-size:0.75rem; font-weight:700; color:var(--primary-orange);">You are the Host</span>' : 
              `<div style="display:flex; gap:6px; align-items:center;">
                <select class="seat-picker-select" id="seat-pick-${r.id}" style="border:1px solid var(--border-light); border-radius:4px; padding:3px 6px; font-size:0.75rem;">
                  ${Array.from({length: r.available_seats}, (_, i) => `<option value="${i+1}">${i+1} Seat${i>0?'s':''}</option>`).join('')}
                </select>
                <button class="btn-join-route" data-id="${r.id}" data-price="${r.price_per_seat}">Book Seat</button>
              </div>`
            }
          </div>
        `;
        openContainer.appendChild(card);
      });

      // Attach join handlers
      openContainer.querySelectorAll('.btn-join-route').forEach(btn => {
        btn.onclick = () => {
          const routeId = btn.dataset.id;
          const price = parseFloat(btn.dataset.price);
          const sel = document.getElementById(`seat-pick-${routeId}`);
          const seats = sel ? parseInt(sel.value) : 1;
          joinCityLinkRoute(routeId, seats, price * seats);
        };
      });
    }
  } catch (err) {
    console.error('Failed to load scheduled carpool dashboard:', err);
  }
}

async function joinCityLinkRoute(routeId, seats, totalFare) {
  if (!AppState.currentUser) return;
  if (AppState.currentUser.wallet_balance < totalFare) {
    openModal('modal-topup');
    document.getElementById('topup-custom-input').value = Math.ceil(totalFare - AppState.currentUser.wallet_balance + 10);
    return;
  }

  try {
    const res = await fetch(`/api/routes/${routeId}/join`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        rider_id: AppState.currentUser.id,
        seats: seats
      })
    });

    const data = await res.json();
    if (data.success) {
      AppState.currentUser.wallet_balance -= totalFare;
      updateUserUI();

      if (data.is_pinned) {
        alert(`🎉 ALL SEATS ARE NOW FULL!\n\nThis scheduled ride has been automatically PINNED and is confirmed for departure.`);
      } else {
        alert(`Confirmed! ${seats} seat(s) booked for ₹${totalFare}. ${data.remaining_seats} seat(s) remaining until ride pins.`);
      }

      await loadCityLinkRoutes();
    } else {
      alert(data.error || 'Could not join route');
    }
  } catch (err) {
    console.error('Join route error:', err);
  }
}

async function postCityLinkRoute() {
  if (!AppState.currentUser) return;
  const origin = document.getElementById('route-origin-select').value;
  const destination = document.getElementById('route-dest-select').value;
  const departure_time = document.getElementById('route-time-input').value;
  const total_seats = parseInt(document.getElementById('route-seats-input').value) || 3;
  const price_per_seat = parseFloat(document.getElementById('route-price-input').value) || 80;
  const notes = document.getElementById('route-notes-input').value;

  try {
    const res = await fetch('/api/routes/plan', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        driver_id: AppState.currentUser.id,
        origin,
        destination,
        departure_time,
        total_seats,
        price_per_seat,
        notes
      })
    });

    const data = await res.json();
    if (data.success) {
      closeModal('modal-post-route');
      await loadCityLinkRoutes();
      alert(`Route scheduled! Offering ${total_seats} seats from ${origin} to ${destination}. Once all seats are booked, it will automatically get pinned!`);
    }
  } catch (err) {
    console.error('Failed to plan route:', err);
  }
}

// --- During-Ride Cockpit Dashboard Lifecycle ---
AppState.activeCockpitRouteId = null;

async function startConfirmedRoute(routeId) {
  try {
    const res = await fetch(`/api/routes/${routeId}/start`, { method: 'POST' });
    const data = await res.json();
    if (data.success) {
      await openDuringRideCockpit(routeId);
    }
  } catch (err) {
    console.error('Failed to start confirmed route:', err);
  }
}

async function openDuringRideCockpit(routeId) {
  try {
    const res = await fetch(`/api/routes/${routeId}/live`);
    const data = await res.json();
    if (!data.cockpit) return;

    const cockpit = data.cockpit;
    AppState.activeCockpitRouteId = routeId;

    // Set UI Texts
    document.getElementById('cockpit-route-title').textContent = `${cockpit.origin} ➔ ${cockpit.destination}`;
    document.getElementById('cockpit-telemetry-sub').textContent = `${cockpit.distance_remaining_km} km remaining • ETA: ~${cockpit.eta_minutes} mins`;
    document.getElementById('active-trip-fare').textContent = cockpit.price_per_seat.toFixed(2);
    document.getElementById('active-pickup-name').textContent = cockpit.origin;
    document.getElementById('active-drop-name').textContent = cockpit.destination;

    // Host details
    document.getElementById('active-driver-name').textContent = `${cockpit.driver_name} (Host)`;
    if (cockpit.driver_avatar) document.getElementById('active-driver-avatar').src = cockpit.driver_avatar;
    document.getElementById('active-vehicle-model').textContent = cockpit.vehicle_model || 'Vehicle';
    document.getElementById('active-vehicle-plate').textContent = cockpit.vehicle_plate || 'PB08';

    // Manifest list
    const manifestBox = document.getElementById('cockpit-passenger-list');
    manifestBox.innerHTML = '';
    cockpit.passengers.forEach(p => {
      const chip = document.createElement('span');
      chip.className = 'passenger-chip';
      chip.innerHTML = `
        <img src="${p.passenger_avatar || 'https://images.unsplash.com/photo-1539571696357-5a69c17a67c6?w=120'}">
        <span class="passenger-chip-name">${p.passenger_name} (${p.seats} seat${p.seats>1?'s':''})</span>
      `;
      manifestBox.appendChild(chip);
    });
    document.getElementById('manifest-count-badge').textContent = `${cockpit.passengers.length} Passenger${cockpit.passengers.length===1?'':'s'} Onboard`;

    // Map Rendering
    if (AppState.activeMap) {
      setTimeout(() => AppState.activeMap.invalidateSize(), 200);

      // Clear previous layers
      Object.values(AppState.activeMarkers).forEach(m => AppState.activeMap.removeLayer(m));
      AppState.activeMarkers = {};
      if (AppState.activePolyline) AppState.activeMap.removeLayer(AppState.activePolyline);

      const oCoords = [cockpit.origin_lat, cockpit.origin_lng];
      const dCoords = [cockpit.destination_lat, cockpit.destination_lng];
      const vCoords = [cockpit.current_lat || cockpit.origin_lat, cockpit.current_lng || cockpit.origin_lng];

      AppState.activeMarkers.origin = L.marker(oCoords, {
        icon: L.divIcon({
          className: 'pin-o',
          html: '<div style="background:#10B981; width:22px; height:22px; border-radius:50%; border:2px solid white; display:flex; align-items:center; justify-content:center; color:white; font-size:10px; font-weight:bold;">P</div>',
          iconSize: [22, 22]
        })
      }).addTo(AppState.activeMap);

      AppState.activeMarkers.drop = L.marker(dCoords, {
        icon: L.divIcon({
          className: 'pin-d',
          html: '<div style="background:#FF7C00; width:22px; height:22px; border-radius:50%; border:2px solid white; display:flex; align-items:center; justify-content:center; color:white; font-size:10px; font-weight:bold;">D</div>',
          iconSize: [22, 22]
        })
      }).addTo(AppState.activeMap);

      AppState.activeMarkers.car = L.marker(vCoords, {
        icon: L.divIcon({
          className: 'pin-car',
          html: '<div style="background:#111827; width:34px; height:34px; border-radius:50%; border:3px solid #F59E0B; display:flex; align-items:center; justify-content:center; font-size:18px; box-shadow:0 0 14px rgba(245,158,11,0.6);">🚗</div>',
          iconSize: [34, 34],
          iconAnchor: [17, 17]
        })
      }).addTo(AppState.activeMap);

      AppState.activePolyline = L.polyline([oCoords, dCoords], {
        color: '#F59E0B',
        weight: 5,
        opacity: 0.85,
        dashArray: '8, 8'
      }).addTo(AppState.activeMap);

      AppState.activeMap.fitBounds([oCoords, dCoords], { padding: [50, 50] });
    }

    switchView('view-active-ride');
  } catch (err) {
    console.error('Failed to open during-ride cockpit:', err);
  }
}

// --- Navigation View Switcher ---
function initNavigation() {
  document.querySelectorAll('.nav-tab').forEach(tab => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('.nav-tab').forEach(t => t.classList.remove('active'));
      tab.classList.add('active');
      switchView(tab.dataset.view);
    });
  });
}

function switchView(viewId) {
  document.querySelectorAll('.app-view').forEach(v => v.classList.remove('active-view'));
  const target = document.getElementById(viewId);
  if (target) {
    target.classList.add('active-view');
  }

  // Update tab bar active state
  document.querySelectorAll('.nav-tab').forEach(t => {
    t.classList.toggle('active', t.dataset.view === viewId);
  });

  if (viewId === 'view-citylink') {
    loadCityLinkRoutes();
  } else if (viewId === 'view-activity') {
    loadActivityHistory();
  } else if (viewId === 'view-ride' && AppState.homeMap) {
    setTimeout(() => AppState.homeMap.invalidateSize(), 150);
  } else if (viewId === 'view-active-ride' && AppState.activeMap) {
    setTimeout(() => AppState.activeMap.invalidateSize(), 150);
  }
}

// --- Modals Management ---
function initModals() {
  document.querySelectorAll('.modal-close-btn, .modal-backdrop').forEach(btn => {
    btn.addEventListener('click', () => {
      const modal = btn.closest('.app-modal');
      if (modal) modal.classList.add('hidden');
    });
  });

  document.querySelectorAll('.amount-chip').forEach(chip => {
    chip.addEventListener('click', () => {
      document.querySelectorAll('.amount-chip').forEach(c => c.classList.remove('active'));
      chip.classList.add('active');
      document.getElementById('topup-custom-input').value = chip.dataset.amount;
    });
  });
}

function openModal(id) {
  const modal = document.getElementById(id);
  if (modal) {
    modal.classList.remove('hidden');
    if (id === 'modal-topup' && AppState.currentUser) {
      document.getElementById('modal-current-bal').textContent = AppState.currentUser.wallet_balance.toFixed(2);
      const amt = parseFloat(document.getElementById('topup-custom-input').value) || 100;
      fetchUpiQr(amt);
    }
    if (id === 'modal-post-route') {
      setTimeout(() => {
        populatePlannedRouteDropdowns();
        initializeCustomLocationDropdowns();
        syncCustomLocationDropdowns();
      }, 0);
    }
  }
}

function closeModal(id) {
  const modal = document.getElementById(id);
  if (modal) modal.classList.add('hidden');
}

// --- Event Handlers Setup ---
function initEventHandlers() {
  // Top-Up Payment Method Tabs
  document.querySelectorAll('.pay-method-tab').forEach(tab => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('.pay-method-tab').forEach(t => t.classList.remove('active'));
      tab.classList.add('active');
      const method = tab.dataset.method;
      if (method === 'upi') {
        document.getElementById('pay-panel-upi').classList.remove('hidden');
        document.getElementById('pay-panel-razorpay').classList.add('hidden');
      } else {
        document.getElementById('pay-panel-upi').classList.add('hidden');
        document.getElementById('pay-panel-razorpay').classList.remove('hidden');
      }
    });
  });

  // Amount input change triggers QR refresh
  document.getElementById('topup-custom-input').addEventListener('input', (e) => {
    const val = parseFloat(e.target.value);
    if (val > 0) fetchUpiQr(val);
  });

  // UPI and Razorpay Buttons
  document.getElementById('confirm-upi-paid-btn').addEventListener('click', handleConfirmUpiPayment);
  document.getElementById('razorpay-checkout-btn').addEventListener('click', handleRazorpayCheckout);

  // Amount Chips
  document.querySelectorAll('.amount-chip').forEach(chip => {
    chip.addEventListener('click', () => {
      document.querySelectorAll('.amount-chip').forEach(c => c.classList.remove('active'));
      chip.classList.add('active');
      const amt = parseFloat(chip.dataset.amount);
      document.getElementById('topup-custom-input').value = amt;
      fetchUpiQr(amt);
    });
  });

  // Scope switcher: Campus Hop vs CityLink
  document.querySelectorAll('.scope-tab').forEach(tab => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('.scope-tab').forEach(t => t.classList.remove('active'));
      tab.classList.add('active');
      AppState.selectedScope = tab.dataset.scope;
      populateLocationDropdowns();
    });
  });

  // Exactly 3 Services: Bike, Scooty, Car selection
  document.querySelectorAll('.service-card').forEach(card => {
    card.addEventListener('click', () => {
      document.querySelectorAll('.service-card').forEach(c => c.classList.remove('active'));
      card.classList.add('active');
      AppState.selectedService = card.dataset.service;
      if (AppState.quotes) {
        updateServicesDisplay({ quotes: AppState.quotes, wallet_balance: AppState.currentUser.wallet_balance });
      }
    });
  });

  // Dropdown changes
  document.getElementById('pickup-select').addEventListener('change', fetchFareQuotes);
  document.getElementById('drop-select').addEventListener('change', fetchFareQuotes);

  // Swap Locations
  document.getElementById('swap-locations-btn').addEventListener('click', () => {
    const p = document.getElementById('pickup-select');
    const d = document.getElementById('drop-select');
    const temp = p.value;
    p.value = d.value;
    d.value = temp;
    syncCustomLocationDropdowns();
    fetchFareQuotes();
  });

  // Quick Top-up Button
  document.getElementById('quick-topup-btn').addEventListener('click', () => openModal('modal-topup'));
  document.getElementById('header-wallet-btn').addEventListener('click', () => openModal('modal-topup'));
  const confirmTopupButton = document.getElementById('confirm-topup-btn');
  if (confirmTopupButton) {
    confirmTopupButton.addEventListener('click', handleWalletTopup);
  }

  // Persona quick menu
  document.getElementById('persona-switch-btn').addEventListener('click', () => switchView('view-profile'));

  // Recenter map
  document.getElementById('recenter-map-btn').addEventListener('click', () => {
    if (AppState.homeMap) AppState.homeMap.setView([31.2536, 75.7037], 16);
  });

  // Primary CTA: Find Ride
  document.getElementById('find-ride-btn').addEventListener('click', handleBookRide);

  // Persistent SOS
  document.getElementById('persistent-sos-btn').addEventListener('click', handleTriggerSOS);
  document.getElementById('dismiss-sos-btn').addEventListener('click', () => closeModal('modal-sos'));

  // Share Live Trip
  document.getElementById('share-live-btn').addEventListener('click', handleShareTrip);
  document.getElementById('copy-share-btn').addEventListener('click', () => {
    const input = document.getElementById('share-link-input');
    input.select();
    navigator.clipboard.writeText(input.value);
    alert('Live tracking link copied to clipboard!');
  });

  // Active Ride Controls
  document.getElementById('sim-telemetry-btn').addEventListener('click', advanceTelemetryStep);
  document.getElementById('complete-ride-btn').addEventListener('click', handleCompleteRide);
  document.getElementById('submit-rating-btn').addEventListener('click', submitRating);

  // CityLink Post Route
  document.getElementById('open-plan-route-btn').addEventListener('click', () => openModal('modal-post-route'));
  document.getElementById('submit-post-route-btn').addEventListener('click', postCityLinkRoute);

  // Emergency contact modal
  document.getElementById('add-emergency-btn').addEventListener('click', () => openModal('modal-add-contact'));
  document.getElementById('submit-contact-btn').addEventListener('click', async () => {
    const name = document.getElementById('emg-name-input').value;
    const relationship = document.getElementById('emg-rel-input').value;
    const phone = document.getElementById('emg-phone-input').value;
    if (!name || !phone) return;

    await fetch('/api/user/emergency-contacts', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        user_id: AppState.currentUser.id,
        name,
        relationship,
        phone,
        is_primary: 0
      })
    });
    closeModal('modal-add-contact');
    await loadEmergencyContacts();
  });

  // Role selector
  document.querySelectorAll('.role-pill-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      const newRole = btn.dataset.role;
      await fetch('/api/user/role', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          user_id: AppState.currentUser.id,
          role: newRole
        })
      });
      AppState.currentUser.role = newRole;
      updateUserUI();
    });
  });
}
