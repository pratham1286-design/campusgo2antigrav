// In-app calls between the two sides of an accepted ride, or a carpool host and a booked passenger.
// The phones talk directly (WebRTC) and never see each other's number. The server only passes the
// call setup between them (/api/calls); the audio never goes through it.
const Call = {
  id: null,
  role: null,        // 'caller' | 'callee'
  offer: null,       // the caller's setup, kept until the callee answers
  pc: null,
  stream: null,
  poll: null,
  clock: null,
  connectedAt: 0,
  starting: false,
  checking: false,
  polling: false,
  ignored: new Set() // calls already declined or ended on this phone
};

// Free public servers that tell each phone its own internet address, so the two can connect directly.
const CALL_ICE_SERVERS = [{ urls: ['stun:stun.l.google.com:19302', 'stun:stun1.l.google.com:19302'] }];

function callsSupported() {
  return Boolean(window.RTCPeerConnection && navigator.mediaDevices && navigator.mediaDevices.getUserMedia);
}

// One message each way: wait (briefly) until the phone knows its network addresses, so the offer
// or answer carries all of them.
function iceGathered(pc, maxMs = 3000) {
  if (pc.iceGatheringState === 'complete') return Promise.resolve();
  return new Promise((resolve) => {
    const check = () => {
      if (pc.iceGatheringState !== 'complete') return;
      pc.removeEventListener('icegatheringstatechange', check);
      resolve();
    };
    pc.addEventListener('icegatheringstatechange', check);
    setTimeout(resolve, maxMs);
  });
}

async function openMicrophone() {
  try {
    return await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
  } catch (err) {
    showAlert('CampusGo needs your microphone for calls. Allow microphone access for CampusGo and try again.');
    return null;
  }
}

function newPeerConnection(stream) {
  const pc = new RTCPeerConnection({ iceServers: CALL_ICE_SERVERS });
  stream.getTracks().forEach((track) => pc.addTrack(track, stream));
  pc.ontrack = (event) => {
    const audio = $('call-remote-audio');
    audio.srcObject = event.streams[0];
    audio.play().catch(() => {});
  };
  pc.onconnectionstatechange = () => {
    if (pc !== Call.pc) return;
    if (pc.connectionState === 'connected') callConnected();
    else if (pc.connectionState === 'disconnected') setCallStatus('Reconnecting…');
    else if (pc.connectionState === 'failed') endCall('Call could not connect. Try again on Wi-Fi or a stronger network.');
  };
  return pc;
}

// --- Call screen ---
function showCallScreen(name, status, mode) {
  $('call-name').textContent = name || 'CampusGo';
  setCallStatus(status);
  setCallMode(mode);
  $('call-screen').classList.remove('hidden');
}

// incoming: Decline + Answer; outgoing and active: Mute + End.
function setCallMode(mode) {
  $('call-accept-btn').classList.toggle('hidden', mode !== 'incoming');
  $('call-mute-btn').classList.toggle('hidden', mode === 'incoming');
  $('call-hangup-btn').textContent = mode === 'incoming' ? 'Decline' : 'End';
  const mute = $('call-mute-btn');
  mute.textContent = 'Mute';
  mute.setAttribute('aria-pressed', 'false');
}

function setCallStatus(text) {
  $('call-status').textContent = text;
}

function callConnected() {
  if (Call.connectedAt) return;
  Call.connectedAt = Date.now();
  stopRingtone();
  setCallMode('active');
  const tick = () => {
    const s = Math.floor((Date.now() - Call.connectedAt) / 1000);
    setCallStatus(`${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`);
  };
  tick();
  Call.clock = setInterval(tick, 1000);
}

// A soft two-tone ring (and vibration) while a call is coming in and the app is open.
let ringing = null;
function startRingtone() {
  stopRingtone();
  if (navigator.vibrate) navigator.vibrate([400, 200, 400, 200, 400]);
  const Ctx = window.AudioContext || window.webkitAudioContext;
  if (!Ctx) return;
  const ctx = new Ctx();
  const ring = () => {
    [0, 0.45].forEach((offset) => {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.frequency.value = offset ? 480 : 440;
      gain.gain.setValueAtTime(0.0001, ctx.currentTime + offset);
      gain.gain.exponentialRampToValueAtTime(0.25, ctx.currentTime + offset + 0.05);
      gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + offset + 0.4);
      osc.connect(gain).connect(ctx.destination);
      osc.start(ctx.currentTime + offset);
      osc.stop(ctx.currentTime + offset + 0.42);
    });
  };
  ctx.resume().catch(() => {});
  ring();
  ringing = { ctx, timer: setInterval(() => { ring(); if (navigator.vibrate) navigator.vibrate([400, 200, 400]); }, 2500) };
}

function stopRingtone() {
  if (navigator.vibrate) navigator.vibrate(0);
  if (!ringing) return;
  clearInterval(ringing.timer);
  ringing.ctx.close().catch(() => {});
  ringing = null;
}

