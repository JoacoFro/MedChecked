const CACHE_NAME = 'astrana-pwa-v1';
const ASSETS = [
  '/astrana/',
  '/static/css/astrana-chat.css',
  '/static/js/astrana-chat.js',
  '/static/manifest.json'
];

// Instalación del Service Worker
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(ASSETS))
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(self.clients.claim());
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
    icon: '/static/images/astrana-icon-192.png',
    badge: '/static/images/astrana-icon-192.png',
    vibrate: [100, 50, 100]
  };
  event.waitUntil(self.registration.showNotification(data.title, options));
});