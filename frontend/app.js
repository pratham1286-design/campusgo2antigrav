/**
 * CampusGo - LPU Community Transit Application Controller
 * Leaflet map, 3 vehicle services, server-held wallet, zone queueing,
 * teacher priority, SOS, carpools and the driver dashboard.
 *
 * Security rule for this file: server data is never inserted as raw HTML.
 * Use textContent, or esc() for every value inside an HTML template string.
 */

// --- Themed popups (replace native alert/confirm) ---
function showDialog({ message, confirmText = 'OK', cancelText = null, danger = false }) {
  return new Promise((resolve) => {
    const host = document.querySelector('.app-container') || document.body;
    const text = String(message ?? '');
    const lower = text.toLowerCase();
    const isError = /could not|failed|error|busy|allow location|copy failed/.test(lower);
    const icon = cancelText ? '❓' : (isError ? '⚠️' : (/🎉|🏁|confirmed|added|scheduled|copied|cancelled/.test(lower) ? '✅' : 'ℹ️'));

    const overlay = document.createElement('div');
    overlay.className = 'app-modal app-dialog';
    overlay.setAttribute('role', 'alertdialog');
    overlay.setAttribute('aria-modal', 'true');

    const backdrop = document.createElement('div');
    backdrop.className = 'modal-backdrop';
    const card = document.createElement('div');
    card.className = 'dialog-card';
    const iconEl = document.createElement('div');
    iconEl.className = 'dialog-icon' + (danger || isError ? ' is-warn' : '');
    iconEl.textContent = icon;
    const msg = document.createElement('p');
    msg.className = 'dialog-message';
    msg.textContent = text;
    const actions = document.createElement('div');
    actions.className = 'dialog-actions';

    const close = (result) => {
      document.removeEventListener('keydown', onKey, true);
      overlay.remove();
      resolve(result);
    };
    const onKey = (e) => {
      if (e.key === 'Escape') { e.stopPropagation(); close(false); }
    };

    if (cancelText) {
      const cancelBtn = document.createElement('button');
      cancelBtn.className = 'dialog-btn dialog-btn-secondary';
      cancelBtn.textContent = cancelText;
      cancelBtn.addEventListener('click', () => close(false));
      actions.appendChild(cancelBtn);
    }
    const okBtn = document.createElement('button');
    okBtn.className = 'dialog-btn dialog-btn-primary' + (danger ? ' is-danger' : '');
    okBtn.textContent = confirmText;
    okBtn.addEventListener('click', () => close(true));
    actions.appendChild(okBtn);

    backdrop.addEventListener('click', () => close(false));
    card.append(iconEl, msg, actions);
    overlay.append(backdrop, card);
    host.appendChild(overlay);
    document.addEventListener('keydown', onKey, true);
    okBtn.focus();
  });
}

function showAlert(message) {
  return showDialog({ message });
}

function showConfirm(message) {
  return showDialog({ message, confirmText: 'Yes', cancelText: 'No', danger: true });
}

const DEFAULT_AVATAR = 'data:image/svg+xml;utf8,' + encodeURIComponent(
  '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 120"><rect width="120" height="120" fill="#FFE9D6"/>'
  + '<circle cx="60" cy="46" r="22" fill="#FF7C00"/><path d="M16 120c4-28 22-42 44-42s40 14 44 42z" fill="#FF7C00"/></svg>'
);
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
  driverOnline: false,
  ratingRideId: null,
  ratingValue: 5,
  currentUpiRef: null,
  homeMap: null,
  activeMap: null,
  homeMarkers: {},
  driverMarkers: {},
  customDrop: null,        // a searched address chosen as the CityLink destination: {name, lat, lng}
  routeLine: null,
  routeKey: '',
  routeSeq: 0,
  placeSeq: 0,
  trackRouteAt: 0,
  tripRouteFor: '',
  activeLeg: null,
  activeFit: null,
  roadMinutes: null,
  trackBusy: false,
  activeMarkers: {},
  activePolyline: null,
  timers: { drivers: null, queue: null, qr: null, gps: null, driverPoll: null, track: null }
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
  detectUserLocation();
  initEventHandlers();
  updateZoneBanner();
  setInterval(updateZoneBanner, 60000);
  $('location-gate-retry').addEventListener('click', detectUserLocation);
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
  if (typeof resetAuthForms === 'function') resetAuthForms();
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

function clearTimers() {
  Object.keys(AppState.timers).forEach((key) => {
    clearInterval(AppState.timers[key]);
    clearTimeout(AppState.timers[key]);
    AppState.timers[key] = null;
  });
}

async function handleLogoutClick() {
  try {
    await apiPost('/api/auth/logout');
  } catch (err) {
    // Offline: still sign out locally.
  }
  handleLogout();
}

function handleLogout() {
  AppState.driverOnline = false;
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
  if (AppState.userPosition) applyUserPosition((t) => { $('auto-detected-badge').textContent = t; });
  else fetchFareQuotes();
  refreshNearbyDrivers();
  clearInterval(AppState.timers.drivers);
  AppState.timers.drivers = setInterval(refreshNearbyDrivers, 8000);
  clearInterval(AppState.timers.gps);
  AppState.timers.gps = setInterval(reportDriverLocation, 15000);
  clearInterval(AppState.timers.driverPoll);
  AppState.timers.driverPoll = setInterval(pollDriverWork, 10000);
  await checkActiveRide();
}

// --- Driver polling ---
// An online driver is told about work without having to open the Profile tab: a ride the
// server assigned automatically opens the live ride screen, and waiting requests light a dot.
function isActiveDriver() {
  const u = AppState.currentUser;
  return Boolean(u && (u.role === 'driver' || u.role === 'both') && u.vehicle && AppState.driverOnline);
}

async function pollDriverWork() {
  if (!isActiveDriver() || AppState.polling) return;
  AppState.polling = true;
  try {
    if (!AppState.activeRide && !AppState.activeCockpitRouteId) {
      await checkActiveRide();
      if (AppState.activeRide && AppState.activeRide.is_rider === false) {
        showAlert(`🚗 New ride assigned!
Pick up at ${AppState.activeRide.pickup_name}.`);
        return;
      }
      await loadDriverRequests();
    }
  } finally {
    AppState.polling = false;
  }
}

function setRequestsDot(count) {
  const dot = $('nav-requests-dot');
  if (dot) dot.classList.toggle('hidden', !count);
}

// --- Driver GPS ---
// The server trusts only the position the driver's own phone reports: it decides who can be
// matched, when a pickup starts, and whether a trip can be completed.
function driverNeedsToReport() {
  const ride = AppState.activeRide;
  return AppState.driverOnline
    || Boolean(ride && ride.is_rider === false)
    || Boolean(AppState.activeCockpitRouteId && AppState.activeCockpitIsHost);
}

