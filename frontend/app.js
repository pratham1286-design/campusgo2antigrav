/**
 * CampusGo - LPU Community Transit Application Controller
 * Leaflet map, 3 vehicle services, server-held wallet, zone queueing,
 * teacher priority, SOS, carpools and the driver dashboard.
 *
 * Security rule for this file: server data is never inserted as raw HTML.
 * Use textContent, or esc() for every value inside an HTML template string.
 */

const DEFAULT_AVATAR = 'https://images.unsplash.com/photo-1539571696357-5a69c17a67c6?w=120';
const LPU_COORDS = [31.2536, 75.7037];

const AppState = {
  currentUser: null,
  authToken: null,
  config: null,
  landmarks: {},
  selectedScope: 'campus_hop', // 'campus_hop' | 'citylink'
  selectedService: 'bike',     // 'bike' | 'scooty' | 'car'
  pickupKey: 'uni_mall',
  dropKey: 'block_34',
  quotes: null,
  activeRide: null,
  activeCockpitRouteId: null,
  activeCockpitIsHost: false,
  ratingRideId: null,
  ratingValue: 5,
  currentUpiRef: null,
  homeMap: null,
  activeMap: null,
  homeMarkers: {},
  driverMarkers: [],
  activeMarkers: {},
  activePolyline: null,
  timers: { drivers: null, queue: null, qr: null }
};

// --- Small helpers ---
function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
}

function safeImageUrl(url) {
  return typeof url === 'string' && /^https:\/\//i.test(url) ? url : DEFAULT_AVATAR;
}

function money(value) {
  return Number(value || 0).toFixed(2);
}

function $(id) {
  return document.getElementById(id);
}

// --- Initialization ---
document.addEventListener('DOMContentLoaded', async () => {
  initNavigation();
  initModals();
  initMaps();
  initializeCustomLocationDropdowns();
  await loadConfig();
  await loadLandmarks();
  initEventHandlers();
  initAuthHandlers();
  await tryResumeSession();
});

async function loadConfig() {
  try {
    const res = await fetch('/api/config');
    AppState.config = await res.json();
  } catch (err) {
    AppState.config = { security_hotline: '+91 1824 517000', razorpay_enabled: false, payments_demo_mode: false };
  }
  const hotline = AppState.config.security_hotline;
  const link = $('sos-hotline-link');
  link.textContent = `Call ${hotline}`;
  link.href = `tel:${hotline.replace(/[^\d+]/g, '')}`;
}

// --- Authentication ---
function getStoredToken() {
  try {
    return localStorage.getItem('campusgo_token');
  } catch (err) {
    return null;
  }
}

function storeToken(token) {
  try {
    if (token) {
      localStorage.setItem('campusgo_token', token);
    } else {
      localStorage.removeItem('campusgo_token');
    }
  } catch (err) {
    // localStorage unavailable (private mode etc.) - session just won't persist across reloads
  }
}

// Wraps fetch() to attach the bearer token and parse the JSON reply.
async function apiFetch(url, options = {}) {
  const headers = Object.assign({}, options.headers || {});
  if (AppState.authToken) {
    headers['Authorization'] = `Bearer ${AppState.authToken}`;
  }
  const res = await fetch(url, Object.assign({}, options, { headers }));
  if (res.status === 401 && AppState.authToken) {
    handleLogout();
  }
  return res;
}

// POST helper: returns { ok, status, data } and never throws on HTTP errors.
async function apiPost(url, payload = {}) {
  const res = await apiFetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  });
  let data = {};
  try {
    data = await res.json();
  } catch (err) {
    data = { error: 'Unexpected server response' };
  }
  return { ok: res.ok, status: res.status, data };
}

async function apiGet(url) {
  const res = await apiFetch(url);
  let data = {};
  try {
    data = await res.json();
  } catch (err) {
    data = { error: 'Unexpected server response' };
  }
  return { ok: res.ok, status: res.status, data };
}

function showLoginScreen() {
  $('login-screen').classList.remove('hidden');
}

function hideLoginScreen() {
  $('login-screen').classList.add('hidden');
}

async function tryResumeSession() {
  const token = getStoredToken();
  if (!token) {
    showLoginScreen();
    return;
  }
  AppState.authToken = token;
  try {
    const { ok, data } = await apiGet('/api/auth/me');
    if (!ok) {
      handleLogout();
      return;
    }
    AppState.currentUser = data.user;
    await onLoginSuccess();
  } catch (err) {
    console.error('Failed to resume session:', err);
    handleLogout();
  }
}

async function handleLogin() {
  const lpuId = $('login-lpu-id-input').value.trim();
  const password = $('login-password-input').value;
  const errorEl = $('login-error-text');
  const btn = $('login-submit-btn');
  errorEl.classList.add('hidden');

  if (!lpuId || !password) {
    errorEl.textContent = 'Enter your LPU ID and password.';
    errorEl.classList.remove('hidden');
    return;
  }

  btn.disabled = true;
  btn.textContent = 'Signing In...';

  try {
    const res = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ lpu_id: lpuId, password })
    });
    const data = await res.json();

    if (!res.ok) {
      errorEl.textContent = data.error || 'Login failed';
      errorEl.classList.remove('hidden');
      return;
    }

    AppState.authToken = data.token;
    AppState.currentUser = data.user;
    storeToken(data.token);
    $('login-password-input').value = '';
    await onLoginSuccess();
  } catch (err) {
    console.error('Login failed:', err);
    errorEl.textContent = 'Could not reach the server. Please try again.';
    errorEl.classList.remove('hidden');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Log In';
  }
}

function clearTimers() {
  Object.keys(AppState.timers).forEach((key) => {
    clearInterval(AppState.timers[key]);
    clearTimeout(AppState.timers[key]);
    AppState.timers[key] = null;
  });
}

function handleLogout() {
  clearTimers();
  AppState.authToken = null;
  AppState.currentUser = null;
  AppState.activeRide = null;
  AppState.activeCockpitRouteId = null;
  AppState.ratingRideId = null;
  storeToken(null);
  showLoginScreen();
}

async function onLoginSuccess() {
  hideLoginScreen();
  updateUserUI();
  await loadEmergencyContacts();
  await loadDriverEarnings();
  fetchFareQuotes();
  refreshNearbyDrivers();
  clearInterval(AppState.timers.drivers);
  AppState.timers.drivers = setInterval(refreshNearbyDrivers, 30000);
  await checkActiveRide();
}

// Re-fetches the user's own record (wallet, role, vehicle) after changes.
async function refreshCurrentUser() {
  try {
    const { ok, data } = await apiGet('/api/auth/me');
    if (!ok) return;
    AppState.currentUser = data.user;
    updateUserUI();
  } catch (err) {
    console.error('Failed to refresh current user:', err);
  }
}

function setWalletBalance(balance) {
  if (!AppState.currentUser || typeof balance !== 'number') return;
  AppState.currentUser.wallet_balance = balance;
  updateUserUI();
}

function initAuthHandlers() {
  $('login-submit-btn').addEventListener('click', handleLogin);
  $('login-password-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') handleLogin();
  });
  $('logout-btn').addEventListener('click', handleLogout);
}

window.initializeCustomLocationDropdowns = initializeCustomLocationDropdowns;
window.syncCustomLocationDropdowns = syncCustomLocationDropdowns;

// --- Map Initialization (OpenStreetMap, no API key needed) ---
function initMaps() {
  if (typeof L === 'undefined') {
    renderMapFallback('campus-map', 'Campus map preview');
    renderMapFallback('active-tracking-map', 'Live tracking map');
    return;
  }

  AppState.homeMap = L.map('campus-map', { zoomControl: false, attributionControl: false }).setView(LPU_COORDS, 16);
  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19,
    attribution: '&copy; OpenStreetMap contributors'
  }).addTo(AppState.homeMap);
  L.control.zoom({ position: 'bottomright' }).addTo(AppState.homeMap);

  AppState.activeMap = L.map('active-tracking-map', { zoomControl: false, attributionControl: false }).setView(LPU_COORDS, 16);
  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19,
    attribution: '&copy; OpenStreetMap contributors'
  }).addTo(AppState.activeMap);
}

