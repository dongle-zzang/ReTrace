import { request as httpRequest } from 'node:http'
import type { IncomingMessage, Server } from 'node:http'
import { request as httpsRequest } from 'node:https'
import type { Duplex } from 'node:stream'
import tailwindcss from '@tailwindcss/vite'
import { loadEnv } from 'vite'

const env = loadEnv(process.env.NODE_ENV || 'development', process.cwd(), '')
const backendProxyTarget = (process.env.NUXT_PUBLIC_BACKEND_BASE_URL || env.NUXT_PUBLIC_BACKEND_BASE_URL)
  ?.trim()
  .replace(/\/+$/, '')

function isWebSocketPath(url = '') {
  return url === '/ws' || url.startsWith('/ws?') || url.startsWith('/ws/')
}

// Dev only: forward the /ws upgrade to the ReTrace origin. Nitro's devProxy and Vite's
// middleware-mode proxy don't handle upgrades, and the server only accepts its own Origin.
function proxyWebSocket(target: string) {
  const upstreamUrl = new URL(target)
  const request = upstreamUrl.protocol === 'https:' ? httpsRequest : httpRequest
  return (req: IncomingMessage, socket: Duplex, head: Buffer) => {
    const upstream = request({
      protocol: upstreamUrl.protocol,
      hostname: upstreamUrl.hostname,
      port: upstreamUrl.port || undefined,
      path: req.url,
      method: req.method,
      headers: { ...req.headers, host: upstreamUrl.host, origin: upstreamUrl.origin },
    })
    const writeHead = (statusLine: string, rawHeaders: string[]) => {
      let text = statusLine + '\r\n'
      for (let index = 0; index < rawHeaders.length; index += 2) text += `${rawHeaders[index]}: ${rawHeaders[index + 1]}\r\n`
      socket.write(text + '\r\n')
    }
    upstream.on('upgrade', (response, upstreamSocket, upstreamHead) => {
      writeHead(`HTTP/1.1 101 ${response.statusMessage || 'Switching Protocols'}`, response.rawHeaders)
      if (upstreamHead.length) socket.write(upstreamHead)
      if (head.length) upstreamSocket.write(head)
      upstreamSocket.pipe(socket).pipe(upstreamSocket)
      upstreamSocket.on('error', () => socket.destroy())
      socket.on('error', () => upstreamSocket.destroy())
      upstreamSocket.on('close', () => socket.destroy())
      socket.on('close', () => upstreamSocket.destroy())
    })
    upstream.on('response', (response) => {
      // Upgrade refused (e.g. 403 Origin check): pass the status through and close.
      socket.end(`HTTP/1.1 ${response.statusCode} ${response.statusMessage ?? ''}\r\nConnection: close\r\nContent-Length: 0\r\n\r\n`)
      response.resume()
    })
    upstream.on('error', () => socket.destroy())
    socket.on('error', () => upstream.destroy())
    upstream.end()
  }
}

export default defineNuxtConfig({
  css: ['~/assets/css/main.css'],
  modules: ['shadcn-nuxt'],
  shadcn: {
    prefix: 'Ui',
    componentDir: '@/components/ui',
  },
  vite: {
    plugins: [tailwindcss()],
  },
  runtimeConfig: {
    public: {
      // Per-camera JPEG rate requested over /ws (1-30). Override with NUXT_PUBLIC_PREVIEW_VIDEO_FPS.
      previewVideoFps: 10,
    },
  },
  nitro: {
    devProxy: backendProxyTarget
      ? {
          '/api': {
            target: `${backendProxyTarget}/api`,
            changeOrigin: true,
          },
        }
      : {},
  },
  hooks: {
    listen(server: Server) {
      if (!backendProxyTarget) return
      const forward = proxyWebSocket(backendProxyTarget)
      // Nuxt's own upgrade handlers (Nitro worker) would also claim /ws, so route around them.
      const existing = server.listeners('upgrade') as ((...args: unknown[]) => void)[]
      server.removeAllListeners('upgrade')
      server.on('upgrade', (req: IncomingMessage, socket: Duplex, head: Buffer) => {
        if (isWebSocketPath(req.url)) forward(req, socket, head)
        else for (const listener of existing) listener.call(server, req, socket, head)
      })
    },
  },
  devtools: { enabled: true },
})
