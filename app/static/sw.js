// SirisDrone service worker: makes the app installable ("Add to Home Screen") so it can be a
// share target on Android. Everything is fetched from the server as normal (nothing cached):
// the library lives on the server and must always be current.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", () => {});
