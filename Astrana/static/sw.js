const CACHE_NAME = 'astrana-pwa-v4';
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

// Escuchar Notificaciones Push con Acciones Interactivas
self.addEventListener('push', (event) => {
  let data = { title: '💊 Astrana: Hora de tus pastillas', body: '¿Tomaste tu medicación? Tocá para confirmar.' };
  if (event.data) {
    try {
      data = event.data.json();
    } catch (e) {
      data.body = event.data.text();
    }
  }

  const options = {
    body: data.body,
    icon: data.icon || '/astrana/icon-192.png',
    badge: data.badge || '/astrana/icon-192.png',
    vibrate: [200, 100, 200, 100, 200],
    data: data.data || {},
    requireInteraction: true,
    actions: data.actions || [
      { action: 'confirmar_toma', title: '✅ Confirmar Toma' }
    ]
  };

  event.waitUntil(self.registration.showNotification(data.title, options));
});

// Manejo del click en la notificación o sus botones
self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const notifData = event.notification.data || {};
  const action = event.action;

  if (action === 'confirmar_toma') {
    // Confirmar toma en segundo plano sin forzar la apertura de la ventana
    event.waitUntil(
      fetch('/api/astrana/pastillero/confirmar/', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json'
        },
        body: JSON.stringify({
          medicamento_id: notifData.medicamento_id || null
        })
      })
      .then(res => res.json())
      .then(resData => {
        const msg = resData.message || 'Toma registrada con éxito en la base de datos.';
        return self.registration.showNotification('✅ Astrana: Toma Confirmada', {
          body: msg,
          icon: '/astrana/icon-192.png',
          badge: '/astrana/icon-192.png',
          timeout: 4000
        });
      })
      .catch(err => {
        console.error('Error al registrar toma desde SW:', err);
      })
    );
  } else {
    // Abrir o enfocar la PWA de Astrana
    event.waitUntil(
      clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clientList) => {
        for (const client of clientList) {
          if (client.url.includes('/astrana/') && 'focus' in client) {
            return client.focus();
          }
        }
        if (clients.openWindow) {
          return clients.openWindow('/astrana/');
        }
      })
    );
  }
});