import type { RealtimeMessage } from '~/types/realtime'

export type ConnectionState = 'connecting' | 'open' | 'reconnecting' | 'closed'
export type TransportState = 'waiting' | 'connecting' | 'connected' | 'reconnecting'

export interface VideoFrame {
  cameraId: string
  sequence: number
  jpeg: Uint8Array
}

type FrameListener = (frame: VideoFrame) => void

const RECONNECT_DELAY_MS = 2000

/** Binary layout: u8 version, u8 id length N, cameraId (N bytes UTF-8), u32 BE sequence, JPEG. */
export function parseVideoFrame(data: ArrayBuffer): VideoFrame | null {
  const bytes = new Uint8Array(data)
  if (bytes.length < 6 || bytes[0] !== 1) return null
  const idLength = bytes[1]!
  const jpegStart = 6 + idLength
  if (bytes.length <= jpegStart) return null
  return {
    cameraId: new TextDecoder().decode(bytes.subarray(2, 2 + idLength)),
    sequence: new DataView(data).getUint32(2 + idLength),
    jpeg: bytes.subarray(jpegStart),
  }
}

/** One WebSocket per page: metadata/status for all cameras and JPEG video for watched cameras. */
export class RealtimePreviewClient {
  private socket: WebSocket | null = null
  private listeners = new Map<string, Set<FrameListener>>()
  private retryTimer: ReturnType<typeof setTimeout> | null = null
  private flushQueued = false
  private sentSubscription: string | null = null
  private opened = false
  private closed = false

  constructor(
    private readonly path: string,
    private readonly videoFps: number,
    private readonly onMessage: (message: RealtimeMessage) => void,
    private readonly onConnection: (state: ConnectionState) => void,
  ) {
    this.connect()
  }

  get subscriptions() {
    return [...this.listeners.keys()].sort()
  }

  /** Receives JPEG frames for a camera; the subscription lasts until the returned function is called. */
  watch(cameraId: string, listener: FrameListener) {
    let set = this.listeners.get(cameraId)
    if (!set) {
      set = new Set()
      this.listeners.set(cameraId, set)
      this.queueSubscribe()
    }
    set.add(listener)
    return () => {
      const current = this.listeners.get(cameraId)
      if (!current?.delete(listener) || current.size) return
      this.listeners.delete(cameraId)
      this.queueSubscribe()
    }
  }

  // Batch watch/unwatch calls from one render into a single subscribe command.
  private queueSubscribe() {
    if (this.flushQueued) return
    this.flushQueued = true
    queueMicrotask(() => {
      this.flushQueued = false
      this.sendSubscribe()
    })
  }

  private sendSubscribe() {
    if (this.closed || this.socket?.readyState !== WebSocket.OPEN) return
    const cameraIds = this.subscriptions
    const key = cameraIds.join('\n')
    if (key === this.sentSubscription) return
    this.sentSubscription = key
    this.socket.send(JSON.stringify({ version: 1, type: 'subscribe', cameraIds, video: true, videoFps: this.videoFps }))
  }

  private connect() {
    if (this.closed) return
    const url = new URL(this.path, window.location.href)
    url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:'

    const socket = new WebSocket(url)
    socket.binaryType = 'arraybuffer'
    this.socket = socket
    this.onConnection(this.opened ? 'reconnecting' : 'connecting')

    socket.onopen = () => {
      if (this.socket !== socket) return
      this.opened = true
      this.sentSubscription = null
      this.onConnection('open')
      this.sendSubscribe()
    }
    socket.onmessage = (event) => {
      if (this.socket !== socket) return
      if (typeof event.data !== 'string') {
        const frame = event.data instanceof ArrayBuffer ? parseVideoFrame(event.data) : null
        if (frame) for (const listener of this.listeners.get(frame.cameraId) ?? []) listener(frame)
        return
      }
      try {
        const message: unknown = JSON.parse(event.data)
        if (!message || typeof message !== 'object' || !('type' in message) || typeof message.type !== 'string') return
        this.onMessage(message as RealtimeMessage)
      } catch {
        // Ignore malformed server messages without breaking other cameras.
      }
    }
    socket.onclose = () => {
      if (this.socket !== socket) return
      this.socket = null
      if (this.closed) return
      this.sentSubscription = null
      this.onConnection('reconnecting')
      this.retryTimer = setTimeout(() => {
        this.retryTimer = null
        this.connect()
      }, RECONNECT_DELAY_MS)
    }
    socket.onerror = () => socket.close()
  }

  close() {
    if (this.closed) return
    this.closed = true
    if (this.retryTimer) clearTimeout(this.retryTimer)
    this.retryTimer = null
    this.listeners.clear()
    const socket = this.socket
    this.socket = null
    socket?.close()
    this.onConnection('closed')
  }
}

type DecodableImage = { src: string; naturalWidth: number; naturalHeight: number; decode(): Promise<void> }

/**
 * Shows JPEG frames in one visible <img>, decoding one frame at a time. Each frame is decoded in
 * an offscreen Image first and only then assigned to the visible <img>, so the swap paints the
 * already-decoded picture without a blank frame. Frames that arrive while a decode is in flight
 * overwrite a single pending slot (no backlog). The previous object URL is revoked after the swap.
 */
export class JpegFrameView {
  private shownUrl: string | null = null
  private loadingUrl: string | null = null
  private pending: Uint8Array | null = null

  constructor(
    private readonly image: { src: string; removeAttribute(name: string): void },
    private readonly onShown: (size: { width: number; height: number }) => void = () => {},
    private readonly createImage: () => DecodableImage = () => new Image(),
  ) {}

  get hasFrame() {
    return this.shownUrl !== null
  }

  push(jpeg: Uint8Array) {
    if (this.loadingUrl) {
      this.pending = jpeg
      return
    }
    this.load(jpeg)
  }

  private load(jpeg: Uint8Array) {
    const url = URL.createObjectURL(new Blob([jpeg as Uint8Array<ArrayBuffer>], { type: 'image/jpeg' }))
    this.loadingUrl = url
    const decoder = this.createImage()
    decoder.src = url
    decoder.decode().then(
      () => this.show(url, decoder),
      () => this.fail(url),
    )
  }

  private show(url: string, decoder: DecodableImage) {
    if (this.loadingUrl !== url) return
    this.loadingUrl = null
    this.image.src = url
    if (this.shownUrl) URL.revokeObjectURL(this.shownUrl)
    this.shownUrl = url
    this.onShown({ width: decoder.naturalWidth, height: decoder.naturalHeight })
    this.next()
  }

  private fail(url: string) {
    URL.revokeObjectURL(url)
    if (this.loadingUrl !== url) return
    this.loadingUrl = null
    this.next()
  }

  private next() {
    const jpeg = this.pending
    this.pending = null
    if (jpeg) this.load(jpeg)
  }

  /** Drops pending work, revokes every object URL and empties the image. */
  clear() {
    this.pending = null
    for (const url of [this.loadingUrl, this.shownUrl]) if (url) URL.revokeObjectURL(url)
    this.loadingUrl = null
    this.shownUrl = null
    this.image.removeAttribute('src')
  }
}