async function reportDriverLocation() {
  if (!AppState.currentUser || !driverNeedsToReport()) return;
  const pos = await getBrowserPosition(8000);
  if (!pos) return;
  const { ok, data } = await apiPost('/api/driver/location', pos);
  if (ok && data.ride_status === 'in_progress' && AppState.activeRide && AppState.activeRide.status !== 'in_progress') {
    checkActiveRide();
  }
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

// --- Welcome / sign up / log in (OTP) ---
const AuthFlow = {
  panel: 'welcome',
  mode: 'login',          // 'login' | 'signup' - which flow the OTP step belongs to
  userType: 'student',
  challengeId: null,
  username: '',
  usernameOk: false,
  suggestionTimer: null,
  availabilityTimer: null,
  resendTimer: null
};

function authShow(panel) {
  AuthFlow.panel = panel;
  document.querySelectorAll('#login-screen [data-panel]').forEach((el) => {
    el.classList.toggle('hidden', el.dataset.panel !== panel);
  });
  authClearErrors();
  const focusTarget = {
    'signup-name': 'signup-name-input',
    'signup-details': 'signup-lpu-input',
    'login': 'login-lpu-input'
  }[panel];
  if (focusTarget) $(focusTarget).focus();
  if (panel === 'otp') document.querySelector('#otp-boxes input').focus();
}

function authClearErrors() {
  document.querySelectorAll('#login-screen [data-error]').forEach((el) => el.classList.add('hidden'));
}

function authError(key, message) {
  const el = document.querySelector(`#login-screen [data-error="${key}"]`);
  el.textContent = message;
  el.classList.remove('hidden');
}

// Runs one async auth call with a disabled/busy button and a friendly network error.
async function authRun(btn, busyText, errorKey, work) {
  const idle = btn.textContent;
  btn.disabled = true;
  btn.textContent = busyText;
  try {
    return await work();
  } catch (err) {
    console.error('Auth request failed:', err);
    authError(errorKey, 'Could not reach the server. Please try again.');
    return null;
  } finally {
    btn.disabled = false;
    btn.textContent = idle;
  }
}

function cleanPhone(value) {
  return value.replace(/[\s\-()]/g, '');
}

// --- Sign up: name + username ---
function setUsernameStatus(text, kind) {
  const el = $('username-status');
  el.textContent = text;
  el.className = `field-status${kind ? ' ' + kind : ''}`;
}

function markSelectedSuggestion() {
  document.querySelectorAll('#username-suggestions .chip').forEach((chip) => {
    chip.classList.toggle('selected', chip.dataset.username === $('signup-username-input').value.trim().toLowerCase());
  });
}

async function loadUsernameSuggestions() {
  const name = $('signup-name-input').value.trim();
  const wrap = $('username-suggest-wrap');
  if (name.length < 2) {
    wrap.classList.add('hidden');
    return;
  }
  const { ok, data } = await apiPost('/api/auth/username-suggestions', { name });
  if (!ok || !data.suggestions || !data.suggestions.length) {
    wrap.classList.add('hidden');
    return;
  }
  const row = $('username-suggestions');
  row.replaceChildren();
  data.suggestions.forEach((username) => {
    const chip = document.createElement('button');
    chip.type = 'button';
    chip.className = 'chip';
    chip.dataset.username = username;
    chip.textContent = username;
    chip.addEventListener('click', () => {
      $('signup-username-input').value = username;
      AuthFlow.usernameOk = true;
      setUsernameStatus('Available', 'ok');
      markSelectedSuggestion();
    });
    row.appendChild(chip);
  });
  wrap.classList.remove('hidden');
  markSelectedSuggestion();
}

async function checkUsernameAvailable() {
  const username = $('signup-username-input').value.trim().toLowerCase();
  markSelectedSuggestion();
  AuthFlow.usernameOk = false;
  if (!username) {
    setUsernameStatus('', '');
    return;
  }
  const { ok, data } = await apiPost('/api/auth/username-available', { username });
  if ($('signup-username-input').value.trim().toLowerCase() !== username) return; // stale reply
  if (!ok) {
    setUsernameStatus(data.error || 'Could not check this username', 'bad');
    return;
  }
  AuthFlow.usernameOk = data.available;
  setUsernameStatus(data.available ? 'Available' : data.reason, data.available ? 'ok' : 'bad');
}

async function signupNameNext() {
  const name = $('signup-name-input').value.trim();
  const username = $('signup-username-input').value.trim().toLowerCase();
  if (!/^[A-Za-z][A-Za-z .'\-]{1,59}$/.test(name)) {
    authError('signup-name', 'Enter your full name using letters only.');
    return;
  }
  if (!username) {
    authError('signup-name', 'Pick a username from the suggestions or type your own.');
    return;
  }
  await checkUsernameAvailable();
  if (!AuthFlow.usernameOk) {
    authError('signup-name', $('username-status').textContent || 'That username is not available.');
    return;
  }
  AuthFlow.username = username;
  authShow('signup-details');
}

// --- OTP step ---
function startResendCountdown(seconds) {
  clearInterval(AuthFlow.resendTimer);
  const label = $('otp-resend-timer');
  const btn = $('otp-resend-btn');
  let left = seconds;
  btn.classList.add('hidden');
  label.textContent = `Resend code in ${left}s`;
  AuthFlow.resendTimer = setInterval(() => {
    left -= 1;
    if (left <= 0) {
      clearInterval(AuthFlow.resendTimer);
      label.textContent = '';
      btn.classList.remove('hidden');
    } else {
      label.textContent = `Resend code in ${left}s`;
    }
  }, 1000);
}

function showOtpStep(reply) {
  AuthFlow.challengeId = reply.challenge_id;
  $('otp-phone-hint').textContent = reply.phone_hint;
  document.querySelectorAll('#otp-boxes input').forEach((box) => { box.value = ''; });
  const note = $('otp-demo-note');
  if (reply.demo_otp) {
    note.replaceChildren('Demo mode, no SMS sent. Your code is ');
    const code = document.createElement('code');
    code.textContent = reply.demo_otp;
    note.appendChild(code);
    note.classList.remove('hidden');
  } else {
    note.classList.add('hidden');
  }
  authShow('otp');
  startResendCountdown(reply.resend_after || 30);
}

function readOtp() {
  return Array.from(document.querySelectorAll('#otp-boxes input')).map((b) => b.value).join('');
}

function initOtpBoxes() {
  const boxes = Array.from(document.querySelectorAll('#otp-boxes input'));
  boxes.forEach((box, i) => {
    box.addEventListener('input', () => {
      box.value = box.value.replace(/\D/g, '').slice(-1);
      if (box.value && i < boxes.length - 1) boxes[i + 1].focus();
      if (readOtp().length === boxes.length) verifyOtp();
    });
    box.addEventListener('keydown', (e) => {
      if (e.key === 'Backspace' && !box.value && i > 0) boxes[i - 1].focus();
      if (e.key === 'Enter') verifyOtp();
    });
    box.addEventListener('paste', (e) => {
      const digits = (e.clipboardData.getData('text') || '').replace(/\D/g, '').slice(0, boxes.length);
      if (!digits) return;
      e.preventDefault();
      digits.split('').forEach((d, j) => { boxes[j].value = d; });
      boxes[Math.min(digits.length, boxes.length - 1)].focus();
      if (digits.length === boxes.length) verifyOtp();
    });
  });
}

async function requestOtp(btn, errorKey) {
  const isSignup = AuthFlow.mode === 'signup';
  const url = isSignup ? '/api/auth/signup/start' : '/api/auth/login/start';
  const payload = isSignup
    ? {
        name: $('signup-name-input').value.trim(),
        username: AuthFlow.username,
        user_type: AuthFlow.userType,
        lpu_id: $('signup-lpu-input').value.trim(),
        phone: cleanPhone($('signup-phone-input').value)
      }
    : { lpu_id: $('login-lpu-input').value.trim(), phone: cleanPhone($('login-phone-input').value) };

  if (!payload.lpu_id || !payload.phone) {
    authError(errorKey, 'Enter your LPU ID and mobile number.');
    return;
  }
  const result = await authRun(btn, 'Sending code...', errorKey, () => apiPost(url, payload));
  if (!result) return;
  if (!result.ok) {
    authError(errorKey, result.data.error || 'Could not send the code.');
    return;
  }
  showOtpStep(result.data);
}

async function verifyOtp() {
  const btn = $('otp-verify-btn');
  if (btn.disabled) return;
  const code = readOtp();
  if (code.length !== 6) {
    authError('otp', 'Enter the 6-digit code.');
    return;
  }
  const url = AuthFlow.mode === 'signup' ? '/api/auth/signup/verify' : '/api/auth/login/verify';
  const result = await authRun(btn, 'Verifying...', 'otp', () => apiPost(url, { challenge_id: AuthFlow.challengeId, otp: code }));
  if (!result) return;
  if (!result.ok) {
    authError('otp', result.data.error || 'Verification failed.');
    if (['OTP_EXPIRED', 'OTP_LOCKED'].includes(result.data.code)) {
      clearInterval(AuthFlow.resendTimer);
      $('otp-resend-timer').textContent = '';
      $('otp-resend-btn').classList.remove('hidden');
    } else {
      document.querySelectorAll('#otp-boxes input').forEach((box) => { box.value = ''; });
      document.querySelector('#otp-boxes input').focus();
    }
    return;
  }
  clearInterval(AuthFlow.resendTimer);
  AppState.authToken = result.data.token;
  AppState.currentUser = result.data.user;
  storeToken(result.data.token);
  resetAuthForms();
  await onLoginSuccess();
}

function resetAuthForms() {
  document.querySelectorAll('#login-screen input').forEach((input) => { input.value = ''; });
  AuthFlow.challengeId = null;
  AuthFlow.username = '';
  AuthFlow.usernameOk = false;
  $('username-suggest-wrap').classList.add('hidden');
  setUsernameStatus('', '');
  setUserType('student');
  authShow('welcome');
}

function setUserType(type) {
  AuthFlow.userType = type;
  document.querySelectorAll('#login-screen .seg-btn').forEach((btn) => {
    const active = btn.dataset.usertype === type;
    btn.classList.toggle('active', active);
    btn.setAttribute('aria-checked', String(active));
  });
  $('signup-lpu-label').textContent = type === 'student' ? 'LPU ID (registration number)' : 'Faculty ID';
  $('signup-lpu-input').placeholder = type === 'student' ? 'e.g. 12204592' : 'e.g. FAC-10822';
  $('signup-lpu-input').inputMode = type === 'student' ? 'numeric' : 'text';
}

function initAuthHandlers() {
  $('auth-go-login').addEventListener('click', () => { AuthFlow.mode = 'login'; authShow('login'); });
  $('auth-go-signup').addEventListener('click', () => { AuthFlow.mode = 'signup'; authShow('signup-name'); });
  $('login-to-signup').addEventListener('click', () => { AuthFlow.mode = 'signup'; authShow('signup-name'); });

  document.querySelectorAll('#login-screen [data-back]').forEach((btn) => {
    btn.addEventListener('click', () => authShow(btn.dataset.back));
  });
  $('otp-back').addEventListener('click', () => {
    clearInterval(AuthFlow.resendTimer);
    authShow(AuthFlow.mode === 'signup' ? 'signup-details' : 'login');
  });

  $('signup-name-input').addEventListener('input', () => {
    clearTimeout(AuthFlow.suggestionTimer);
    AuthFlow.suggestionTimer = setTimeout(loadUsernameSuggestions, 350);
  });
  $('signup-username-input').addEventListener('input', () => {
    clearTimeout(AuthFlow.availabilityTimer);
    AuthFlow.usernameOk = false;
    setUsernameStatus('', '');
    markSelectedSuggestion();
    AuthFlow.availabilityTimer = setTimeout(checkUsernameAvailable, 400);
  });
  $('signup-name-next').addEventListener('click', signupNameNext);
  $('signup-username-input').addEventListener('keydown', (e) => { if (e.key === 'Enter') signupNameNext(); });

  document.querySelectorAll('#login-screen .seg-btn').forEach((btn) => {
    btn.addEventListener('click', () => setUserType(btn.dataset.usertype));
  });
  $('signup-send-otp').addEventListener('click', () => requestOtp($('signup-send-otp'), 'signup-details'));
  $('login-send-otp').addEventListener('click', () => requestOtp($('login-send-otp'), 'login'));
  ['signup-phone-input', 'signup-lpu-input'].forEach((id) => {
    $(id).addEventListener('keydown', (e) => { if (e.key === 'Enter') requestOtp($('signup-send-otp'), 'signup-details'); });
  });
  ['login-lpu-input', 'login-phone-input'].forEach((id) => {
    $(id).addEventListener('keydown', (e) => { if (e.key === 'Enter') requestOtp($('login-send-otp'), 'login'); });
  });

  $('otp-verify-btn').addEventListener('click', verifyOtp);
  $('otp-resend-btn').addEventListener('click', () => {
    requestOtp($('otp-resend-btn'), 'otp');
  });
  initOtpBoxes();
  $('logout-btn').addEventListener('click', handleLogoutClick);
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

const CURRENT_LOCATION_KEY = 'current_location';
const NOT_IN_CAMPUS_KEY = 'not_in_campus';
const CUSTOM_PLACE_KEY = 'custom_place';

// Real GPS coordinates are sent only when the pickup is "My current location".
function pickupCoords(key) {
  if (key !== CURRENT_LOCATION_KEY || !AppState.userPosition) return {};
  return { pickup_lat: AppState.userPosition.lat, pickup_lng: AppState.userPosition.lng };
}

// Mirrors the server: "My current location" is a campus pickup only near campus.
function registerCurrentLocation() {
  const pos = AppState.userPosition;
  if (!pos) return;
  let near = Infinity;
  Object.entries(AppState.landmarks).forEach(([key, loc]) => {
    if (key === CURRENT_LOCATION_KEY || loc.zone.startsWith('CityLink')) return;
    near = Math.min(near, distanceKm(pos.lat, pos.lng, loc.lat, loc.lng));
  });
  if (near <= 2) {
    delete AppState.landmarks[CURRENT_LOCATION_KEY];
    return;
  }
  AppState.landmarks[CURRENT_LOCATION_KEY] = {
    name: 'My current location',
    lat: pos.lat,
    lng: pos.lng,
    zone: 'CityLink-Current',
    custom: true
  };
}

// --- Real location: asks the browser for the user's position ---
function distanceKm(lat1, lng1, lat2, lng2) {
  const rad = (d) => (d * Math.PI) / 180;
  const a = Math.sin(rad(lat2 - lat1) / 2) ** 2
    + Math.cos(rad(lat1)) * Math.cos(rad(lat2)) * Math.sin(rad(lng2 - lng1) / 2) ** 2;
  return 6371 * 2 * Math.asin(Math.sqrt(a));
}

function requestPosition(highAccuracy, timeoutMs) {
  return new Promise((resolve) => {
    navigator.geolocation.getCurrentPosition(
      (pos) => resolve({ pos: { lat: pos.coords.latitude, lng: pos.coords.longitude } }),
      (err) => resolve({ error: err }),
      { enableHighAccuracy: highAccuracy, timeout: timeoutMs, maximumAge: 60000 }
    );
  });
}

function showLocationGate(title, message, showRetry = true) {
  const gate = $('location-gate');
  if (!gate) return;
  $('location-gate-title').textContent = title;
  $('location-gate-msg').textContent = message;
  $('location-gate-retry').classList.toggle('hidden', !showRetry);
  gate.classList.remove('hidden');
}

function hideLocationGate() {
  const gate = $('location-gate');
  if (gate) gate.classList.add('hidden');
}

// Location is compulsory: the app stays behind a gate until a real fix arrives.
async function detectUserLocation() {
  if (AppState.locating) return;
  AppState.locating = true;
  const badge = $('auto-detected-badge');
  const setBadge = (text, off) => {
    if (!badge) return;
    badge.textContent = text;
    badge.classList.toggle('off-campus', Boolean(off));
    badge.classList.remove('hidden');
  };

  try {
    if (!navigator.geolocation || window.isSecureContext === false) {
      setBadge('Needs HTTPS', true);
      showLocationGate('Secure connection needed',
        'Your browser only shares location over HTTPS. Open CampusGo through its https:// address and allow location access.', false);
      return;
    }

    setBadge('Locating…');
    showLocationGate('Turn on location', 'CampusGo needs your live location to set your pickup and match nearby riders. Tap Allow when your browser asks.', false);

    let result = await requestPosition(false, 15000);
    if (!result.pos && result.error && result.error.code !== 1) {
      result = await requestPosition(true, 20000);
    }
    if (!result.pos) {
      setBadge('Location off', true);
      const denied = result.error && result.error.code === 1;
      showLocationGate(
        denied ? 'Location is blocked' : 'Could not find you',
        denied
          ? 'Location access is blocked for this site. Click the lock icon in the address bar, set Location to Allow, then tap Try again.'
          : 'We could not get your position. Check that GPS or Wi-Fi location is on, then tap Try again.'
      );
      return;
    }

    AppState.userPosition = result.pos;
    hideLocationGate();
    applyUserPosition(setBadge);
    startLocationWatch();
  } finally {
    AppState.locating = false;
  }
}

function applyUserPosition(setBadge) {
  const pos = AppState.userPosition;
  if (!pos) return;
  let nearestKey = null;
  let nearest = Infinity;
  Object.entries(AppState.landmarks).forEach(([key, loc]) => {
    if (loc.zone.startsWith('CityLink') || loc.custom) return;
    const d = distanceKm(pos.lat, pos.lng, loc.lat, loc.lng);
    if (d < nearest) { nearest = d; nearestKey = key; }
  });

  drawUserMarker();
  registerCurrentLocation();
  const onCampus = nearestKey !== null && nearest <= 2;
  AppState.offCampus = !onCampus;
  if (onCampus) {
    // Inside the campus: the pickup is the nearest campus point.
    AppState.pickupKey = nearestKey;
  } else {
    // Outside: Campus Hop keeps showing "Not in campus"; CityLink uses the real position.
    AppState.pickupKey = AppState.selectedScope === 'campus_hop' ? NOT_IN_CAMPUS_KEY : CURRENT_LOCATION_KEY;
  }
  AppState.selectedScope = AppState.selectedScope || 'campus_hop';
  if (AppState.homeMap) AppState.homeMap.setView([pos.lat, pos.lng], onCampus ? 16 : 13);
  populateLocationDropdowns();
  setBadge(onCampus ? 'Auto-detected' : 'Not in campus', !onCampus);
}

function drawUserMarker() {
  const pos = AppState.userPosition;
  if (!AppState.homeMap || !pos) return;
  if (AppState.userMarker) AppState.homeMap.removeLayer(AppState.userMarker);
  AppState.userMarker = L.marker([pos.lat, pos.lng], { icon: pinIcon('#2563EB', 'Me') })
    .addTo(AppState.homeMap).bindPopup('You are here');
}

// Keeps the position fresh, and brings the gate back if access is revoked.
function startLocationWatch() {
  if (AppState.locationWatchId != null) return;
  AppState.locationWatchId = navigator.geolocation.watchPosition(
    (p) => {
      AppState.userPosition = { lat: p.coords.latitude, lng: p.coords.longitude };
      const wasOff = AppState.offCampus;
      registerCurrentLocation();
      drawUserMarker();
      const nowOff = !AppState.landmarks[CURRENT_LOCATION_KEY] ? false : true;
      if (wasOff !== undefined && wasOff !== nowOff) {
        applyUserPosition((text, off) => {
          const b = $('auto-detected-badge');
          b.textContent = text;
          b.classList.toggle('off-campus', Boolean(off));
        });
      }
    },
    (err) => {
      if (err.code === 1) {
        navigator.geolocation.clearWatch(AppState.locationWatchId);
        AppState.locationWatchId = null;
        detectUserLocation();
      }
    },
    { enableHighAccuracy: false, maximumAge: 15000, timeout: 30000 }
  );
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
      const boundary = trigger.closest('.bottom-sheet, .modal-card, .app-view');
      const limit = boundary ? boundary.getBoundingClientRect() : { top: 0, bottom: window.innerHeight };
      const navBar = document.querySelector('.bottom-nav-bar');
      const floor = Math.min(limit.bottom, navBar ? navBar.getBoundingClientRect().top : window.innerHeight);
      const spaceBelow = floor - triggerRect.bottom - 16;
      const spaceAbove = triggerRect.top - Math.max(limit.top, 0) - 16;
      const openUp = spaceBelow < 200 && spaceAbove > spaceBelow;

      if (openUp) {
        menu.style.top = 'auto';
        menu.style.bottom = 'calc(100% + 8px)';
      } else {
        menu.style.top = 'calc(100% + 8px)';
        menu.style.bottom = 'auto';
      }

      const room = openUp ? spaceAbove : spaceBelow;
      menu.style.maxHeight = `${Math.min(280, Math.max(140, room))}px`;
      requestAnimationFrame(() => {
        const r = menu.getBoundingClientRect();
        if (!openUp && r.bottom > floor) {
          const scroller = trigger.closest('.bottom-sheet, .modal-card, .app-view');
          if (scroller) scroller.scrollTop += r.bottom - floor + 8;
        }
      });
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
  const lockedOut = isHop && AppState.offCampus;
  $('app-root').dataset.scope = AppState.selectedScope;
  if (isHop) AppState.customDrop = null;

  if (AppState.offCampus) {
    // Away from campus: Campus Hop is locked to "Not in campus"; CityLink starts from the real position.
    if (lockedOut) AppState.pickupKey = NOT_IN_CAMPUS_KEY;
    else if (AppState.pickupKey === NOT_IN_CAMPUS_KEY) AppState.pickupKey = CURRENT_LOCATION_KEY;
  } else if (AppState.pickupKey === NOT_IN_CAMPUS_KEY) {
    AppState.pickupKey = 'uni_mall';
  }

  if (lockedOut) {
    const optLocked = document.createElement('option');
    optLocked.value = NOT_IN_CAMPUS_KEY;
    optLocked.textContent = '📍 Not in campus';
    pickupSelect.appendChild(optLocked);
  }

  Object.entries(AppState.landmarks).forEach(([key, loc]) => {
    const isCity = loc.zone.startsWith('CityLink');
    if (!lockedOut && ((isHop && !isCity) || (!isHop))) {
      const optP = document.createElement('option');
      optP.value = key;
      optP.textContent = loc.custom ? `📍 ${loc.name}` : `${loc.name} (${loc.zone})`;
      if (loc.custom) pickupSelect.insertBefore(optP, pickupSelect.firstChild);
      else pickupSelect.appendChild(optP);

      if (!loc.custom) {
        const optD = document.createElement('option');
        optD.value = key;
        optD.textContent = `${loc.name} (${loc.zone})`;
        dropSelect.appendChild(optD);
      }
    }
  });

  pickupSelect.value = AppState.pickupKey || 'uni_mall';
  if (pickupSelect.value !== AppState.pickupKey && pickupSelect.options.length) {
    pickupSelect.value = pickupSelect.options[pickupSelect.options.length > 1 && pickupSelect.options[0].value === CURRENT_LOCATION_KEY ? 1 : 0].value;
    AppState.pickupKey = pickupSelect.value;
  }
  const hasOption = (key) => Array.from(dropSelect.options).some((o) => o.value === key);
  if (isHop) {
    dropSelect.value = hasOption(AppState.dropKey) ? AppState.dropKey : 'block_34';
  } else if (AppState.customDrop) {
    dropSelect.value = '';
  } else {
    const isCityKey = (key) => hasOption(key) && Boolean(AppState.landmarks[key] && AppState.landmarks[key].zone.startsWith('CityLink'));
    dropSelect.value = isCityKey(AppState.dropKey) ? AppState.dropKey : 'jalandhar_bus_stand';
  }
  if (isHop && dropSelect.value === pickupSelect.value) {
    // Never default the destination to the pickup itself (e.g. when standing at Block 34).
    const other = Array.from(dropSelect.options).find((o) => o.value !== pickupSelect.value);
    if (other) dropSelect.value = other.value;
  }
  AppState.dropKey = AppState.customDrop && !isHop ? CUSTOM_PLACE_KEY : dropSelect.value;
  syncDropSearchInput();

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
    if (location.custom) return;
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

  const p = pickupPlace();
  if (p) {
    AppState.homeMarkers.pickup = L.marker([p.lat, p.lng], { icon: pinIcon('#10B981', 'P') })
      .addTo(AppState.homeMap).bindPopup(`<b>Pickup:</b> ${esc(p.name)}`);
  }
  const d = dropPlace();
  if (d) {
    AppState.homeMarkers.drop = L.marker([d.lat, d.lng], { icon: pinIcon('#FF7C00', 'D') })
      .addTo(AppState.homeMap).bindPopup(`<b>Destination:</b> ${esc(d.name)}`);
  }
  drawHomeRoute();
}

// Shows drivers who are actually online and free, as reported by the server.
async function refreshNearbyDrivers() {
  if (!AppState.homeMap || !AppState.currentUser) return;
  try {
    const { ok, data } = await apiGet('/api/drivers/nearby');
    if (!ok) return;
    AppState.availableDrivers = data.drivers;
    renderAvailableRiders();
    // Markers are moved, not redrawn, so drivers glide across the map between updates.
    const seen = new Set();
    data.drivers.forEach((dr) => {
      seen.add(dr.key);
      const existing = AppState.driverMarkers[dr.key];
      if (existing) {
        existing.setLatLng([dr.lat, dr.lng]);
      } else {
        AppState.driverMarkers[dr.key] = L.marker([dr.lat, dr.lng], { icon: vehicleIcon(dr.category) })
          .addTo(AppState.homeMap)
          .bindPopup(`<b>${esc(dr.first_name)}</b> (${esc(dr.category)})<br>Available`);
      }
    });
    Object.keys(AppState.driverMarkers).forEach((key) => {
      if (!seen.has(key)) {
        AppState.homeMap.removeLayer(AppState.driverMarkers[key]);
        delete AppState.driverMarkers[key];
      }
    });
  } catch (err) {
    console.error('Failed to load nearby drivers:', err);
  }
}

// --- Destination (CityLink: any place, or a popular one) and route drawing ---
function pickupPlace() {
  if (AppState.pickupKey === NOT_IN_CAMPUS_KEY) return null;
  return AppState.landmarks[AppState.pickupKey] || null;
}

function dropPlace() {
  if (AppState.selectedScope === 'citylink' && AppState.customDrop) return AppState.customDrop;
  return AppState.landmarks[AppState.dropKey] || null;
}

function dropPayload() {
  if (AppState.selectedScope === 'citylink' && AppState.customDrop) {
    const d = AppState.customDrop;
    return { drop_key: CUSTOM_PLACE_KEY, drop_lat: d.lat, drop_lng: d.lng, drop_name: d.name };
  }
  return { drop_key: $('drop-select').value };
}

function syncDropSearchInput() {
  const input = $('drop-search-input');
  if (!input || document.activeElement === input) return;
  const d = dropPlace();
  input.value = d ? d.name : '';
}

function popularDestinations() {
  return Object.entries(AppState.landmarks)
    .filter(([, loc]) => loc.zone.startsWith('CityLink') && !loc.custom)
    .map(([key, loc]) => ({ key, name: loc.name, detail: loc.zone.replace('CityLink-', '') }));
}

// Typed words are matched against the popular destinations first (whole words, then word starts).
function matchPopular(query) {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return popularDestinations();
  return popularDestinations()
    .map((p) => {
      const hay = `${p.name} ${p.detail}`.toLowerCase();
      const tokens = hay.split(/[^a-z0-9]+/);
      let score = 0;
      words.forEach((w) => {
        if (tokens.includes(w)) score += 2;
        else if (tokens.some((t) => t.startsWith(w))) score += 1.5;
        else if (hay.includes(w)) score += 1;
        else score -= 10;
      });
      if (hay.startsWith(words[0])) score += 1;
      return { ...p, score };
    })
    .filter((p) => p.score > 0)
    .sort((a, b) => b.score - a.score);
}

function suggestionButton(name, detail, onPick) {
  const b = document.createElement('button');
  b.type = 'button';
  b.className = 'drop-suggestion';
  b.setAttribute('role', 'option');
  const n = document.createElement('span');
  n.className = 'drop-suggestion-name';
  n.textContent = name;
  b.appendChild(n);
  if (detail) {
    const d = document.createElement('span');
    d.className = 'drop-suggestion-detail';
    d.textContent = detail;
    b.appendChild(d);
  }
  b.addEventListener('click', onPick);
  return b;
}

function renderDropSuggestions(query, places, status) {
  const box = $('drop-suggestions');
  box.replaceChildren();
  const addHeading = (text) => {
    const h = document.createElement('div');
    h.className = 'drop-suggestions-heading';
    h.textContent = text;
    box.appendChild(h);
  };
  const popular = matchPopular(query);
  if (popular.length) {
    addHeading('POPULAR DESTINATIONS');
    popular.slice(0, query ? 4 : 30).forEach((p) => {
      box.appendChild(suggestionButton(p.name, p.detail, () => chooseDrop({ key: p.key })));
    });
  }
  if (places && places.length) {
    addHeading('PLACES');
    places.forEach((p) => {
      box.appendChild(suggestionButton(p.name, p.detail, () => chooseDrop({ custom: { name: p.label || p.name, lat: p.lat, lng: p.lng } })));
    });
  }
  if (status) {
    const st = document.createElement('div');
    st.className = 'drop-suggestions-status';
    st.textContent = status;
    box.appendChild(st);
  }
  box.classList.toggle('hidden', !box.children.length);
}

function chooseDrop(choice) {
  if (choice.custom) {
    AppState.customDrop = choice.custom;
    AppState.dropKey = CUSTOM_PLACE_KEY;
    $('drop-select').value = '';
  } else {
    AppState.customDrop = null;
    AppState.dropKey = choice.key;
    $('drop-select').value = choice.key;
    syncCustomLocationDropdowns();
  }
  $('drop-suggestions').classList.add('hidden');
  $('drop-search-input').blur();
  syncDropSearchInput();
  fetchFareQuotes();
}

let dropSearchTimer = null;
async function searchDropPlaces(query) {
  const seq = ++AppState.placeSeq;
  renderDropSuggestions(query, [], query.length >= 3 ? 'Searching…' : '');
  if (query.length < 3) return;
  const pos = AppState.userPosition;
  const near = pos ? `&lat=${pos.lat}&lng=${pos.lng}` : '';
  const { ok, data } = await apiGet(`/api/places/search?q=${encodeURIComponent(query)}${near}`);
  if (seq !== AppState.placeSeq) return;  // a newer search replaced this one
  if (!ok) {
    renderDropSuggestions(query, [], 'Place search is unavailable right now. Pick a popular destination.');
    return;
  }
  renderDropSuggestions(query, data.places, data.places.length ? '' : 'No other places found. Try a different spelling.');
}

function initDropSearch() {
  const input = $('drop-search-input');
  input.addEventListener('focus', () => {
    input.select();
    // On small phones the suggestions open below the fold, so bring the box to the top of the panel.
    setTimeout(() => input.scrollIntoView({ block: 'start', behavior: 'smooth' }), 150);
    const current = dropPlace();
    searchDropPlaces(current && input.value === current.name ? '' : input.value.trim());
  });
  input.addEventListener('input', () => {
    clearTimeout(dropSearchTimer);
    const q = input.value.trim();
    if (q.length < 3) { searchDropPlaces(q); return; }
    renderDropSuggestions(q, [], 'Searching…');
    dropSearchTimer = setTimeout(() => searchDropPlaces(q), 650);
  });
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      const first = $('drop-suggestions').querySelector('.drop-suggestion');
      if (first) first.click();
    } else if (event.key === 'Escape') {
      $('drop-suggestions').classList.add('hidden');
      input.blur();
    }
  });
  document.addEventListener('click', (event) => {
    if (!event.target.closest('#drop-search-box')) {
      $('drop-suggestions').classList.add('hidden');
      syncDropSearchInput();
    }
  });
}