function renderMapFallback(elementId, label) {
  const mapElement = $(elementId);
  if (!mapElement || mapElement.querySelector('.map-fallback')) return;

  mapElement.innerHTML = `
    <div class="map-fallback" role="img" aria-label="${esc(label)}">
      <div class="map-fallback-road road-horizontal"></div>
      <div class="map-fallback-road road-vertical"></div>
      <div class="map-fallback-area area-academic">Academic Blocks</div>
      <div class="map-fallback-area area-mall">Uni-Mall</div>
      <div class="map-fallback-area area-hostels">Hostels</div>
      <div class="map-fallback-pin pin-campus">LPU</div>
      <div class="map-fallback-caption">${esc(label)} • LPU Campus</div>
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


// --- Map pins for pickup/drop and real online drivers ---
function pinIcon(color, label, size = 24) {
  return L.divIcon({
    className: 'custom-map-pin',
    html: `<div style="background:${color}; width:${size}px; height:${size}px; border-radius:50%; border:2px solid white; box-shadow:0 2px 6px rgba(0,0,0,0.3); display:flex; align-items:center; justify-content:center; color:white; font-size:10px; font-weight:bold;">${esc(label)}</div>`,
    iconSize: [size, size],
    iconAnchor: [size / 2, size / 2]
  });
}

function vehicleIcon(category, size = 28, border = '#FF7C00') {
  const iconChar = category === 'bike' ? '🏍' : category === 'scooty' ? '🛵' : '🚗';
  return L.divIcon({
    className: 'driver-car-pin',
    html: `<div style="background:#111827; width:${size}px; height:${size}px; border-radius:50%; border:2px solid ${border}; display:flex; align-items:center; justify-content:center; font-size:14px; box-shadow:0 3px 8px rgba(0,0,0,0.3);">${iconChar}</div>`,
    iconSize: [size, size],
    iconAnchor: [size / 2, size / 2]
  });
}

function renderMapLandmarks() {
  if (!AppState.homeMap) {
    renderMapFallback('campus-map', 'Campus map preview');
    return;
  }

  Object.values(AppState.homeMarkers).forEach((m) => AppState.homeMap.removeLayer(m));
  AppState.homeMarkers = {};

  const p = AppState.landmarks[AppState.pickupKey];
  if (p) {
    AppState.homeMarkers.pickup = L.marker([p.lat, p.lng], { icon: pinIcon('#10B981', 'P') })
      .addTo(AppState.homeMap).bindPopup(`<b>Pickup:</b> ${esc(p.name)}`);
  }
  const d = AppState.landmarks[AppState.dropKey];
  if (d) {
    AppState.homeMarkers.drop = L.marker([d.lat, d.lng], { icon: pinIcon('#FF7C00', 'D') })
      .addTo(AppState.homeMap).bindPopup(`<b>Destination:</b> ${esc(d.name)}`);
  }
}

// Shows drivers who are actually online and free, as reported by the server.
async function refreshNearbyDrivers() {
  if (!AppState.homeMap || !AppState.currentUser) return;
  try {
    const { ok, data } = await apiGet('/api/drivers/nearby');
    if (!ok) return;
    AppState.driverMarkers.forEach((m) => AppState.homeMap.removeLayer(m));
    AppState.driverMarkers = data.drivers.map((dr) => L.marker([dr.lat, dr.lng], { icon: vehicleIcon(dr.category) })
      .addTo(AppState.homeMap)
      .bindPopup(`<b>${esc(dr.first_name)}</b> (${esc(dr.category)})<br>Available`));
  } catch (err) {
    console.error('Failed to load nearby drivers:', err);
  }
}

// --- Server-side fare quotes ---
async function fetchFareQuotes() {
  if (!AppState.currentUser) return;

  const pickupKey = $('pickup-select').value;
  const dropKey = $('drop-select').value;
  AppState.pickupKey = pickupKey;
  AppState.dropKey = dropKey;
  renderMapLandmarks();

  try {
    const { ok, data } = await apiPost('/api/rides/quote', {
      pickup_key: pickupKey,
      drop_key: dropKey,
      scope: AppState.selectedScope
    });
    if (!ok) {
      AppState.quotes = null;
      $('selected-service-summary').textContent = data.error || 'Choose a valid pickup and drop';
      $('wallet-warning-banner').classList.add('hidden');
      return;
    }
    AppState.quotes = data.quotes;
    updateServicesDisplay(data);
  } catch (err) {
    console.error('Failed to fetch fare quotes:', err);
  }
}

function updateServicesDisplay(data) {
  const quotes = data.quotes;
  const modePill = $('pricing-mode-tag');
  if (AppState.selectedScope === 'campus_hop') {
    modePill.textContent = 'Flat Campus Hop';
    modePill.style.backgroundColor = 'var(--primary-orange-light)';
  } else {
    modePill.textContent = 'Dynamic CityLink';
    modePill.style.backgroundColor = '#FEF3C7';
  }

  ['bike', 'scooty', 'car'].forEach((srv) => {
    const q = quotes[srv];
    if (!q) return;
    $(`fare-${srv}`).textContent = q.total_fare.toFixed(0);
    $(`eta-${srv}`).textContent = `~${q.estimated_minutes} min`;
    $(`avail-${srv}`).textContent = `${q.available_drivers} driver${q.available_drivers === 1 ? '' : 's'} online`;
  });

  const selectedQuote = quotes[AppState.selectedService];
  if (selectedQuote) {
    const sType = AppState.selectedScope === 'campus_hop' ? 'Flat' : 'Est.';
    $('selected-service-summary').textContent = `${AppState.selectedService.toUpperCase()} • ${sType} ₹${selectedQuote.total_fare.toFixed(0)}`;
    const warningBanner = $('wallet-warning-banner');
    if (!selectedQuote.has_sufficient_balance) {
      warningBanner.classList.remove('hidden');
      $('banner-deficit-amount').textContent = selectedQuote.deficit.toFixed(0);
    } else {
      warningBanner.classList.add('hidden');
    }
  }
}

// --- User profile UI ---
function updateUserUI() {
  const u = AppState.currentUser;
  if (!u) return;

  $('header-wallet-amount').textContent = money(u.wallet_balance);
  $('header-avatar').src = safeImageUrl(u.avatar_url);

  const badgeEl = $('header-badge');
  if (u.is_teacher_priority) {
    badgeEl.classList.remove('hidden');
    $('header-badge-text').textContent = 'Faculty Priority';
  } else {
    badgeEl.classList.add('hidden');
  }

  $('profile-card-name').textContent = u.name;
  $('profile-card-avatar').src = safeImageUrl(u.avatar_url);
  $('profile-card-department').textContent = u.department || 'Lovely Professional University';
  $('profile-card-id').textContent = `LPU ID: ${u.lpu_id}`;
  $('profile-account-lpu').textContent = `Signed in as ${u.lpu_id} (${u.email})`;

  document.querySelectorAll('.role-pill-btn').forEach((btn) => {
    btn.classList.toggle('active', btn.dataset.role === u.role);
  });

  const driverSec = $('driver-dashboard-section');
  if (u.role === 'driver' || u.role === 'both') {
    driverSec.classList.remove('hidden');
    if (u.vehicle) {
      const extras = [u.vehicle.color, u.vehicle.has_helmet ? 'Helmet Included' : '', u.vehicle.has_ac ? 'AC Equipped' : '']
        .filter(Boolean).join(' • ');
      $('veh-model-plate').textContent = `${u.vehicle.model} • ${u.vehicle.plate_number}`;
      $('veh-specs').textContent = `${u.vehicle.category.toUpperCase()} • ${extras}`;
      $('edit-vehicle-btn').textContent = 'Edit';
    } else {
      $('veh-model-plate').textContent = 'No vehicle registered';
      $('veh-specs').textContent = 'Add your vehicle to go online and accept rides';
      $('edit-vehicle-btn').textContent = 'Add Vehicle';
    }
  } else {
    driverSec.classList.add('hidden');
  }
}

// --- Emergency contacts ---
async function loadEmergencyContacts() {
  if (!AppState.currentUser) return;
  try {
    const { ok, data } = await apiGet('/api/user/emergency-contacts');
    if (!ok) return;
    const container = $('emergency-contacts-list');
    container.replaceChildren();

    data.contacts.forEach((c) => {
      const el = document.createElement('div');
      el.className = 'contact-item';
      el.innerHTML = `
        <div>
          <div class="contact-name">${esc(c.name)} ${c.is_primary ? '🛡️ (Primary)' : ''}</div>
          <div class="contact-rel">${esc(c.relationship)}</div>
        </div>
        <div class="contact-phone">${esc(c.phone)}</div>
        <button class="btn-link contact-remove-btn" data-id="${esc(c.id)}" title="Remove contact">✕</button>
      `;
      container.appendChild(el);
    });
  } catch (err) {
    console.error('Failed to load emergency contacts:', err);
  }
}

async function handleAddContact() {
  const errorEl = $('emg-error-text');
  errorEl.classList.add('hidden');
  const name = $('emg-name-input').value.trim();
  const phone = $('emg-phone-input').value.trim();
  if (!name || !phone) {
    errorEl.textContent = 'Name and phone number are required.';
    errorEl.classList.remove('hidden');
    return;
  }
  const { ok, data } = await apiPost('/api/user/emergency-contacts', {
    name,
    relationship: $('emg-rel-input').value.trim(),
    phone
  });
  if (!ok) {
    errorEl.textContent = data.error || 'Could not save contact';
    errorEl.classList.remove('hidden');
    return;
  }
  ['emg-name-input', 'emg-rel-input', 'emg-phone-input'].forEach((id) => { $(id).value = ''; });
  closeModal('modal-add-contact');
  await loadEmergencyContacts();
}

async function handleRemoveContact(contactId) {
  if (!confirm('Remove this emergency contact?')) return;
  const res = await apiFetch(`/api/user/emergency-contacts/${encodeURIComponent(contactId)}`, { method: 'DELETE' });
  if (!res.ok) {
    alert('Could not remove contact');
    return;
  }
  await loadEmergencyContacts();
}

// --- Active ride / cockpit detection ---
async function checkActiveRide() {
  if (!AppState.currentUser) return;
  try {
    const sess = await apiGet('/api/user/active-session');
    if (sess.ok && sess.data.active_session && sess.data.session_type === 'scheduled_route'
        && sess.data.active_session.status === 'in_progress') {
      await openDuringRideCockpit(sess.data.active_session.route_id);
      return;
    }

    const { ok, data } = await apiGet('/api/rides/active');
    if (!ok) return;

    if (data.active_ride) {
      AppState.activeRide = data.active_ride;
      if (data.active_ride.status === 'queued') {
        renderQueueStatus(data.active_ride);
      } else {
        hideQueueStatus();
        renderActiveRide(data.active_ride);
        switchView('view-active-ride');
      }
    } else {
      AppState.activeRide = null;
      hideQueueStatus();
    }
  } catch (err) {
    console.error('Failed to check active ride:', err);
  }
}

// --- Booking ---
async function handleBookRide() {
  if (!AppState.currentUser) return;
  const quote = AppState.quotes ? AppState.quotes[AppState.selectedService] : null;
  if (!quote) {
    alert($('selected-service-summary').textContent || 'Choose a valid pickup and drop first.');
    return;
  }
  if (!quote.has_sufficient_balance) {
    $('topup-custom-input').value = Math.ceil(quote.deficit + 20);
    openModal('modal-topup');
    return;
  }

  const btn = $('find-ride-btn');
  btn.disabled = true;
  $('find-ride-text').textContent = 'Matching...';

  try {
    const { ok, status, data } = await apiPost('/api/rides/book', {
      pickup_key: AppState.pickupKey,
      drop_key: AppState.dropKey,
      service_type: AppState.selectedService,
      scope: AppState.selectedScope
    });

    if (!ok) {
      if (status === 402) {
        $('topup-custom-input').value = Math.ceil((data.deficit || 0) + 20);
        openModal('modal-topup');
      } else if (data.code === 'CONCURRENT_RIDE_EXISTS') {
        alert('You already have a ride in progress. Check the Activity tab to view or cancel it.');
        await checkActiveRide();
        switchView('view-activity');
      } else {
        alert(data.error || 'Failed to book ride');
      }
      return;
    }

    setWalletBalance(data.wallet_balance);
    if (data.status === 'queued') {
      alert(`All nearby drivers are busy. You are #${data.queue_position} in the queue.${data.is_priority ? ' (Teacher Priority Applied ⭐)' : ''}\nYour fare is held and fully refunded if you cancel.`);
      await checkActiveRide();
      switchView('view-activity');
    } else {
      await checkActiveRide();
      switchView('view-active-ride');
    }
  } catch (err) {
    console.error('Book ride failed:', err);
    alert('Could not reach the server. Please try again.');
  } finally {
    btn.disabled = false;
    $('find-ride-text').textContent = 'Find Ride';
  }
}

