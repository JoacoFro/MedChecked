const CACHE_NAME = 'astrana-pwa-v3';
const ASSETS = [
  '/astrana/',
  '/manifest.json',
  '/astrana/icon-192.png',
  '/astrana/icon-512.png'
];

// Instalación del Service Worker
self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.addAll(ASSETS)));
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(
      keys.filter((key) => key.startsWith('astrana-pwa-') && key !== CACHE_NAME)
        .map((key) => caches.delete(key))
    )).then(() => self.clients.claim())
  );
});

// Estrategia Network First (priorizar respuestas frescas del backend)
self.addEventListener('fetch', (event) => {
  if (event.request.method !== 'GET') return;
  event.respondWith(
    fetch(event.request).catch(() => caches.match(event.request))
  );
});

// Escuchar Notificaciones Push
self.addEventListener('push', (event) => {
  const data = event.data ? event.data.json() : { title: 'Astrana', body: 'Nueva alerta recibida' };
  const options = {
    body: data.body,
    icon: '/astrana/icon-192.png',
    badge: '/astrana/icon-192.png',
    vibrate: [100, 50, 100]
  };
  event.waitUntil(self.registration.showNotification(data.title, options));
});