// Road route for the selected trip, drawn on the home map with its distance and time.
const roadRouteCache = {};
async function fetchRoadRoute(a, b) {
  const key = [a.lat, a.lng, b.lat, b.lng].map((v) => Number(v).toFixed(3)).join(',');
  if (roadRouteCache[key]) return roadRouteCache[key];
  const q = `a_lat=${a.lat}&a_lng=${a.lng}&b_lat=${b.lat}&b_lng=${b.lng}`;
  const { ok, data } = await apiGet(`/api/route?${q}`);
  if (!ok) return null;
  roadRouteCache[key] = data;
  return data;
}

async function drawHomeRoute() {
  const map = AppState.homeMap;
  if (!map || !AppState.currentUser) return;
  const p = pickupPlace();
  const d = dropPlace();
  const chip = $('route-info-chip');
  const key = p && d ? [p.lat, p.lng, d.lat, d.lng].join(',') : '';
  if (key === AppState.routeKey) return;
  AppState.routeKey = key;
  AppState.roadMinutes = null;
  if (AppState.routeLine) { map.removeLayer(AppState.routeLine); AppState.routeLine = null; }
  chip.classList.add('hidden');
  if (!key) return;

  const seq = ++AppState.routeSeq;
  const straight = [[p.lat, p.lng], [d.lat, d.lng]];
  AppState.routeLine = L.polyline(straight, { color: '#FF7C00', weight: 4, opacity: 0.7, dashArray: '6, 8' }).addTo(map);
  map.fitBounds(straight, { padding: [40, 40] });

  const route = await fetchRoadRoute(p, d);
  if (!route || seq !== AppState.routeSeq) return;
  if (AppState.routeLine) map.removeLayer(AppState.routeLine);
  AppState.routeLine = L.polyline(route.points, { color: '#FF7C00', weight: 5, opacity: 0.9 }).addTo(map);
  map.fitBounds(AppState.routeLine.getBounds(), { padding: [40, 40] });
  chip.textContent = `${route.km} km • ${route.minutes} min by road`;
  chip.classList.remove('hidden');
  AppState.roadMinutes = route.minutes;
  if (AppState.quotes) updateServicesDisplay({ quotes: AppState.quotes });
}