// --- Queue card (Activity tab) ---
function renderQueueStatus(ride) {
  $('queue-status-card').classList.remove('hidden');
  $('nav-queue-dot').classList.remove('hidden');
  $('queue-pos-badge').textContent = `Position #${ride.queue_position || 1}`;
  $('queue-priority-note').classList.toggle('hidden', !ride.is_priority);

  // Poll so the rider moves to the live view as soon as a driver accepts.
  if (!AppState.timers.queue) {
    AppState.timers.queue = setInterval(checkActiveRide, 10000);
  }
}

function hideQueueStatus() {
  $('queue-status-card').classList.add('hidden');
  $('nav-queue-dot').classList.add('hidden');
  clearInterval(AppState.timers.queue);
  AppState.timers.queue = null;
}

async function handleCancelRide() {
  const ride = AppState.activeRide;
  if (!ride) return;
  const isRider = ride.is_rider !== false;
  const question = isRider
    ? 'Cancel this ride? Your held fare will be refunded in full.'
    : 'Release this ride? It will go back to the queue for another driver.';
  if (!confirm(question)) return;

  const { ok, data } = await apiPost(`/api/rides/${encodeURIComponent(ride.id)}/cancel`);
  if (!ok) {
    alert(data.error || 'Could not cancel the ride');
    return;
  }
  AppState.activeRide = null;
  hideQueueStatus();
  if (typeof data.wallet_balance === 'number') setWalletBalance(data.wallet_balance);
  switchView(isRider ? 'view-ride' : 'view-profile');
  fetchFareQuotes();
  if (!isRider) loadDriverRequests();
}

