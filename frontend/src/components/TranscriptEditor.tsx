import type { TranscriptSegment } from '../types'
import type { DraftSegment, SaveState } from '../hooks/useTranscriptDraft'

interface Props {
  segments: TranscriptSegment[]
  draft: DraftSegment[]
  onTextChange: (index: number, text: string) => void
  onToggleDeleted: (index: number) => void
  onSave: () => void
  dirty: boolean
  saveState: SaveState
  errorMessage: string
  survivorCount: number
}

function formatTime(seconds: number): string {
  const m = Math.floor(seconds / 60)
  const s = Math.floor(seconds % 60)
  return `${m}:${s.toString().padStart(2, '0')}`
}

export function TranscriptEditor({
  segments,
  draft,
  onTextChange,
  onToggleDeleted,
  onSave,
  dirty,
  saveState,
  errorMessage,
  survivorCount,
}: Props) {
  if (segments.length === 0) {
    return <p className="transcript-empty">No transcript segments to edit.</p>
  }

  return (
    <div className="transcript-editor">
      {segments.map((seg, i) => {
        const row = draft[i] ?? { text: seg.text, deleted: false }
        const lastSurvivor = !row.deleted && survivorCount === 1
        return (
          <div className={`transcript-row${row.deleted ? ' deleted' : ''}`} key={i}>
            <div className="transcript-row-header">
              <span className="transcript-time">
                {formatTime(seg.start)}–{formatTime(seg.end)}
              </span>
              <button
                type="button"
                className="transcript-delete-btn"
                onClick={() => onToggleDeleted(i)}
                disabled={lastSurvivor}
                title={lastSurvivor ? 'At least one caption must remain' : undefined}
                aria-label={row.deleted ? 'Undo delete' : `Delete segment ${i + 1}`}
              >
                {row.deleted ? 'Undo' : 'Delete'}
              </button>
            </div>
            <textarea
              className="transcript-textarea"
              value={row.text}
              onChange={(e) => onTextChange(i, e.target.value)}
              disabled={row.deleted}
              rows={2}
            />
          </div>
        )
      })}

      <div className="transcript-save-row">
        <button
          type="button"
          className="btn-secondary"
          onClick={onSave}
          disabled={!dirty || saveState === 'saving'}
        >
          {saveState === 'saving' ? 'Saving…' : 'Save Transcript'}
        </button>
        {saveState === 'saved' && <span className="transcript-status transcript-status-ok">Saved</span>}
        {saveState === 'error' && (
          <span className="transcript-status transcript-status-error">{errorMessage}</span>
        )}
      </div>
    </div>
  )
}
