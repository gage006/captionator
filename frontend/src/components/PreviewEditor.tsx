import { useEffect, useRef, useState } from 'react'
import type { StyleInfo, TranscriptSegment, RenderRequest } from '../types'
import { sourceVideoUrl } from '../api/client'
import { StylePicker } from './StylePicker'
import { TranscriptEditor } from './TranscriptEditor'
import { CollapsibleSection } from './CollapsibleSection'
import { captionTextStyle, captionBackdrop } from './captionStyle'
import { useTranscriptDraft } from '../hooks/useTranscriptDraft'

interface Props {
  jobId: string
  styles: StyleInfo[]
  segments: TranscriptSegment[]
  onSegmentsChange: (segments: TranscriptSegment[]) => void
  onSave: (req: RenderRequest) => void
}

const DEFAULT_POS = { x: 0.5, y: 0.85 } // centered, 15% up from the bottom
const SNAP_THRESHOLD = 0.03
// Pointer travel below this is a click (opens inline editing), at or above it
// a drag. The same dead zone suppresses position updates, so a click never
// nudges the caption placement.
const CLICK_THRESHOLD_PX = 5

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
  /** True once the pointer has left the click dead zone. */
  moved: boolean
}

export function PreviewEditor({
  jobId,
  styles,
  segments,
  onSegmentsChange,
  onSave,
}: Props) {
  const [selectedStyle, setSelectedStyle] = useState(styles[0]?.id ?? '')
  const [position, setPosition] = useState(DEFAULT_POS)
  const [scale, setScale] = useState(1.0)
  const [time, setTime] = useState(0)
  const [dragging, setDragging] = useState(false)
  const [seekedToSample, setSeekedToSample] = useState(false)
  const [styleOpen, setStyleOpen] = useState(true)
  const [transcriptOpen, setTranscriptOpen] = useState(true)

  // One shared draft for both edit surfaces (panel list + overlay editor).
  const transcript = useTranscriptDraft(jobId, segments, onSegmentsChange)

  // Intrinsic video size and the on-screen size of the video box, used to size the
  // overlay text in proportion to the eventual burned output.
  const [videoDims, setVideoDims] = useState<{ w: number; h: number } | null>(null)
  const [stageSize, setStageSize] = useState<{ w: number; h: number } | null>(null)

  const stageRef = useRef<HTMLDivElement>(null)
  const dragRef = useRef<DragState | null>(null)
  const videoRef = useRef<HTMLVideoElement>(null)

  // Inline overlay editing: pinned to the segment index it opened on, so
  // playback advancing can't yank the textarea onto another segment.
  const [editingIndex, setEditingIndex] = useState<number | null>(null)
  const [editValue, setEditValue] = useState('')
  const cancelEditRef = useRef(false)

  const selectedInfo = styles.find((s) => s.id === selectedStyle)

  // Default to the first style once the list is available (in case it loaded late).
  useEffect(() => {
    if (!selectedStyle && styles.length > 0) setSelectedStyle(styles[0].id)
  }, [styles, selectedStyle])

  // A new job means a new <video> source — re-arm the "hide until seeked" gate.
  useEffect(() => {
    setSeekedToSample(false)
  }, [jobId])

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
        // Dead zone: don't reposition until this is clearly a drag, so a
        // click (which opens inline editing on pointerup) leaves the
        // placement untouched.
        if (
          !drag.moved &&
          Math.hypot(e.clientX - drag.startX, e.clientY - drag.startY) <
            CLICK_THRESHOLD_PX
        ) {
          return
        }
        drag.moved = true
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
      const drag = dragRef.current
      if (drag) {
        dragRef.current = null
        setDragging(false)
        // A press-and-release inside the dead zone is a click: open inline
        // editing for whichever caption is on screen. (Ref indirection
        // because this listener is registered once with empty deps.)
        if (drag.mode === 'move' && !drag.moved) {
          openEditorRef.current()
        }
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
    if (editingIndex !== null) return // typing in the inline editor, not dragging
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
      moved: false,
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
      moved: false,
    }
    setDragging(true)
  }

  // Preview from the draft (so unsaved edits show) and skip draft-deleted
  // segments — a deleted segment's window shows the same fallback as the
  // gaps between segments.
  const activeIndex = segments.findIndex(
    (s, i) => !transcript.draft[i]?.deleted && time >= s.start && time < s.end,
  )
  const firstSurvivorIndex = transcript.draft.findIndex((d) => !d.deleted)
  // Whichever segment's text the overlay is showing right now — the one a
  // click should edit. null when only the bare "Caption preview" fallback is up.
  const displayIndex =
    activeIndex >= 0 ? activeIndex : firstSurvivorIndex >= 0 ? firstSurvivorIndex : null
  const displayText =
    (displayIndex !== null ? transcript.draft[displayIndex]?.text : undefined) ||
    'Caption preview'

  // The click that opens the editor is detected in a window pointerup
  // listener registered once with empty deps, so it reaches the current
  // display index and draft through a ref.
  const openEditorRef = useRef(() => {})
  openEditorRef.current = () => {
    if (displayIndex === null) return
    videoRef.current?.pause()
    cancelEditRef.current = false
    setEditValue(transcript.draft[displayIndex]?.text ?? '')
    setEditingIndex(displayIndex)
  }

  const commitEdit = () => {
    if (cancelEditRef.current) {
      cancelEditRef.current = false
    } else if (editingIndex !== null) {
      transcript.setText(editingIndex, editValue.trim())
    }
    setEditingIndex(null)
  }

  const renderedH = stageSize?.h ?? 0
  const intrinsicH = videoDims?.h ?? 1080
  const fontPx = renderedH > 0 ? (selectedInfo?.base_font_size ?? 48) * (renderedH / intrinsicH) * scale : 0

  const atCenter = position.x === 0.5

  return (
    <div className="preview-editor">
      <div className="preview-stage" ref={stageRef}>
        <video
          ref={videoRef}
          className="preview-video"
          style={{ opacity: seekedToSample ? 1 : 0 }}
          src={sourceVideoUrl(jobId)}
          controls
          playsInline
          preload="auto"
          onLoadedMetadata={(e) => {
            const video = e.currentTarget
            setVideoDims({ w: video.videoWidth, h: video.videoHeight })
            // The first frame is often black (intros/fades), making caption
            // alignment guesswork. Seek to a representative frame — the middle of
            // the first caption, i.e. where real content is on screen — so the
            // user aligns against what they'll actually see. Falls back to 1s.
            const sample =
              segments.length > 0
                ? (segments[0].start + segments[0].end) / 2
                : 1
            const target = Math.min(sample, (video.duration || sample) - 0.05)
            if (Number.isFinite(target) && target > 0) {
              // Stay hidden until `seeked` fires — setting currentTime only
              // starts an async seek, and the element keeps painting frame 0
              // (often black) until the new frame actually decodes.
              video.currentTime = target
            } else {
              // No seek will happen, so there's nothing to wait for.
              setSeekedToSample(true)
            }
          }}
          onSeeked={() => setSeekedToSample(true)}
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
              aria-label="Caption position — drag to move, click to edit"
            >
              {editingIndex !== null ? (
                <textarea
                  className="caption-edit-textarea"
                  value={editValue}
                  onChange={(e) => setEditValue(e.target.value)}
                  onPointerDown={(e) => e.stopPropagation()}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' && !e.shiftKey) {
                      e.preventDefault()
                      commitEdit()
                    } else if (e.key === 'Escape') {
                      cancelEditRef.current = true
                      setEditingIndex(null)
                    }
                  }}
                  onBlur={commitEdit}
                  autoFocus
                  rows={2}
                  aria-label="Edit caption text"
                />
              ) : (
                <span className="caption-text" style={captionTextStyle(selectedInfo)}>
                  <span style={captionBackdrop(selectedInfo.id)}>{displayText}</span>
                </span>
              )}
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
        Drag the caption to position it · click it to edit the text · drag the
        corner dot to resize
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

      <CollapsibleSection
        title="Caption Style"
        open={styleOpen}
        onToggle={() => setStyleOpen((v) => !v)}
      >
        <StylePicker
          styles={styles}
          selected={selectedStyle}
          onSelect={setSelectedStyle}
        />
      </CollapsibleSection>

      <CollapsibleSection
        title="Transcript"
        open={transcriptOpen}
        onToggle={() => setTranscriptOpen((v) => !v)}
      >
        <TranscriptEditor
          segments={segments}
          draft={transcript.draft}
          onTextChange={transcript.setText}
          onToggleDeleted={transcript.toggleDeleted}
          onSave={transcript.save}
          dirty={transcript.dirty}
          saveState={transcript.saveState}
          errorMessage={transcript.errorMessage}
          survivorCount={transcript.survivorCount}
        />
      </CollapsibleSection>

      <button
        className="btn-primary"
        disabled={!selectedStyle || transcript.busy}
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
      {transcript.busy && (
        <p className="preview-hint">Save your transcript edits before rendering.</p>
      )}
    </div>
  )
}