// --- Activity / wallet history ---
async function loadActivityHistory() {
  if (!AppState.currentUser) return;
  const container = $('transactions-list');
  try {
    const { ok, data } = await apiGet('/api/wallet');
    if (!ok) throw new Error(data.error);
    container.replaceChildren();
    setWalletBalance(data.balance);

    if (!data.transactions || data.transactions.length === 0) {
      container.innerHTML = '<p class="empty-state">No transactions or completed rides yet.</p>';
      return;
    }

    data.transactions.forEach((transaction) => {
      const item = document.createElement('div');
      item.className = 'tx-item';
      const amount = Number(transaction.amount || 0);
      const date = new Date(transaction.created_at * 1000).toLocaleDateString();
      item.innerHTML = `
        <div>
          <span class="tx-desc"></span>
          <span class="tx-date">${esc(date)}</span>
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

// --- Live ride screen ---
function passengerChip(name, avatar, suffix = '') {
  const chip = document.createElement('span');
  chip.className = 'passenger-chip';
  chip.innerHTML = `
    <img src="${esc(safeImageUrl(avatar))}" alt="">
    <span class="passenger-chip-name">${esc(name)}${suffix ? ` ${esc(suffix)}` : ''}</span>
  `;
  return chip;
}

function updateRideControls(status, isRider) {
  const cancelBtn = $('cancel-ride-btn');
  const completeBtn = $('complete-ride-btn');
  const cancellable = ['queued', 'matched', 'arriving'].includes(status);
  cancelBtn.classList.toggle('hidden', !cancellable);
  cancelBtn.querySelector('span').textContent = isRider ? 'Cancel Ride (Refund)' : 'Release Ride';
  completeBtn.classList.remove('hidden');
  completeBtn.disabled = status !== 'in_progress';
  completeBtn.title = status === 'in_progress' ? '' : 'Available after pickup';
}

function renderActiveRide(ride) {
  AppState.activeCockpitRouteId = null;
  $('active-trip-fare').textContent = money(ride.fare);
  $('active-pickup-name').textContent = ride.pickup_name;
  $('active-drop-name').textContent = ride.drop_name;
  $('cockpit-route-title').textContent = `${ride.pickup_name} ➔ ${ride.drop_name}`;

  const isArriving = ride.status === 'arriving' || ride.status === 'matched';
  $('cockpit-live-status-text').textContent = isArriving ? 'DRIVER ARRIVING' : 'RIDE IN PROGRESS';
  $('cockpit-telemetry-sub').textContent = isArriving ? 'Driver is heading to your pickup point' : 'En route to destination';

  $('active-driver-name').textContent = ride.driver_name || 'Assigned Driver';
  $('active-driver-avatar').src = safeImageUrl(ride.driver_avatar);
  $('active-vehicle-model').textContent = ride.vehicle_model || 'Vehicle';
  $('active-vehicle-plate').textContent = ride.vehicle_plate || '—';

  const manifestBox = $('cockpit-passenger-list');
  manifestBox.replaceChildren(passengerChip(ride.rider_name || 'Rider', ride.rider_avatar, '(Rider)'));
  $('manifest-count-badge').textContent = '1 Passenger';

  updateRideControls(ride.status, ride.is_rider !== false);
  $('share-live-btn').classList.toggle('hidden', !ride.share_token);

  if (AppState.activeMap) {
    setTimeout(() => AppState.activeMap.invalidateSize(), 200);
    clearActiveMap();

    const pCoords = [ride.pickup_lat, ride.pickup_lng];
    const dCoords = [ride.drop_lat, ride.drop_lng];
    const drCoords = [ride.driver_live_lat || ride.pickup_lat, ride.driver_live_lng || ride.pickup_lng];

    AppState.activeMarkers.pickup = L.marker(pCoords, { icon: pinIcon('#10B981', 'P', 22) }).addTo(AppState.activeMap);
    AppState.activeMarkers.drop = L.marker(dCoords, { icon: pinIcon('#FF7C00', 'D', 22) }).addTo(AppState.activeMap);
    AppState.activeMarkers.driver = L.marker(drCoords, { icon: vehicleIcon(ride.service_type, 32) }).addTo(AppState.activeMap);
    AppState.activePolyline = L.polyline([pCoords, dCoords], { color: '#FF7C00', weight: 5, opacity: 0.8, dashArray: '8, 8' })
      .addTo(AppState.activeMap);
    AppState.activeMap.fitBounds([pCoords, dCoords], { padding: [40, 40] });
  }
}

function clearActiveMap() {
  Object.values(AppState.activeMarkers).forEach((m) => AppState.activeMap.removeLayer(m));
  AppState.activeMarkers = {};
  if (AppState.activePolyline) {
    AppState.activeMap.removeLayer(AppState.activePolyline);
    AppState.activePolyline = null;
  }
}

// --- Simulation step (scheduled cockpit OR on-demand ride) ---
async function advanceTelemetryStep() {
  if (AppState.activeCockpitRouteId) {
    const { ok, data } = await apiPost(`/api/routes/${encodeURIComponent(AppState.activeCockpitRouteId)}/telemetry-step`);
    if (!ok) {
      alert(data.error || 'Could not update the trip');
      return;
    }
    if (AppState.activeMarkers.car) AppState.activeMarkers.car.setLatLng([data.current_lat, data.current_lng]);
    $('cockpit-telemetry-sub').textContent = `${data.distance_remaining_km} km remaining • Moving towards destination`;
    return;
  }

  if (!AppState.activeRide) return;
  const { ok, data } = await apiPost(`/api/rides/${encodeURIComponent(AppState.activeRide.id)}/telemetry-step`);
  if (!ok) {
    alert(data.error || 'Could not update the ride');
    return;
  }
  AppState.activeRide.status = data.status;
  if (AppState.activeMarkers.driver && data.current_lat) {
    AppState.activeMarkers.driver.setLatLng([data.current_lat, data.current_lng]);
  }
  const isArriving = data.status === 'arriving' || data.status === 'matched';
  $('cockpit-live-status-text').textContent = isArriving ? 'DRIVER ARRIVING' : 'RIDE IN PROGRESS';
  if (data.distance_remaining_km !== undefined) {
    $('cockpit-telemetry-sub').textContent = `${isArriving ? 'Driver is' : 'Destination is'} ${data.distance_remaining_km} km away`;
  }
  updateRideControls(data.status, AppState.activeRide.is_rider !== false);
}

// --- Completing trips ---
async function handleCompleteRide() {
  if (AppState.activeCockpitRouteId) {
    const { ok, data } = await apiPost(`/api/routes/${encodeURIComponent(AppState.activeCockpitRouteId)}/complete`);
    if (!ok) {
      alert(data.error || 'Could not complete the trip');
      return;
    }
    alert(`🏁 Carpool trip completed! Host payout of ₹${money(data.driver_payout)} added to your wallet.`);
    AppState.activeCockpitRouteId = null;
    switchView('view-citylink');
    await refreshCurrentUser();
    await loadDriverEarnings();
    return;
  }

  if (!AppState.activeRide) return;
  const rideId = AppState.activeRide.id;
  const { ok, data } = await apiPost(`/api/rides/${encodeURIComponent(rideId)}/complete`);
  if (!ok) {
    alert(data.error || 'Could not complete the ride');
    return;
  }
  AppState.activeRide = null;
  switchView('view-ride');
  await refreshCurrentUser();
  await loadDriverEarnings();
  fetchFareQuotes();
  openRatingModal(rideId);
}

// --- Rating ---
function openRatingModal(rideId) {
  AppState.ratingRideId = rideId;
  setRating(5);
  document.querySelectorAll('#modal-rating .tag-pill').forEach((t) => t.classList.remove('active'));
  openModal('modal-rating');
}

function setRating(value) {
  AppState.ratingValue = value;
  document.querySelectorAll('#star-rating-picker .star-btn').forEach((star) => {
    star.classList.toggle('active', Number(star.dataset.val) <= value);
  });
}

async function submitRating() {
  const rideId = AppState.ratingRideId;
  if (!rideId) {
    closeModal('modal-rating');
    return;
  }
  const tags = Array.from(document.querySelectorAll('#modal-rating .tag-pill.active')).map((t) => t.textContent.trim());
  const { ok, data } = await apiPost(`/api/rides/${encodeURIComponent(rideId)}/rate`, {
    rating: AppState.ratingValue,
    tags: tags.join(', ')
  });
  if (!ok && data.code !== 'ALREADY_RATED') {
    alert(data.error || 'Could not save your rating');
    return;
  }
  AppState.ratingRideId = null;
  closeModal('modal-rating');
}

// --- SOS ---
function getBrowserPosition(timeoutMs = 5000) {
  return new Promise((resolve) => {
    if (!navigator.geolocation) {
      resolve(null);
      return;
    }
    navigator.geolocation.getCurrentPosition(
      (pos) => resolve({ lat: pos.coords.latitude, lng: pos.coords.longitude }),
      () => resolve(null),
      { enableHighAccuracy: true, timeout: timeoutMs, maximumAge: 30000 }
    );
  });
}

async function resolveSosLocation() {
  // 1. The phone's real position, named after the closest landmark.
  const gps = await getBrowserPosition();
  if (gps) {
    const { ok, data } = await apiPost('/api/campus/locate', gps);
    if (ok && data.distance_km < 2) {
      return { ...gps, name: `Near ${data.landmark.name}` };
    }
    return { ...gps, name: `GPS ${gps.lat.toFixed(5)}, ${gps.lng.toFixed(5)}` };
  }
  // 2. Without GPS: the driver's live position during a ride.
  const ride = AppState.activeRide;
  if (ride && ride.driver_live_lat && ride.status === 'in_progress') {
    return { lat: ride.driver_live_lat, lng: ride.driver_live_lng, name: `En route from ${ride.pickup_name} (approx.)` };
  }
  if (ride) {
    return { lat: ride.pickup_lat, lng: ride.pickup_lng, name: `${ride.pickup_name} (approx.)` };
  }
  // 3. Last resort: the pickup selected on the booking form.
  const p = AppState.landmarks[AppState.pickupKey] || { lat: LPU_COORDS[0], lng: LPU_COORDS[1], name: 'LPU Campus' };
  return { lat: p.lat, lng: p.lng, name: `${p.name} (approx., GPS unavailable)` };
}

const DELIVERY_LABELS = {
  sent: '✓ Texted',
  failed: '✗ Sending failed',
  not_sent: 'Not texted',
  invalid_number: 'Invalid number'
};

async function handleTriggerSOS() {
  if (!AppState.currentUser) return;
  const sosBtn = $('persistent-sos-btn');
  sosBtn.disabled = true;
  try {
    const loc = await resolveSosLocation();
    const { ok, data } = await apiPost('/api/sos/trigger', {
      ride_id: AppState.activeRide ? AppState.activeRide.id : null,
      lat: loc.lat,
      lng: loc.lng,
      location_name: loc.name
    });
    if (!ok) {
      alert(`${data.error || 'Could not record the SOS.'}\nCall campus security now: ${AppState.config.security_hotline}`);
      return;
    }

    $('sos-display-location').textContent = loc.name;
    const total = data.contacts_notified.length;
    $('sos-contacts-count').textContent = data.sms_gateway_configured
      ? `${data.contacts_sent} of ${total} contacts texted`
      : `SMS is not set up on this server. Your ${total} contacts were NOT texted. Call them directly.`;
    $('sos-gateway-tag').textContent = data.sms_gateway_configured ? 'SMS gateway active' : 'No SMS gateway';
    $('sos-sms-text').textContent = data.sos_message;
    $('sos-advice-text').textContent = data.campus_security_dispatch.message;

    const hotline = data.campus_security_hotline;
    $('sos-hotline-link').textContent = `Call ${hotline}`;
    $('sos-hotline-link').href = `tel:${hotline.replace(/[^\d+]/g, '')}`;

    const listEl = $('sos-dispatched-recipients');
    listEl.replaceChildren();
    data.contacts_notified.forEach((c) => {
      const row = document.createElement('div');
      row.className = 'sos-recipient-item';
      const status = c.delivery.status;
      row.innerHTML = `
        <div>
          <strong>${esc(c.contact_name)}</strong> (${esc(c.relationship)}) •
          <a href="tel:${esc(String(c.contact_phone).replace(/[^\d+]/g, ''))}" style="font-family:var(--font-mono)">${esc(c.contact_phone)}</a>
        </div>
        <span class="check">${esc(DELIVERY_LABELS[status] || status)}</span>
      `;
      listEl.appendChild(row);
    });

    openModal('modal-sos');
  } catch (err) {
    console.error('Failed to trigger SOS:', err);
    alert(`Could not reach the server. Call campus security now: ${AppState.config.security_hotline}`);
  } finally {
    sosBtn.disabled = false;
  }
}

// --- Share live trip link ---
function handleShareTrip() {
  const ride = AppState.activeRide;
  if (!ride || !ride.share_token) {
    alert('Live sharing is available for on-demand rides.');
    return;
  }
  $('share-link-input').value = `${window.location.origin}/track/${encodeURIComponent(ride.share_token)}`;
  openModal('modal-share-trip');
}

// --- Wallet top-up: UPI QR & Razorpay ---
function setTopupStatus(message) {
  const el = $('topup-status-text');
  el.textContent = message || '';
  el.classList.toggle('hidden', !message);
}

function currentTopupAmount() {
  return parseFloat($('topup-custom-input').value) || 100;
}

function scheduleQrRefresh() {
  clearTimeout(AppState.timers.qr);
  AppState.timers.qr = setTimeout(() => fetchUpiQr(currentTopupAmount()), 400);
}

async function fetchUpiQr(amount) {
  if (!AppState.currentUser) return;
  const container = $('upi-qr-container');
  container.innerHTML = '<div class="qr-loading-spinner">Generating UPI QR...</div>';
  $('qr-pay-amount-label').textContent = amount;
  $('rzp-pay-amount-label').textContent = amount;
  AppState.currentUpiRef = null;

  try {
    const { ok, data } = await apiPost('/api/payments/upi/create-qr', { amount });
    if (!ok) {
      const msg = document.createElement('span');
      msg.style.cssText = 'color:red; font-size:0.7rem;';
      msg.textContent = data.error || 'Could not create QR';
      container.replaceChildren(msg);
      return;
    }
    AppState.currentUpiRef = data.reference_id;
    const img = document.createElement('img');
    img.src = data.qr_data_url;
    img.alt = 'UPI payment QR code';
    img.style.cssText = 'width:100%; height:auto;';
    container.replaceChildren(img);
    $('upi-vpa-text').textContent = data.vpa;
    $('open-upi-intent-btn').href = data.upi_uri.startsWith('upi://') ? data.upi_uri : '#';
  } catch (err) {
    console.error('Failed to generate UPI QR:', err);
    container.textContent = 'Failed to load QR';
  }
}

async function handleConfirmUpiPayment() {
  if (!AppState.currentUser || !AppState.currentUpiRef) return;
  const { ok, status, data } = await apiPost('/api/payments/upi/confirm', { reference_id: AppState.currentUpiRef });
  if (!ok) {
    setTopupStatus(data.error || 'Payment confirmation failed');
    return;
  }
  if (status === 202) {
    setTopupStatus(data.message);
    AppState.currentUpiRef = null;
    return;
  }
  setWalletBalance(data.new_balance);
  closeModal('modal-topup');
  fetchFareQuotes();
  alert(`₹${money(data.amount_credited)} added. New balance: ₹${money(data.new_balance)}`);
}

function loadRazorpayScript() {
  if (window.Razorpay) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const script = document.createElement('script');
    script.src = 'https://checkout.razorpay.com/v1/checkout.js';
    script.onload = resolve;
    script.onerror = () => reject(new Error('Could not load Razorpay checkout'));
    document.head.appendChild(script);
  });
}

async function verifyRazorpayPayment(orderId, paymentId, signature) {
  const { ok, data } = await apiPost('/api/payments/razorpay/verify', {
    order_id: orderId,
    payment_id: paymentId,
    signature
  });
  if (!ok) {
    setTopupStatus(data.error || 'Payment verification failed');
    return;
  }
  setWalletBalance(data.new_balance);
  closeModal('modal-topup');
  fetchFareQuotes();
  alert(`₹${money(data.amount_credited)} added. New balance: ₹${money(data.new_balance)}`);
}

async function handleRazorpayCheckout() {
  if (!AppState.currentUser) return;
  setTopupStatus('');
  const { ok, data: order } = await apiPost('/api/payments/razorpay/create-order', { amount: currentTopupAmount() });
  if (!ok) {
    setTopupStatus(order.error || 'Failed to start the payment');
    return;
  }

  if (order.demo_mode) {
    // Demo servers accept a fixed test signature; live servers never do.
    await verifyRazorpayPayment(order.order_id, `pay_demo_${Date.now()}`, 'demo_signature_valid');
    return;
  }

  try {
    await loadRazorpayScript();
  } catch (err) {
    setTopupStatus(err.message);
    return;
  }
  const checkout = new window.Razorpay({
    key: order.key_id,
    amount: order.amount,
    currency: order.currency,
    order_id: order.order_id,
    name: order.merchant_name,
    description: 'CampusGo wallet top-up',
    prefill: { email: AppState.currentUser.email, contact: AppState.currentUser.phone },
    handler: (resp) => verifyRazorpayPayment(resp.razorpay_order_id, resp.razorpay_payment_id, resp.razorpay_signature)
  });
  checkout.on('payment.failed', (resp) => setTopupStatus(resp.error && resp.error.description ? resp.error.description : 'Payment failed'));
  checkout.open();
}

function configureTopupModal() {
  const cfg = AppState.config || {};
  $('modal-current-bal').textContent = money(AppState.currentUser.wallet_balance);
  $('confirm-upi-label').textContent = cfg.payments_demo_mode ? 'Simulate Payment (Demo)' : "I've Paid";
  $('rzp-mode-badge').textContent = cfg.payments_demo_mode ? 'RAZORPAY DEMO MODE' : 'RAZORPAY SECURE CHECKOUT';
  document.querySelector('.pay-method-tab[data-method="razorpay"]').classList.toggle('hidden', !cfg.razorpay_enabled);
  setTopupStatus('');
  fetchUpiQr(currentTopupAmount());
}

// --- Driver dashboard ---
async function loadDriverEarnings() {
  if (!AppState.currentUser) return;
  try {
    const { ok, data } = await apiGet('/api/driver/earnings');
    if (!ok) return;
    $('driver-total-earned').textContent = money(data.total_earnings);
    $('driver-total-trips').textContent = data.total_trips;
    $('driver-rating-val').textContent = data.avg_rating.toFixed(1);
    $('driver-online-toggle').checked = data.is_online;
    $('online-status-text').textContent = data.is_online ? 'Online' : 'Offline';
    $('driver-requests-section').classList.toggle('hidden', !data.has_vehicle);
    if (data.has_vehicle) loadDriverRequests();
  } catch (err) {
    console.error('Failed to load driver earnings:', err);
  }
}

async function handleToggleOnline(event) {
  const wantOnline = event.target.checked;
  const { ok, data } = await apiPost('/api/driver/toggle-online', { is_online: wantOnline });
  if (!ok) {
    event.target.checked = !wantOnline;
    if (data.code === 'VEHICLE_REQUIRED') {
      openVehicleModal();
    } else {
      alert(data.error || 'Could not change your status');
    }
    return;
  }
  $('online-status-text').textContent = data.is_online ? 'Online' : 'Offline';
  refreshNearbyDrivers();
  fetchFareQuotes();
}

async function loadDriverRequests() {
  const list = $('driver-requests-list');
  const { ok, data } = await apiGet('/api/driver/requests');
  if (!ok) {
    list.innerHTML = `<p class="empty-state">${esc(data.error || 'Requests unavailable')}</p>`;
    return;
  }
  if (!data.requests.length) {
    list.innerHTML = '<p class="empty-state">No waiting requests.</p>';
    return;
  }
  list.replaceChildren();
  data.requests.forEach((r) => {
    const card = document.createElement('div');
    card.className = 'driver-request-card';
    card.innerHTML = `
      <div>
        <div class="veh-title">${esc(r.pickup_name)} ➔ ${esc(r.drop_name)}</div>
        <div class="veh-sub">${esc(r.rider_name)}${r.is_priority ? ' ⭐ Faculty' : ''} • You earn ₹${money(r.driver_payout)}</div>
      </div>
      <button class="btn-sm-primary" data-action="accept-ride" data-id="${esc(r.id)}">Accept</button>
    `;
    list.appendChild(card);
  });
}

async function handleAcceptRide(rideId) {
  const { ok, data } = await apiPost('/api/driver/accept', { ride_id: rideId });
  if (!ok) {
    alert(data.error || 'Could not accept this ride');
    loadDriverRequests();
    return;
  }
  await checkActiveRide();
}

function openVehicleModal() {
  const v = AppState.currentUser && AppState.currentUser.vehicle;
  $('veh-category-input').value = v ? v.category : 'bike';
  $('veh-model-input').value = v ? v.model : '';
  $('veh-plate-input').value = v ? v.plate_number : '';
  $('veh-color-input').value = v ? v.color : '';
  $('veh-capacity-input').value = v ? v.capacity : 1;
  $('veh-helmet-input').checked = v ? Boolean(v.has_helmet) : true;
  $('veh-ac-input').checked = v ? Boolean(v.has_ac) : false;
  $('veh-error-text').classList.add('hidden');
  openModal('modal-vehicle');
}

async function handleSaveVehicle() {
  const errorEl = $('veh-error-text');
  const { ok, data } = await apiPost('/api/user/vehicle', {
    category: $('veh-category-input').value,
    model: $('veh-model-input').value.trim(),
    plate_number: $('veh-plate-input').value.trim(),
    color: $('veh-color-input').value.trim(),
    capacity: parseInt($('veh-capacity-input').value, 10) || 1,
    has_helmet: $('veh-helmet-input').checked,
    has_ac: $('veh-ac-input').checked
  });
  if (!ok) {
    errorEl.textContent = data.error || 'Could not save vehicle';
    errorEl.classList.remove('hidden');
    return;
  }
  closeModal('modal-vehicle');
  await refreshCurrentUser();
  await loadDriverEarnings();
}

// --- CityLink carpools ---
function routeCardHtml(r, pinned) {
  const chips = r.passengers.length
    ? r.passengers.map((p) => `
        <span class="passenger-chip">
          <img src="${esc(safeImageUrl(p.passenger_avatar))}" alt="">
          <span class="passenger-chip-name">${esc(p.passenger_name)}</span>
          <span class="passenger-chip-seats">(${Number(p.seats)} seat${p.seats > 1 ? 's' : ''})</span>
        </span>`).join('')
    : '<span style="font-size:0.68rem; color:var(--text-muted);">No passengers yet</span>';

  const id = esc(r.id);
  let actions = '';
  if (r.status === 'in_progress') {
    actions = (r.is_host || r.has_joined)
      ? `<button class="btn-start-cockpit" data-action="cockpit" data-id="${id}">🟢 Live Trip • Open Cockpit</button>`
      : '<div class="route-note">Trip in progress</div>';
  } else if (r.is_host) {
    actions = `
      ${r.passengers.length ? `<button class="btn-start-cockpit" data-action="start" data-id="${id}">🚀 Start Trip</button>` : '<div class="route-note">You are the host • waiting for passengers</div>'}
      <button class="btn-sm-outline" data-action="cancel-route" data-id="${id}">Cancel Trip & Refund Passengers</button>`;
  } else if (r.has_joined) {
    actions = `<button class="btn-sm-outline" data-action="leave" data-id="${id}">Leave Trip (Full Refund)</button>`;
  } else if (!pinned && r.available_seats > 0) {
    const options = Array.from({ length: r.available_seats }, (_, i) => `<option value="${i + 1}">${i + 1} Seat${i > 0 ? 's' : ''}</option>`).join('');
    actions = `
      <div style="display:flex; gap:6px; align-items:center;">
        <select class="seat-picker-select" data-seat-for="${id}" style="border:1px solid var(--border-light); border-radius:4px; padding:3px 6px; font-size:0.75rem;">${options}</select>
        <button class="btn-join-route" data-action="join" data-id="${id}">Book Seat</button>
      </div>`;
  } else {
    actions = '<div class="route-note">🔒 Ride is fully booked</div>';
  }

  return `
    ${pinned ? `<div class="pinned-banner-header"><span class="pinned-pill">📌 PINNED & FULL</span><span class="pinned-seats-full">All ${Number(r.total_seats)} Seats Booked</span></div>` : ''}
    <div class="route-card-header">
      <div>
        <div class="route-dest-title">${esc(r.origin)} ➔ ${esc(r.destination)}</div>
        <div style="font-size:0.75rem; color:var(--text-secondary);">Departure: <strong>${esc(r.departure_time)}</strong></div>
        ${r.notes ? `<div style="font-size:0.7rem; color:var(--text-muted);">${esc(r.notes)}</div>` : ''}
      </div>
      <div class="route-price-tag">₹${Number(r.price_per_seat).toFixed(0)} <span style="font-size:0.6rem; color:var(--text-muted);">/seat</span></div>
    </div>
    <div class="route-driver-row">
      <img class="route-driver-avatar" src="${esc(safeImageUrl(r.driver_avatar))}" alt="">
      <span class="route-driver-text"><strong>${esc(r.driver_name)}</strong> • ${esc(r.vehicle_model || 'Vehicle')}${r.vehicle_plate ? ` (${esc(r.vehicle_plate)})` : ''}</span>
    </div>
    <div style="margin: 6px 0;">
      <div style="font-size:0.65rem; font-weight:700; color:var(--text-muted);">PASSENGERS:</div>
      <div class="passenger-chips-row" style="margin-top:2px;">${chips}</div>
    </div>
    <div class="route-card-footer">
      ${pinned ? '' : `<span class="route-seats-pill">${Number(r.available_seats)} of ${Number(r.total_seats)} seats remaining</span>`}
      ${actions}
    </div>
  `;
}

async function loadCityLinkRoutes() {
  if (!AppState.currentUser) return;
  try {
    const { ok, data } = await apiGet('/api/routes/scheduled');
    if (!ok) return;
    const routes = data.routes || [];
    const pinnedContainer = $('pinned-routes-container');
    const openContainer = $('citylink-routes-container');
    pinnedContainer.replaceChildren();
    openContainer.replaceChildren();

    const pinnedRoutes = routes.filter((r) => r.status === 'pinned' || r.status === 'in_progress');
    const openRoutes = routes.filter((r) => r.status === 'open');

    if (!pinnedRoutes.length) {
      pinnedContainer.innerHTML = '<p style="font-size:0.75rem; color:var(--text-muted); padding:8px 0;">No pinned routes currently. Scheduled rides pin here once all seats fill up!</p>';
    }
    pinnedRoutes.forEach((r) => {
      const card = document.createElement('div');
      card.className = 'pinned-route-card';
      card.innerHTML = routeCardHtml(r, true);
      pinnedContainer.appendChild(card);
    });

    if (!openRoutes.length) {
      openContainer.innerHTML = '<p style="text-align:center; color:var(--text-muted); font-size:0.8rem; padding:16px;">No open carpools at the moment. Use "+ Plan a Route" to offer seats!</p>';
    }
    openRoutes.forEach((r) => {
      const card = document.createElement('div');
      card.className = 'route-card';
      card.innerHTML = routeCardHtml(r, false);
      openContainer.appendChild(card);
    });
  } catch (err) {
    console.error('Failed to load scheduled carpool dashboard:', err);
  }
}

// One delegated handler for every button on route cards (no inline onclick).
async function handleRouteAction(event) {
  const btn = event.target.closest('[data-action]');
  if (!btn) return;
  const routeId = btn.dataset.id;
  const action = btn.dataset.action;

  if (action === 'cockpit') {
    await openDuringRideCockpit(routeId);
  } else if (action === 'start') {
    await startConfirmedRoute(routeId);
  } else if (action === 'join') {
    const sel = document.querySelector(`[data-seat-for="${CSS.escape(routeId)}"]`);
    await joinCityLinkRoute(routeId, sel ? parseInt(sel.value, 10) : 1);
  } else if (action === 'leave') {
    if (!confirm('Leave this trip? Your seat fare will be refunded.')) return;
    await routeSimpleAction(routeId, 'leave', (d) => `Booking cancelled. ₹${money(d.refunded)} refunded.`);
  } else if (action === 'cancel-route') {
    if (!confirm('Cancel this trip? Every passenger will be refunded.')) return;
    await routeSimpleAction(routeId, 'cancel', (d) => `Trip cancelled. ${d.refunded_bookings} booking(s) refunded.`);
  }
}

async function routeSimpleAction(routeId, verb, successMessage) {
  const { ok, data } = await apiPost(`/api/routes/${encodeURIComponent(routeId)}/${verb}`);
  if (!ok) {
    alert(data.error || 'Something went wrong');
    return;
  }
  alert(successMessage(data));
  await refreshCurrentUser();
  await loadCityLinkRoutes();
}

async function joinCityLinkRoute(routeId, seats) {
  if (!AppState.currentUser) return;
  const { ok, status, data } = await apiPost(`/api/routes/${encodeURIComponent(routeId)}/join`, { seats });
  if (!ok) {
    if (status === 402) {
      $('topup-custom-input').value = Math.ceil((data.deficit || 0) + 10);
      openModal('modal-topup');
    } else {
      alert(data.error || 'Could not join route');
    }
    return;
  }
  setWalletBalance(data.wallet_balance);
  if (data.is_pinned) {
    alert('🎉 ALL SEATS ARE NOW FULL!\n\nThis scheduled ride has been automatically PINNED and is confirmed for departure.');
  } else {
    alert(`Confirmed! ${seats} seat(s) booked for ₹${money(data.fare_paid)}. ${data.remaining_seats} seat(s) remaining.`);
  }
  await loadCityLinkRoutes();
}

async function postCityLinkRoute() {
  if (!AppState.currentUser) return;
  const payload = {
    origin: $('route-origin-select').value,
    destination: $('route-dest-select').value,
    departure_time: $('route-time-input').value.trim(),
    total_seats: parseInt($('route-seats-input').value, 10) || 1,
    price_per_seat: parseFloat($('route-price-input').value) || 80,
    notes: $('route-notes-input').value.trim()
  };
  const { ok, data } = await apiPost('/api/routes/plan', payload);
  if (!ok) {
    if (data.code === 'VEHICLE_REQUIRED') {
      closeModal('modal-post-route');
      openVehicleModal();
    } else {
      alert(data.error || 'Could not publish the route');
    }
    return;
  }
  closeModal('modal-post-route');
  await loadCityLinkRoutes();
  alert(`Route scheduled! Offering ${data.total_seats} seat(s) from ${payload.origin} to ${payload.destination}.`);
}

// --- During-ride cockpit for carpools ---
async function startConfirmedRoute(routeId) {
  const { ok, data } = await apiPost(`/api/routes/${encodeURIComponent(routeId)}/start`);
  if (!ok) {
    alert(data.error || 'Could not start the trip');
    return;
  }
  await openDuringRideCockpit(routeId);
}

async function openDuringRideCockpit(routeId) {
  try {
    const { ok, data } = await apiGet(`/api/routes/${encodeURIComponent(routeId)}/live`);
    if (!ok || !data.cockpit) {
      alert(data.error || 'Could not open the trip');
      return;
    }
    const cockpit = data.cockpit;
    AppState.activeCockpitRouteId = routeId;
    AppState.activeCockpitIsHost = cockpit.is_host;

    $('cockpit-route-title').textContent = `${cockpit.origin} ➔ ${cockpit.destination}`;
    $('cockpit-live-status-text').textContent = cockpit.status === 'in_progress' ? 'CARPOOL IN PROGRESS' : 'CARPOOL CONFIRMED';
    $('cockpit-telemetry-sub').textContent = `${cockpit.distance_remaining_km} km remaining • ETA: ~${cockpit.eta_minutes} mins`;
    $('active-trip-fare').textContent = money(cockpit.price_per_seat);
    $('active-pickup-name').textContent = cockpit.origin;
    $('active-drop-name').textContent = cockpit.destination;
    $('active-driver-name').textContent = `${cockpit.driver_name} (Host)`;
    $('active-driver-avatar').src = safeImageUrl(cockpit.driver_avatar);
    $('active-vehicle-model').textContent = cockpit.vehicle_model || 'Vehicle';
    $('active-vehicle-plate').textContent = cockpit.vehicle_plate || '—';

    const manifestBox = $('cockpit-passenger-list');
    manifestBox.replaceChildren(...cockpit.passengers.map((p) =>
      passengerChip(p.passenger_name, p.passenger_avatar, `(${p.seats} seat${p.seats > 1 ? 's' : ''})`)));
    $('manifest-count-badge').textContent = `${cockpit.passengers.length} Passenger${cockpit.passengers.length === 1 ? '' : 's'}`;

    $('cancel-ride-btn').classList.add('hidden');
    $('share-live-btn').classList.add('hidden');
    const completeBtn = $('complete-ride-btn');
    completeBtn.classList.toggle('hidden', !cockpit.is_host);
    completeBtn.disabled = cockpit.status !== 'in_progress';

    if (AppState.activeMap) {
      setTimeout(() => AppState.activeMap.invalidateSize(), 200);
      clearActiveMap();
      const oCoords = [cockpit.origin_lat, cockpit.origin_lng];
      const dCoords = [cockpit.destination_lat, cockpit.destination_lng];
      const vCoords = [cockpit.current_lat || cockpit.origin_lat, cockpit.current_lng || cockpit.origin_lng];
      AppState.activeMarkers.origin = L.marker(oCoords, { icon: pinIcon('#10B981', 'P', 22) }).addTo(AppState.activeMap);
      AppState.activeMarkers.drop = L.marker(dCoords, { icon: pinIcon('#FF7C00', 'D', 22) }).addTo(AppState.activeMap);
      AppState.activeMarkers.car = L.marker(vCoords, { icon: vehicleIcon(cockpit.vehicle_category || 'car', 34, '#F59E0B') }).addTo(AppState.activeMap);
      AppState.activePolyline = L.polyline([oCoords, dCoords], { color: '#F59E0B', weight: 5, opacity: 0.85, dashArray: '8, 8' })
        .addTo(AppState.activeMap);
      AppState.activeMap.fitBounds([oCoords, dCoords], { padding: [50, 50] });
    }

    switchView('view-active-ride');
  } catch (err) {
    console.error('Failed to open during-ride cockpit:', err);
  }
}

// --- Navigation ---
function initNavigation() {
  document.querySelectorAll('.nav-tab').forEach((tab) => {
    tab.addEventListener('click', () => switchView(tab.dataset.view));
  });
}

function switchView(viewId) {
  document.querySelectorAll('.app-view').forEach((v) => v.classList.remove('active-view'));
  const target = $(viewId);
  if (target) target.classList.add('active-view');

  document.querySelectorAll('.nav-tab').forEach((t) => {
    t.classList.toggle('active', t.dataset.view === viewId);
  });

  if (viewId === 'view-citylink') {
    loadCityLinkRoutes();
  } else if (viewId === 'view-activity') {
    loadActivityHistory();
  } else if (viewId === 'view-profile') {
    loadDriverEarnings();
  } else if (viewId === 'view-ride' && AppState.homeMap) {
    setTimeout(() => AppState.homeMap.invalidateSize(), 150);
  } else if (viewId === 'view-active-ride' && AppState.activeMap) {
    setTimeout(() => AppState.activeMap.invalidateSize(), 150);
  }
}

// --- Modals ---
function initModals() {
  document.querySelectorAll('.modal-close-btn, .modal-backdrop').forEach((btn) => {
    btn.addEventListener('click', () => {
      const modal = btn.closest('.app-modal');
      if (modal) modal.classList.add('hidden');
    });
  });
}

function openModal(id) {
  const modal = $(id);
  if (!modal) return;
  modal.classList.remove('hidden');
  if (id === 'modal-topup' && AppState.currentUser) {
    configureTopupModal();
  }
  if (id === 'modal-post-route') {
    setTimeout(() => {
      populatePlannedRouteDropdowns();
      initializeCustomLocationDropdowns();
      syncCustomLocationDropdowns();
    }, 0);
  }
}

function closeModal(id) {
  const modal = $(id);
  if (modal) modal.classList.add('hidden');
}

// --- Event wiring ---
function initEventHandlers() {
  // Top-up payment method tabs
  document.querySelectorAll('.pay-method-tab').forEach((tab) => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('.pay-method-tab').forEach((t) => t.classList.remove('active'));
      tab.classList.add('active');
      const isUpi = tab.dataset.method === 'upi';
      $('pay-panel-upi').classList.toggle('hidden', !isUpi);
      $('pay-panel-razorpay').classList.toggle('hidden', isUpi);
    });
  });

  $('topup-custom-input').addEventListener('input', () => {
    if (currentTopupAmount() > 0) scheduleQrRefresh();
  });
  document.querySelectorAll('.amount-chip').forEach((chip) => {
    chip.addEventListener('click', () => {
      document.querySelectorAll('.amount-chip').forEach((c) => c.classList.remove('active'));
      chip.classList.add('active');
      $('topup-custom-input').value = parseFloat(chip.dataset.amount);
      scheduleQrRefresh();
    });
  });
  $('confirm-upi-paid-btn').addEventListener('click', handleConfirmUpiPayment);
  $('razorpay-checkout-btn').addEventListener('click', handleRazorpayCheckout);

  // Scope switcher: Campus Hop vs CityLink
  document.querySelectorAll('.scope-tab').forEach((tab) => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('.scope-tab').forEach((t) => t.classList.remove('active'));
      tab.classList.add('active');
      AppState.selectedScope = tab.dataset.scope;
      populateLocationDropdowns();
    });
  });

  // Bike / Scooty / Car
  document.querySelectorAll('.service-card').forEach((card) => {
    card.addEventListener('click', () => {
      document.querySelectorAll('.service-card').forEach((c) => c.classList.remove('active'));
      card.classList.add('active');
      AppState.selectedService = card.dataset.service;
      if (AppState.quotes && AppState.currentUser) {
        updateServicesDisplay({ quotes: AppState.quotes });
      }
    });
  });

  $('pickup-select').addEventListener('change', fetchFareQuotes);
  $('drop-select').addEventListener('change', fetchFareQuotes);
  $('swap-locations-btn').addEventListener('click', () => {
    const p = $('pickup-select');
    const d = $('drop-select');
    const temp = p.value;
    p.value = d.value;
    d.value = temp;
    syncCustomLocationDropdowns();
    fetchFareQuotes();
  });

  $('quick-topup-btn').addEventListener('click', () => openModal('modal-topup'));
  $('header-wallet-btn').addEventListener('click', () => openModal('modal-topup'));
  $('persona-switch-btn').addEventListener('click', () => switchView('view-profile'));
  $('recenter-map-btn').addEventListener('click', () => {
    if (AppState.homeMap) AppState.homeMap.setView(LPU_COORDS, 16);
  });

  // Booking and live ride
  $('find-ride-btn').addEventListener('click', handleBookRide);
  $('cancel-queued-ride-btn').addEventListener('click', handleCancelRide);
  $('cancel-ride-btn').addEventListener('click', handleCancelRide);
  $('sim-telemetry-btn').addEventListener('click', advanceTelemetryStep);
  $('complete-ride-btn').addEventListener('click', handleCompleteRide);

  // Rating
  document.querySelectorAll('#star-rating-picker .star-btn').forEach((star) => {
    star.addEventListener('click', () => setRating(Number(star.dataset.val)));
  });
  document.querySelectorAll('#modal-rating .tag-pill').forEach((tag) => {
    tag.addEventListener('click', () => tag.classList.toggle('active'));
  });
  $('submit-rating-btn').addEventListener('click', submitRating);

  // SOS and sharing
  $('persistent-sos-btn').addEventListener('click', handleTriggerSOS);
  $('dismiss-sos-btn').addEventListener('click', () => closeModal('modal-sos'));
  $('share-live-btn').addEventListener('click', handleShareTrip);
  $('copy-share-btn').addEventListener('click', async () => {
    const input = $('share-link-input');
    input.select();
    try {
      await navigator.clipboard.writeText(input.value);
      alert('Live tracking link copied to clipboard!');
    } catch (err) {
      alert('Copy failed - select the link and copy it manually.');
    }
  });

  // CityLink
  $('open-plan-route-btn').addEventListener('click', () => openModal('modal-post-route'));
  $('submit-post-route-btn').addEventListener('click', postCityLinkRoute);
  $('pinned-routes-container').addEventListener('click', handleRouteAction);
  $('citylink-routes-container').addEventListener('click', handleRouteAction);

  // Driver dashboard
  $('driver-online-toggle').addEventListener('change', handleToggleOnline);
  $('edit-vehicle-btn').addEventListener('click', openVehicleModal);
  $('submit-vehicle-btn').addEventListener('click', handleSaveVehicle);
  $('refresh-requests-btn').addEventListener('click', loadDriverRequests);
  $('driver-requests-list').addEventListener('click', (event) => {
    const btn = event.target.closest('[data-action="accept-ride"]');
    if (btn) handleAcceptRide(btn.dataset.id);
  });

  // Emergency contacts
  $('add-emergency-btn').addEventListener('click', () => {
    $('emg-error-text').classList.add('hidden');
    openModal('modal-add-contact');
  });
  $('submit-contact-btn').addEventListener('click', handleAddContact);
  $('emergency-contacts-list').addEventListener('click', (event) => {
    const btn = event.target.closest('.contact-remove-btn');
    if (btn) handleRemoveContact(btn.dataset.id);
  });

  // Role selector
  document.querySelectorAll('.role-pill-btn').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const newRole = btn.dataset.role;
      const { ok, data } = await apiPost('/api/user/role', { role: newRole });
      if (!ok) {
        alert(data.error || 'Could not change role');
        return;
      }
      AppState.currentUser.role = newRole;
      updateUserUI();
      if (newRole !== 'rider' && !AppState.currentUser.vehicle) {
        openVehicleModal();
      } else {
        loadDriverEarnings();
      }
    });
  });
}