// Zone badge and class-change rush banner reflect the real pickup zone and the real clock.
function isRushHour(now = new Date()) {
  const m = now.getHours() * 60 + now.getMinutes();
  return (m >= 510 && m <= 555) || (m >= 765 && m <= 810) || (m >= 1005 && m <= 1065);
}

function updateZoneBanner() {
  const zoneText = $('current-zone-text');
  const rush = $('rush-indicator');
  if (!zoneText || !rush) return;
  const pickup = AppState.landmarks[AppState.pickupKey];
  if (AppState.pickupKey === NOT_IN_CAMPUS_KEY || (pickup && pickup.custom)) {
    zoneText.textContent = 'Outside campus';
  } else {
    zoneText.textContent = pickup ? pickup.zone.replace(/^Zone-/, '') + ' Zone' : 'Select pickup';
  }
  rush.classList.toggle('hidden', !isRushHour());
}

// Live availability of riders (drivers) for the selected vehicle on the home booking panel.
function renderAvailableRiders() {
  const note = $('available-riders-note');
  if (!note) return;
  const drivers = AppState.availableDrivers;
  if (!drivers) { note.classList.add('hidden'); return; }
  const count = drivers.filter((d) => d.category === AppState.selectedService).length;
  const pickup = pickupPlace();
  const drop = dropPlace();
  const path = pickup && drop ? `${pickup.name} → ${drop.name}` : 'your route';
  note.classList.remove('hidden');
  note.classList.toggle('none', count === 0);
  note.textContent = count
    ? `${count} ${AppState.selectedService} rider${count > 1 ? 's' : ''} available now for ${path}`
    : `No ${AppState.selectedService} riders online right now. You'll be queued for ${path}.`;
}

