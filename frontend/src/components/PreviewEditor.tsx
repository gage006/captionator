import { useEffect, useRef, useState } from 'react'
import type { StyleInfo, TranscriptSegment, RenderRequest } from '../types'
import { sourceVideoUrl } from '../api/client'
import { StylePicker } from './StylePicker'
import { captionTextStyle, captionBackdrop } from './captionStyle'

interface Props {
  jobId: string
  styles: StyleInfo[]
  segments: TranscriptSegment[]
  onSave: (req: RenderRequest) => void
}

const DEFAULT_POS = { x: 0.5, y: 0.85 } // centered, 15% up from the bottom
const SNAP_THRESHOLD = 0.03

const clamp = (v: number, min: number, max: number) =>
  Math.min(max, Math.max(min, v))

type DragState = {
  mode: 'move' | 'resize'
  startX: number
  startY: number
  startPos: { x: number; y: number }
  startScale: number
  startDist: number
  centerX: number
  centerY: number
}

export function PreviewEditor({ jobId, styles, segments, onSave }: Props) {
  const [selectedStyle, setSelectedStyle] = useState(styles[0]?.id ?? '')
  const [position, setPosition] = useState(DEFAULT_POS)
  const [scale, setScale] = useState(1.0)
  const [time, setTime] = useState(0)
  const [dragging, setDragging] = useState(false)

  // Intrinsic video size and the on-screen size of the video box, used to size the
  // overlay text in proportion to the eventual burned output.
  const [videoDims, setVideoDims] = useState<{ w: number; h: number } | null>(null)
  const [stageSize, setStageSize] = useState<{ w: number; h: number } | null>(null)

  const stageRef = useRef<HTMLDivElement>(null)
  const dragRef = useRef<DragState | null>(null)

  const selectedInfo = styles.find((s) => s.id === selectedStyle)

  // Default to the first style once the list is available (in case it loaded late).
  useEffect(() => {
    if (!selectedStyle && styles.length > 0) setSelectedStyle(styles[0].id)
  }, [styles, selectedStyle])

  // Track the rendered size of the video box.
  useEffect(() => {
    const el = stageRef.current
    if (!el) return
    const ro = new ResizeObserver((entries) => {
      const r = entries[0].contentRect
      setStageSize({ w: r.width, h: r.height })
    })
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  // Global pointer handlers so a drag keeps tracking outside the caption box.
  useEffect(() => {
    const onMove = (e: PointerEvent) => {
      const drag = dragRef.current
      const stage = stageRef.current
      if (!drag || !stage) return
      const rect = stage.getBoundingClientRect()

      if (drag.mode === 'move') {
        const dx = (e.clientX - drag.startX) / rect.width
        const dy = (e.clientY - drag.startY) / rect.height
        let nx = clamp(drag.startPos.x + dx, 0.02, 0.98)
        const ny = clamp(drag.startPos.y + dy, 0.04, 0.98)
        if (Math.abs(nx - 0.5) < SNAP_THRESHOLD) nx = 0.5 // snap to horizontal center
        setPosition({ x: nx, y: ny })
      } else {
        const dist = Math.hypot(e.clientX - drag.centerX, e.clientY - drag.centerY)
        setScale(clamp((drag.startScale * dist) / drag.startDist, 0.3, 4))
      }
    }
    const onUp = () => {
      if (dragRef.current) {
        dragRef.current = null
        setDragging(false)
      }
    }
    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', onUp)
    return () => {
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', onUp)
    }
  }, [])

  const startMove = (e: React.PointerEvent) => {
    e.preventDefault()
    dragRef.current = {
      mode: 'move',
      startX: e.clientX,
      startY: e.clientY,
      startPos: { ...position },
      startScale: scale,
      startDist: 0,
      centerX: 0,
      centerY: 0,
    }
    setDragging(true)
  }

  const startResize = (e: React.PointerEvent) => {
    e.preventDefault()
    e.stopPropagation()
    const stage = stageRef.current
    if (!stage) return
    const rect = stage.getBoundingClientRect()
    const centerX = rect.left + position.x * rect.width
    const centerY = rect.top + position.y * rect.height
    const startDist = Math.hypot(e.clientX - centerX, e.clientY - centerY) || 1
    dragRef.current = {
      mode: 'resize',
      startX: e.clientX,
      startY: e.clientY,
      startPos: { ...position },
      startScale: scale,
      startDist,
      centerX,
      centerY,
    }
    setDragging(true)
  }

  const activeSegment = segments.find((s) => time >= s.start && time < s.end)
  const displayText =
    activeSegment?.text || segments[0]?.text || 'Caption preview'

  const renderedH = stageSize?.h ?? 0
  const intrinsicH = videoDims?.h ?? 1080
  const fontPx = renderedH > 0 ? (selectedInfo?.base_font_size ?? 48) * (renderedH / intrinsicH) * scale : 0

  const atCenter = position.x === 0.5

  return (
    <div className="preview-editor">
      <div className="preview-stage" ref={stageRef}>
        <video
          className="preview-video"
          src={sourceVideoUrl(jobId)}
          controls
          playsInline
          onLoadedMetadata={(e) =>
            setVideoDims({
              w: e.currentTarget.videoWidth,
              h: e.currentTarget.videoHeight,
            })
          }
          onTimeUpdate={(e) => setTime(e.currentTarget.currentTime)}
        />
        <div className="caption-overlay">
          {dragging && atCenter && <div className="center-guide" />}
          {selectedInfo && fontPx > 0 && (
            <div
              className={`caption-block${dragging ? ' dragging' : ''}`}
              style={{
                left: `${position.x * 100}%`,
                top: `${position.y * 100}%`,
                fontSize: `${fontPx}px`,
              }}
              onPointerDown={startMove}
              role="button"
              tabIndex={0}
              aria-label="Caption position — drag to move"
            >
              <span className="caption-text" style={captionTextStyle(selectedInfo)}>
                <span style={captionBackdrop(selectedInfo.id)}>{displayText}</span>
              </span>
              <span
                className="resize-handle"
                onPointerDown={startResize}
                aria-label="Drag to resize captions"
              />
            </div>
          )}
        </div>
      </div>

      <p className="preview-hint">
        Drag the caption to position it · drag the corner dot to resize
      </p>

      <div className="size-row">
        <span className="size-label">Size</span>
        <input
          type="range"
          min={0.3}
          max={4}
          step={0.05}
          value={scale}
          onChange={(e) => setScale(Number(e.target.value))}
        />
        <span className="size-val">{Math.round(scale * 100)}%</span>
      </div>

      <StylePicker
        styles={styles}
        selected={selectedStyle}
        onSelect={setSelectedStyle}
      />

      <button
        className="btn-primary"
        disabled={!selectedStyle}
        onClick={() =>
          onSave({
            style: selectedStyle,
            position_x: position.x,
            position_y: position.y,
            scale,
          })
        }
      >
        Render with this style
      </button>
    </div>
  )
}
