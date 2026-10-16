// Shows a friendly page when CampusGo is opened without a connection. Nothing else is cached:
// rides, payments and positions always come from the network, never from a stale copy.
// Also shows push notifications (even when the app is closed) and opens the right screen on tap.
const CACHE = 'campusgo-offline-v1';

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.add('/offline.html')).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (event) => {
  event.waitUntil(caches.keys()
    .then((keys) => Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key))))
    .then(() => self.clients.claim()));
});

self.addEventListener('fetch', (event) => {
  if (event.request.mode !== 'navigate') return;
  event.respondWith(fetch(event.request).catch(() => caches.match('/offline.html')));
});

self.addEventListener('push', (event) => {
  let message = {};
  try {
    message = event.data ? event.data.json() : {};
  } catch (_) {
    message = { body: event.data ? event.data.text() : '' };
  }
  const incomingCall = message.title === 'Incoming call';
  event.waitUntil(self.registration.showNotification(message.title || 'CampusGo', {
    body: message.body || '',
    icon: '/icon-192.png',
    tag: message.tag || undefined,
    renotify: Boolean(message.tag),        // a newer update about the same ride still alerts
    requireInteraction: incomingCall,      // a call stays on screen until answered or dismissed
    vibrate: incomingCall ? [500, 250, 500, 250, 500] : [200, 100, 200],
    data: { url: message.url || '/' }
  }));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const url = new URL((event.notification.data && event.notification.data.url) || '/', self.location.origin).href;
  event.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((windows) => {
    const open = windows.find((w) => new URL(w.url).origin === self.location.origin);
    if (open) {
      open.postMessage({ type: 'open', url });
      return open.focus();
    }
    return self.clients.openWindow(url);
  }));
});
