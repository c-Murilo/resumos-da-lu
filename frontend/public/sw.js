/* Service worker do Resumos da Lu.
   Mantém a casca do app disponível offline; a API nunca é cacheada. */

const VERSION = 'v2'
const SHELL = `shell-${VERSION}`
const ASSETS = `assets-${VERSION}`
const SHELL_URLS = ['/', '/manifest.webmanifest', '/icons/icon-192.png', '/icons/icon-512.png']

self.addEventListener('install', event => {
  event.waitUntil(caches.open(SHELL).then(cache => cache.addAll(SHELL_URLS)).then(() => self.skipWaiting()))
})

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(key => key !== SHELL && key !== ASSETS).map(key => caches.delete(key))))
      .then(() => self.clients.claim())
  )
})

self.addEventListener('fetch', event => {
  const { request } = event
  if (request.method !== 'GET') return

  const url = new URL(request.url)
  // Chamadas à API precisam de dados reais: nada de cache nem de resposta velha.
  if (url.pathname.startsWith('/api/')) return

  // Navegação: tenta a rede e cai para a casca guardada quando estiver offline.
  if (request.mode === 'navigate') {
    event.respondWith(fetch(request).catch(() => caches.match('/', { ignoreSearch: true })))
    return
  }

  // Assets do build têm hash no nome, então cache-first é seguro.
  event.respondWith(
    caches.match(request).then(cached => cached || fetch(request).then(response => {
      if (response.ok && (url.origin === self.location.origin || response.type === 'opaque')) {
        const copy = response.clone()
        caches.open(ASSETS).then(cache => cache.put(request, copy))
      }
      return response
    }))
  )
})