// --- Server-side fare quotes ---
async function fetchFareQuotes() {
  if (!AppState.currentUser) return;

  const pickupKey = $('pickup-select').value;
  const extra = dropPayload();
  AppState.pickupKey = pickupKey;
  AppState.dropKey = extra.drop_key;
  renderMapLandmarks();
  renderAvailableRiders();
  updateZoneBanner();

  if (pickupKey === NOT_IN_CAMPUS_KEY) {
    AppState.quotes = null;
    $('selected-service-summary').textContent = 'You are not in campus. Switch to CityLink to ride from here.';
    $('wallet-warning-banner').classList.add('hidden');
    return;
  }

  try {
    const { ok, data } = await apiPost('/api/rides/quote', {
      pickup_key: pickupKey,
      ...extra,
      scope: AppState.selectedScope,
      ...pickupCoords(pickupKey)
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
    $(`eta-${srv}`).textContent = `~${AppState.roadMinutes || q.estimated_minutes} min`;
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
  if (!(await showConfirm('Remove this emergency contact?'))) return;
  const res = await apiFetch(`/api/user/emergency-contacts/${encodeURIComponent(contactId)}`, { method: 'DELETE' });
  if (!res.ok) {
    showAlert('Could not remove contact');
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
    showAlert($('selected-service-summary').textContent || 'Choose a valid pickup and drop first.');
    return;
  }
  if (!quote.has_sufficient_balance) {
    $('topup-custom-input').value = Math.ceil(quote.deficit + 20);
    openModal('modal-topup');
    return;
  }

  const btn = $('find-ride-btn');
  btn.disabled = true;
  $('find-ride-text').textContent = 'Sending request...';

  try {
    const { ok, status, data } = await apiPost('/api/rides/book', {
      pickup_key: AppState.pickupKey,
      ...dropPayload(),
      service_type: AppState.selectedService,
      scope: AppState.selectedScope,
      ...pickupCoords(AppState.pickupKey)
    });

    if (!ok) {
      if (status === 402) {
        $('topup-custom-input').value = Math.ceil((data.deficit || 0) + 20);
        openModal('modal-topup');
      } else if (data.code === 'CONCURRENT_RIDE_EXISTS') {
        showAlert('You already have a ride in progress. Check the Activity tab to view or cancel it.');
        await checkActiveRide();
        switchView('view-activity');
      } else {
        showAlert(data.error || 'Failed to book ride');
      }
      return;
    }

    setWalletBalance(data.wallet_balance);
    const n = Number(data.drivers_notified || 0);
    showAlert(n > 0
      ? `Request sent to ${n} nearby driver${n > 1 ? 's' : ''}. You'll be matched as soon as one accepts.${data.is_priority ? ' (Teacher Priority Applied ⭐)' : ''}
Your fare is held and fully refunded if you cancel.`
      : `No drivers are online right now, but your request is open and the first driver to accept gets it.${data.is_priority ? ' (Teacher Priority Applied ⭐)' : ''}
Your fare is held and fully refunded if you cancel.`);
    await checkActiveRide();
    switchView('view-activity');
  } catch (err) {
    console.error('Book ride failed:', err);
    showAlert('Could not reach the server. Please try again.');
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
  if (!(await showConfirm(question))) return;

  const { ok, data } = await apiPost(`/api/rides/${encodeURIComponent(ride.id)}/cancel`);
  if (!ok) {
    showAlert(data.error || 'Could not cancel the ride');
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
        <strong class="tx-amount ${amount < 0 ? 'negative' : 'positive'}">${amount < 0 ? '-' : '+'}₹${Math.abs(amount).toFixed(2)}</strong>
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
  $('sim-telemetry-btn').classList.toggle('hidden', !(AppState.config && AppState.config.demo_simulation) || isRider);
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
    fitActiveMap(ride);
  }
  AppState.trackRouteAt = 0;
  AppState.tripRouteFor = '';
  updateLiveTracking(ride);
  startRideTracking();
}

// While the driver is coming, frame driver + pickup; once the trip starts, driver + destination.
function fitActiveMap(ride) {
  if (!AppState.activeMap) return;
  const target = rideTarget(ride);
  const points = [[target.lat, target.lng]];
  if (ride.driver_live_lat != null) points.push([ride.driver_live_lat, ride.driver_live_lng]);
  else points.push([ride.pickup_lat, ride.pickup_lng], [ride.drop_lat, ride.drop_lng]);
  AppState.activeFit = points;
  AppState.activeMap.fitBounds(points, { padding: [50, 50], maxZoom: 16 });
}

function clearActiveMap() {
  Object.values(AppState.activeMarkers).forEach((m) => AppState.activeMap.removeLayer(m));
  AppState.activeMarkers = {};
  if (AppState.activePolyline) {
    AppState.activeMap.removeLayer(AppState.activePolyline);
    AppState.activePolyline = null;
  }
  if (AppState.activeLeg) {
    AppState.activeMap.removeLayer(AppState.activeLeg);
    AppState.activeLeg = null;
  }
}

// --- Live trip tracking (driver position, road route and ETA, refreshed every few seconds) ---
function startRideTracking() {
  if (AppState.timers.track) return;
  AppState.timers.track = setInterval(trackActiveRide, 5000);
}

function stopRideTracking() {
  clearInterval(AppState.timers.track);
  AppState.timers.track = null;
}

function rideTarget(ride) {
  const arriving = ride.status === 'matched' || ride.status === 'arriving';
  return arriving
    ? { lat: ride.pickup_lat, lng: ride.pickup_lng, arriving }
    : { lat: ride.drop_lat, lng: ride.drop_lng, arriving };
}

async function updateLiveTracking(ride) {
  if (!AppState.activeMap) return;
  const hasDriver = ride.driver_live_lat != null && ride.driver_live_lng != null;
  const driverAt = hasDriver ? [ride.driver_live_lat, ride.driver_live_lng] : null;
  if (driverAt && AppState.activeMarkers.driver) AppState.activeMarkers.driver.setLatLng(driverAt);

  const target = rideTarget(ride);
  if (!driverAt) return;
  const km = distanceKm(driverAt[0], driverAt[1], target.lat, target.lng);
  const sub = $('cockpit-telemetry-sub');
  const baseText = target.arriving ? 'Driver is' : 'Destination is';
  sub.textContent = `${baseText} ${km.toFixed(1)} km away`;

  // The road route and ETA come from the server; asked at most every 15 seconds.
  const now = Date.now();
  if (now - AppState.trackRouteAt < 15000) return;
  AppState.trackRouteAt = now;
  const route = await fetchRoadRoute({ lat: driverAt[0], lng: driverAt[1] }, target);
  if (!route || !AppState.activeRide || AppState.activeRide.id !== ride.id) return;
  if (AppState.activeLeg) AppState.activeMap.removeLayer(AppState.activeLeg);
  AppState.activeLeg = L.polyline(route.points, { color: '#2563EB', weight: 5, opacity: 0.85 }).addTo(AppState.activeMap);
  sub.textContent = `${baseText} ${route.km} km away • about ${route.minutes} min`;

  // Once per ride, the whole trip (pickup to destination) replaces the straight dashed line.
  if (AppState.tripRouteFor !== ride.id) {
    AppState.tripRouteFor = ride.id;
    const trip = await fetchRoadRoute({ lat: ride.pickup_lat, lng: ride.pickup_lng }, { lat: ride.drop_lat, lng: ride.drop_lng });
    if (trip && AppState.activeRide && AppState.activeRide.id === ride.id) {
      if (AppState.activePolyline) AppState.activeMap.removeLayer(AppState.activePolyline);
      AppState.activePolyline = L.polyline(trip.points, { color: '#FF7C00', weight: 4, opacity: 0.6, dashArray: '2, 8' }).addTo(AppState.activeMap);
    }
  }
}

async function trackActiveRide() {
  if (!AppState.activeRide || !AppState.currentUser || AppState.activeCockpitRouteId) {
    stopRideTracking();
    return;
  }
  if (AppState.trackBusy) return;
  AppState.trackBusy = true;
  try {
    const { ok, data } = await apiGet('/api/rides/active');
    if (!ok) return;
    const ride = data.active_ride;
    if (!AppState.activeRide) return;

    if (!ride || ride.id !== AppState.activeRide.id) {
      // The ride finished or was cancelled from the other side.
      AppState.activeRide = null;
      stopRideTracking();
      clearActiveMap();
      showAlert('This ride has ended. You can see it in your Activity tab.');
      switchView('view-ride');
      await refreshCurrentUser();
      await loadDriverEarnings();
      fetchFareQuotes();
      return;
    }
    if (ride.status === 'queued') {
      // The driver released the ride: it is waiting for another driver again.
      stopRideTracking();
      await checkActiveRide();
      switchView('view-activity');
      return;
    }
    const changed = ride.status !== AppState.activeRide.status;
    AppState.activeRide = ride;
    if (changed) renderActiveRide(ride);
    else updateLiveTracking(ride);
  } finally {
    AppState.trackBusy = false;
  }
}

// --- Demo-only simulation step (server refuses it unless DEMO_SIMULATE_MOVEMENT=1) ---
async function advanceTelemetryStep() {
  if (AppState.activeCockpitRouteId) {
    const { ok, data } = await apiPost(`/api/routes/${encodeURIComponent(AppState.activeCockpitRouteId)}/telemetry-step`);
    if (!ok) {
      showAlert(data.error || 'Could not update the trip');
      return;
    }
    if (AppState.activeMarkers.car) AppState.activeMarkers.car.setLatLng([data.current_lat, data.current_lng]);
    $('cockpit-telemetry-sub').textContent = `${data.distance_remaining_km} km remaining • Moving towards destination`;
    return;
  }

  if (!AppState.activeRide) return;
  const { ok, data } = await apiPost(`/api/rides/${encodeURIComponent(AppState.activeRide.id)}/telemetry-step`);
  if (!ok) {
    showAlert(data.error || 'Could not update the ride');
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
      showAlert(data.error || 'Could not complete the trip');
      return;
    }
    showAlert(`🏁 Carpool trip completed! Host payout of ₹${money(data.driver_payout)} added to your wallet.`);
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
    showAlert(data.error || 'Could not complete the ride');
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
    showAlert(data.error || 'Could not save your rating');
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
    return { lat: ride.driver_live_lat, lng: ride.driver_live_lng, name: `En route from ${ride.pickup_name}`, approximate: true };
  }
  if (ride) {
    return { lat: ride.pickup_lat, lng: ride.pickup_lng, name: ride.pickup_name, approximate: true };
  }
  // 3. No fix and no ride: say so instead of guessing a place.
  return { lat: null, lng: null, name: 'Location unavailable', approximate: true };
}

const DELIVERY_LABELS = {
  sent: '✓ Texted',
  failed: '✗ Sending failed',
  not_sent: 'Not texted',
  rate_limited: 'Not texted (limit reached)',
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
      approximate: Boolean(loc.approximate),
      location_name: loc.name
    });
    if (!ok) {
      showAlert(`${data.error || 'Could not record the SOS.'}\nCall campus security now: ${AppState.config.security_hotline}`);
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
    showAlert(`Could not reach the server. Call campus security now: ${AppState.config.security_hotline}`);
  } finally {
    sosBtn.disabled = false;
  }
}

// --- Share live trip link ---
function handleShareTrip() {
  const ride = AppState.activeRide;
  if (!ride || !ride.share_token) {
    showAlert('Live sharing is available for on-demand rides.');
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
  showAlert(`₹${money(data.amount_credited)} added. New balance: ₹${money(data.new_balance)}`);
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
  showAlert(`₹${money(data.amount_credited)} added. New balance: ₹${money(data.new_balance)}`);
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
    AppState.driverOnline = data.is_online;
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
  const pos = wantOnline ? await getBrowserPosition() : null;
  const { ok, data } = await apiPost('/api/driver/toggle-online', { is_online: wantOnline, ...(pos || {}) });
  if (!ok) {
    event.target.checked = !wantOnline;
    if (data.code === 'VEHICLE_REQUIRED') {
      openVehicleModal();
    } else {
      showAlert(data.error || 'Could not change your status');
    }
    return;
  }
  AppState.driverOnline = data.is_online;
  $('online-status-text').textContent = data.is_online ? 'Online' : 'Offline';
  if (data.is_online && !pos) {
    showAlert('Allow location access in your browser. Without your live position riders cannot be matched to you.');
  }
  refreshNearbyDrivers();
  fetchFareQuotes();
  if (data.is_online) loadDriverRequests(); else setRequestsDot(0);
}

async function loadDriverRequests() {
  const list = $('driver-requests-list');
  const { ok, data } = await apiGet('/api/driver/requests');
  if (!ok) {
    setRequestsDot(0);
    list.innerHTML = `<p class="empty-state">${esc(data.error || 'Requests unavailable')}</p>`;
    return;
  }
  if (data.offline) {
    setRequestsDot(0);
    list.innerHTML = '<p class="empty-state">Go online to see ride requests.</p>';
    return;
  }
  setRequestsDot(data.requests.length);
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
        <div class="veh-sub">${r.distance_to_pickup_km == null ? 'Distance unknown' : `${Number(r.distance_to_pickup_km)} km to pickup`} • ${Number(r.trip_km)} km trip</div>
      </div>
      <button class="btn-sm-primary" data-action="accept-ride" data-id="${esc(r.id)}">Accept</button>
    `;
    list.appendChild(card);
  });
}

async function handleAcceptRide(rideId) {
  const { ok, data } = await apiPost('/api/driver/accept', { ride_id: rideId });
  if (!ok) {
    showAlert(data.error || 'Could not accept this ride');
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
        <div style="font-size:0.75rem; color:var(--text-secondary);">Departure: <strong>${esc(fmtDeparture(r.departure_time))}</strong></div>
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
    if (!(await showConfirm('Leave this trip? Your seat fare will be refunded.'))) return;
    await routeSimpleAction(routeId, 'leave', (d) => `Booking cancelled. ₹${money(d.refunded)} refunded.`);
  } else if (action === 'cancel-route') {
    if (!(await showConfirm('Cancel this trip? Every passenger will be refunded.'))) return;
    await routeSimpleAction(routeId, 'cancel', (d) => `Trip cancelled. ${d.refunded_bookings} booking(s) refunded.`);
  }
}

async function routeSimpleAction(routeId, verb, successMessage) {
  const { ok, data } = await apiPost(`/api/routes/${encodeURIComponent(routeId)}/${verb}`);
  if (!ok) {
    showAlert(data.error || 'Something went wrong');
    return;
  }
  showAlert(successMessage(data));
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
      showAlert(data.error || 'Could not join route');
    }
    return;
  }
  setWalletBalance(data.wallet_balance);
  if (data.is_pinned) {
    showAlert('🎉 ALL SEATS ARE NOW FULL!\n\nThis scheduled ride has been automatically PINNED and is confirmed for departure.');
  } else {
    showAlert(`Confirmed! ${seats} seat(s) booked for ₹${money(data.fare_paid)}. ${data.remaining_seats} seat(s) remaining.`);
  }
  await loadCityLinkRoutes();
}

function toLocalInputValue(date) {
  const pad = (n) => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function defaultDeparture() {
  return toLocalInputValue(new Date(Date.now() + 2 * 3600 * 1000));
}

function fmtDeparture(value) {
  const d = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(value || '') ? new Date(value) : null;
  return d ? d.toLocaleString([], { weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }) : value;
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
      showAlert(data.error || 'Could not publish the route');
    }
    return;
  }
  closeModal('modal-post-route');
  await loadCityLinkRoutes();
  showAlert(`Route scheduled! Offering ${data.total_seats} seat(s) from ${payload.origin} to ${payload.destination}.`);
}

// --- During-ride cockpit for carpools ---
async function startConfirmedRoute(routeId) {
  const { ok, data } = await apiPost(`/api/routes/${encodeURIComponent(routeId)}/start`);
  if (!ok) {
    showAlert(data.error || 'Could not start the trip');
    return;
  }
  await openDuringRideCockpit(routeId);
}

async function openDuringRideCockpit(routeId) {
  try {
    const { ok, data } = await apiGet(`/api/routes/${encodeURIComponent(routeId)}/live`);
    if (!ok || !data.cockpit) {
      showAlert(data.error || 'Could not open the trip');
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
      AppState.activeFit = [oCoords, dCoords];
      AppState.activeMap.fitBounds(AppState.activeFit, { padding: [50, 50] });
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
    // The map was sized while hidden: fix its size, then frame the trip again.
    setTimeout(() => {
      AppState.activeMap.invalidateSize();
      if (AppState.activeFit) AppState.activeMap.fitBounds(AppState.activeFit, { padding: [50, 50], maxZoom: 16 });
    }, 150);
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
      renderAvailableRiders();
      if (AppState.quotes && AppState.currentUser) {
        updateServicesDisplay({ quotes: AppState.quotes });
      }
    });
  });

  initDropSearch();
  $('pickup-select').addEventListener('change', fetchFareQuotes);
  $('drop-select').addEventListener('change', fetchFareQuotes);
  $('swap-locations-btn').addEventListener('click', () => {
    const p = $('pickup-select');
    const d = $('drop-select');
    if (p.value === CURRENT_LOCATION_KEY || AppState.customDrop) return;
    if (!Array.from(p.options).some((o) => o.value === d.value) || !Array.from(d.options).some((o) => o.value === p.value)) return;
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
    if (AppState.homeMap) {
      const c = AppState.userPosition ? [AppState.userPosition.lat, AppState.userPosition.lng] : LPU_COORDS;
      AppState.homeMap.setView(c, 16);
    }
  });

  // Booking and live ride
  $('find-ride-btn').addEventListener('click', handleBookRide);
  $('cancel-queued-ride-btn').addEventListener('click', handleCancelRide);
  $('cancel-ride-btn').addEventListener('click', handleCancelRide);
  $('sim-telemetry-btn').addEventListener('click', advanceTelemetryStep);
  if (!(AppState.config && AppState.config.demo_simulation)) $('sim-telemetry-btn').classList.add('hidden');
  $('route-time-input').value = defaultDeparture();
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
      showAlert('Live tracking link copied to clipboard!');
    } catch (err) {
      showAlert('Copy failed - select the link and copy it manually.');
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
        showAlert(data.error || 'Could not change role');
        return;
      }
      AppState.currentUser.role = newRole;
      if (newRole === 'rider') setRequestsDot(0);
      updateUserUI();
      if (newRole !== 'rider' && !AppState.currentUser.vehicle) {
        openVehicleModal();
      } else {
        loadDriverEarnings();
      }
    });
  });
}