// --- Placing a call ---
// target: { context_type: 'ride' | 'route', context_id, booking_id? (host calling a passenger) }
async function startCall(target, name) {
  if (Call.id || Call.starting) return;
  if (!callsSupported()) {
    showAlert("This phone's browser can't make in-app calls. Update Chrome and try again.");
    return;
  }
  Call.starting = true;
  try {
    const stream = await openMicrophone();
    if (!stream) return;
    Call.stream = stream;
    Call.role = 'caller';
    showCallScreen(name, 'Calling…', 'outgoing');
    Call.pc = newPeerConnection(stream);
    await Call.pc.setLocalDescription(await Call.pc.createOffer());
    await iceGathered(Call.pc);
    const { ok, data } = await apiPost('/api/calls', { ...target, offer: Call.pc.localDescription.sdp });
    if (!ok) {
      endCall(data.error || 'Could not place the call', false);
      return;
    }
    Call.id = data.call_id;
    $('call-name').textContent = data.callee_name;
    setCallStatus('Ringing…');
    Call.poll = setInterval(pollCall, 1500);
  } catch (err) {
    console.error('Call failed:', err);
    endCall('Could not place the call', Boolean(Call.id));
  } finally {
    Call.starting = false;
  }
}

// Both sides: follows the call on the server (the answer arriving, or the other side hanging up).
async function pollCall() {
  if (!Call.id || Call.polling) return;
  Call.polling = true;
  try {
    const { ok, data } = await apiGet(`/api/calls/${encodeURIComponent(Call.id)}`);
    if (!ok || !Call.id) return;
    if (data.status === 'connected' && Call.role === 'caller' && data.answer && Call.pc && !Call.pc.remoteDescription) {
      setCallStatus('Connecting…');
      await Call.pc.setRemoteDescription({ type: 'answer', sdp: data.answer });
    } else if (data.status === 'declined') {
      endCall('Call declined', false);
    } else if (data.status === 'missed') {
      endCall(Call.role === 'caller' ? 'No answer' : 'Missed call', false);
    } else if (data.status === 'ended') {
      endCall('Call ended', false);
    }
  } catch (err) {
    console.error('Call update failed:', err);
  } finally {
    Call.polling = false;
  }
}

// --- Receiving a call ---
// Checked every few seconds while the app is open, and at once when it is opened from the
// "Incoming call" notification.
async function checkIncomingCall() {
  if (!AppState.currentUser || Call.id || Call.starting || Call.checking || appHidden()) return;
  Call.checking = true;
  try {
    const { ok, data } = await apiGet('/api/calls/incoming');
    const call = ok && data.call;
    if (!call || Call.id || Call.ignored.has(call.id)) return;
    Call.id = call.id;
    Call.role = 'callee';
    Call.offer = call.offer;
    const who = call.caller_role.charAt(0).toUpperCase() + call.caller_role.slice(1);
    showCallScreen(call.caller_name, `${who} is calling…`, 'incoming');
    startRingtone();
    Call.poll = setInterval(pollCall, 2000);  // notices the caller giving up
  } catch (err) {
    console.error('Incoming call check failed:', err);
  } finally {
    Call.checking = false;
  }
}

async function answerCall() {
  if (Call.role !== 'callee' || Call.pc) return;
  stopRingtone();
  if (!callsSupported()) {
    endCall("This phone's browser can't take in-app calls.");
    return;
  }
  const stream = await openMicrophone();
  if (!stream) {
    endCall('Microphone blocked');
    return;
  }
  try {
    Call.stream = stream;
    setCallMode('active');
    setCallStatus('Connecting…');
    Call.pc = newPeerConnection(stream);
    await Call.pc.setRemoteDescription({ type: 'offer', sdp: Call.offer });
    await Call.pc.setLocalDescription(await Call.pc.createAnswer());
    await iceGathered(Call.pc);
    const { ok, data } = await apiPost(`/api/calls/${encodeURIComponent(Call.id)}/answer`, { answer: Call.pc.localDescription.sdp });
    if (!ok) endCall(data.error || 'This call has ended', false);
  } catch (err) {
    console.error('Answering failed:', err);
    endCall('Could not connect the call');
  }
}

// Hang up, decline or give up. tellServer=false when the server already knows (the other side ended it).
function endCall(message, tellServer = true) {
  const id = Call.id;
  if (id) Call.ignored.add(id);
  clearInterval(Call.poll);
  clearInterval(Call.clock);
  stopRingtone();
  if (Call.pc) Call.pc.close();
  if (Call.stream) Call.stream.getTracks().forEach((track) => track.stop());
  $('call-remote-audio').srcObject = null;
  Object.assign(Call, { id: null, role: null, offer: null, pc: null, stream: null, poll: null, clock: null, connectedAt: 0 });
  if (tellServer && id) apiPost(`/api/calls/${encodeURIComponent(id)}/end`).catch(() => {});
  setCallStatus(message || 'Call ended');
  setCallMode('ended');
  $('call-mute-btn').classList.add('hidden');
  setTimeout(() => { if (!Call.id) $('call-screen').classList.add('hidden'); }, 1500);
}

function toggleMute() {
  if (!Call.stream) return;
  const track = Call.stream.getAudioTracks()[0];
  if (!track) return;
  track.enabled = !track.enabled;
  $('call-mute-btn').textContent = track.enabled ? 'Mute' : 'Unmute';
  $('call-mute-btn').setAttribute('aria-pressed', String(!track.enabled));
}

function initCalls() {
  $('call-accept-btn').addEventListener('click', answerCall);
  $('call-hangup-btn').addEventListener('click', () => {
    if (Call.id || Call.pc) endCall(Call.role === 'callee' && !Call.pc ? 'Call declined' : 'Call ended');
    else $('call-screen').classList.add('hidden');
  });
  $('call-mute-btn').addEventListener('click', toggleMute);
  setInterval(checkIncomingCall, 4000);
  document.addEventListener('visibilitychange', checkIncomingCall);
}

document.addEventListener('DOMContentLoaded', initCalls);
