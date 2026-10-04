importScripts('https://www.gstatic.com/firebasejs/10.12.2/firebase-app-compat.js');
importScripts('https://www.gstatic.com/firebasejs/10.12.2/firebase-messaging-compat.js');

firebase.initializeApp(__FIREBASE_CONFIG__);

const messaging = firebase.messaging();

messaging.onBackgroundMessage((payload) => {
  const notification = payload.notification || payload.data || {};
  const title = notification.title || 'Astrana';
  const options = {
    body: notification.body || '',
    icon: '/astrana/icon-192.png',
    data: { url: notification.url || payload.data?.url || '/astrana/' }
  };

  self.registration.showNotification(title, options);
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const url = event.notification.data?.url || '/astrana/';
  event.waitUntil(clients.openWindow(url));
});