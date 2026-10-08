import type { Point } from '~/types/overlay'

/**
 * Coordinate conversion between the source video frame (normalized 0-1) and the screen.
 * Pure functions only, so overlays, editors and tests share one implementation.
 */

export interface Size {
  width: number
  height: number
}

export interface Rect extends Size {
  left: number
  top: number
}

export type ObjectFit = 'contain' | 'cover' | 'fill' | 'none' | 'scale-down'

/**
 * Where the video content is drawn inside its box, like the browser does for `object-fit` and
 * `object-position` (fractions, default centered). With `cover` the rect extends past the box.
 */
export function contentRect(
  box: Size,
  media: Size,
  fit: ObjectFit = 'contain',
  position: Point = { x: 0.5, y: 0.5 },
): Rect {
  if (!(box.width > 0 && box.height > 0 && media.width > 0 && media.height > 0)) {
    return { left: 0, top: 0, width: Math.max(0, box.width), height: Math.max(0, box.height) }
  }
  if (fit === 'fill') return { left: 0, top: 0, width: box.width, height: box.height }

  const contain = Math.min(box.width / media.width, box.height / media.height)
  const scale = fit === 'cover'
    ? Math.max(box.width / media.width, box.height / media.height)
    : fit === 'none'
      ? 1
      : fit === 'scale-down' ? Math.min(1, contain) : contain
  const width = media.width * scale
  const height = media.height * scale
  return {
    left: (box.width - width) * position.x,
    top: (box.height - height) * position.y,
    width,
    height,
  }
}

export function clamp01(value: number) {
  return Math.min(1, Math.max(0, value))
}

/**
 * Client (viewport) coordinates → normalized frame coordinates. `rect` is the on-screen content
 * rect, e.g. the overlay's getBoundingClientRect(), which already includes page zoom, CSS
 * transforms and fullscreen scaling.
 */
export function clientToNormalized(clientX: number, clientY: number, rect: Rect, clamp = true): Point {
  const x = rect.width ? (clientX - rect.left) / rect.width : 0
  const y = rect.height ? (clientY - rect.top) / rect.height : 0
  return clamp ? { x: clamp01(x), y: clamp01(y) } : { x, y }
}

/** Normalized frame coordinates → pixels within a content rect of `size`. */
export function normalizedToPixels(point: Point, size: Size): Point {
  return { x: point.x * size.width, y: point.y * size.height }
}

export function distance(a: Point, b: Point) {
  return Math.hypot(a.x - b.x, a.y - b.y)
}

/** Absolute polygon area (shoelace). */
export function polygonArea(points: Point[]) {
  let sum = 0
  for (let index = 0; index < points.length; index++) {
    const a = points[index]!
    const b = points[(index + 1) % points.length]!
    sum += a.x * b.y - b.x * a.y
  }
  return Math.abs(sum) / 2
}

/** Area centroid; falls back to the vertex mean for degenerate polygons. */
export function polygonCentroid(points: Point[]): Point {
  if (!points.length) return { x: 0, y: 0 }
  let area = 0
  let cx = 0
  let cy = 0
  for (let index = 0; index < points.length; index++) {
    const a = points[index]!
    const b = points[(index + 1) % points.length]!
    const cross = a.x * b.y - b.x * a.y
    area += cross
    cx += (a.x + b.x) * cross
    cy += (a.y + b.y) * cross
  }
  if (Math.abs(area) < 1e-12) {
    return {
      x: points.reduce((sum, point) => sum + point.x, 0) / points.length,
      y: points.reduce((sum, point) => sum + point.y, 0) / points.length,
    }
  }
  return { x: cx / (3 * area), y: cy / (3 * area) }
}

function orientation(a: Point, b: Point, c: Point) {
  const value = (b.y - a.y) * (c.x - b.x) - (b.x - a.x) * (c.y - b.y)
  return Math.abs(value) < 1e-12 ? 0 : value > 0 ? 1 : -1
}

function onSegment(a: Point, b: Point, c: Point) {
  return Math.min(a.x, c.x) <= b.x && b.x <= Math.max(a.x, c.x)
    && Math.min(a.y, c.y) <= b.y && b.y <= Math.max(a.y, c.y)
}

export function segmentsIntersect(p1: Point, p2: Point, q1: Point, q2: Point) {
  const o1 = orientation(p1, p2, q1)
  const o2 = orientation(p1, p2, q2)
  const o3 = orientation(q1, q2, p1)
  const o4 = orientation(q1, q2, p2)
  if (o1 !== o2 && o3 !== o4) return true
  return (o1 === 0 && onSegment(p1, q1, p2))
    || (o2 === 0 && onSegment(p1, q2, p2))
    || (o3 === 0 && onSegment(q1, p1, q2))
    || (o4 === 0 && onSegment(q1, p2, q2))
}

/** True when two non-adjacent edges of the closed polygon cross or touch. */
export function isSelfIntersecting(points: Point[]) {
  const count = points.length
  if (count < 4) return false
  for (let i = 0; i < count; i++) {
    const a1 = points[i]!
    const a2 = points[(i + 1) % count]!
    for (let j = i + 1; j < count; j++) {
      // Skip edges that share a vertex with edge i.
      if (j === i + 1 || (i === 0 && j === count - 1)) continue
      if (segmentsIntersect(a1, a2, points[j]!, points[(j + 1) % count]!)) return true
    }
  }
  return false
}
