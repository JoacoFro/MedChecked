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
});importScripts('https://www.gstatic.com/firebasejs/10.12.2/firebase-app-compat.js');
importScripts('https://www.gstatic.com/firebasejs/10.12.2/firebase-messaging-compat.js');

firebase.initializeApp({
  apiKey: 'AIzaSyCZMbFBIDSReRREJ7yBcqOcZPiCOPTmVbU',
  authDomain: 'astrana-2dba0.firebaseapp.com',
  projectId: 'astrana-2dba0',
  storageBucket: 'astrana-2dba0.firebasestorage.app',
  messagingSenderId: '809775279655',
  appId: '1:809775279655:web:b53a39db43aff582b1a802'
});

const messaging = firebase.messaging();

messaging.onBackgroundMessage((payload) => {
  const notificationTitle = payload.notification.title;
  const notificationOptions = {
    body: payload.notification.body,
    icon: '/astrana/icon-192.png'
  };

  self.registration.showNotification(notificationTitle, notificationOptions);
});