import type { CSSProperties } from 'react'
import type { StyleInfo } from '../types'

// Swatch background per style for the StylePicker cards (not used by the video overlay,
// which sits on the real video frame).
export const PREVIEW_BG: Record<string, string> = {
  neon: '#0a0a1a',
  clean_box: '#1a1a1a',
  duo_tone: '#111',
  mixed_weight: '#111',
  keyword_pop: '#111',
}

export const BOLD_STYLES = [
  'tiktok_bold',
  'karaoke',
  'clean_box',
  'duo_tone',
  'mixed_weight',
  'keyword_pop',
]
export const LARGE_STYLES = ['tiktok_bold', 'duo_tone', 'mixed_weight', 'keyword_pop']

/**
 * CSS approximation of a caption style's *text* appearance (color, weight, italic,
 * outline/glow). Shared by the StylePicker swatch and the live preview overlay so the
 * two always agree. Font size is applied by the caller — the swatch uses a fixed size,
 * the overlay scales it to the video.
 */
export function captionTextStyle(style: StyleInfo): CSSProperties {
  const isMixedWeight = style.id === 'mixed_weight'
  return {
    color: isMixedWeight ? 'transparent' : style.preview_color,
    fontWeight: BOLD_STYLES.includes(style.id) ? 700 : 400,
    fontStyle: style.id === 'cinematic' ? 'italic' : 'normal',
    textShadow:
      style.id === 'neon'
        ? `0 0 8px ${style.preview_color}, 0 0 20px ${style.preview_color}`
        : ['tiktok_bold', 'duo_tone'].includes(style.id)
        ? '2px 2px 0 #000, -2px -2px 0 #000, 2px -2px 0 #000, -2px 2px 0 #000'
        : style.id === 'classic' || style.id === 'minimal' || style.id === 'karaoke'
        ? '1px 1px 2px #000, -1px -1px 2px #000'
        : 'none',
    WebkitTextStroke: isMixedWeight ? '2px #ffffff' : undefined,
  }
}

/**
 * Backdrop behind the text block for "box" styles (clean_box renders an opaque box in
 * ASS). Returns undefined for styles that draw text directly on the frame.
 */
export function captionBackdrop(styleId: string): CSSProperties | undefined {
  if (styleId === 'clean_box') {
    return {
      background: 'rgba(0, 0, 0, 0.6)',
      padding: '0.08em 0.35em',
      borderRadius: '4px',
    }
  }
  return undefined
}
