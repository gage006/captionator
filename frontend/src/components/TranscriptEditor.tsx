import { useEffect, useState } from 'react'
import type { TranscriptSegment } from '../types'
import { updateTranscript } from '../api/client'

interface Props {
  jobId: string
  segments: TranscriptSegment[]
  onSegmentsChange: (segments: TranscriptSegment[]) => void
}

function formatTime(seconds: number): string {
  const m = Math.floor(seconds / 60)
  const s = Math.floor(seconds % 60)
  return `${m}:${s.toString().padStart(2, '0')}`
}

export function TranscriptEditor({ jobId, segments, onSegmentsChange }: Props) {
  const [texts, setTexts] = useState<string[]>(() => segments.map((s) => s.text))
  const [saveState, setSaveState] = useState<'idle' | 'saving' | 'saved' | 'error'>('idle')
  const [errorMessage, setErrorMessage] = useState('')

  // A new job means a fresh transcript — resync the editable copy. Keyed on
  // jobId only (not segments) so saving our own edits doesn't get clobbered
  // by the very segments update it just triggered.
  useEffect(() => {
    setTexts(segments.map((s) => s.text))
    setSaveState('idle')
  }, [jobId]) // eslint-disable-line react-hooks/exhaustive-deps

  const dirty = texts.some((t, i) => t !== segments[i]?.text)

  const handleChange = (index: number, value: string) => {
    setTexts((prev) => prev.map((t, i) => (i === index ? value : t)))
    setSaveState('idle')
  }

  const handleSave = async () => {
    setSaveState('saving')
    setErrorMessage('')
    try {
      const res = await updateTranscript(jobId, texts)
      onSegmentsChange(res.segments)
      setTexts(res.segments.map((s) => s.text))
      setSaveState('saved')
    } catch (err) {
      setErrorMessage(err instanceof Error ? err.message : 'Could not save transcript.')
      setSaveState('error')
    }
  }

  if (segments.length === 0) {
    return <p className="transcript-empty">No transcript segments to edit.</p>
  }

  return (
    <div className="transcript-editor">
      {segments.map((seg, i) => (
        <div className="transcript-row" key={i}>
          <span className="transcript-time">
            {formatTime(seg.start)}–{formatTime(seg.end)}
          </span>
          <textarea
            className="transcript-textarea"
            value={texts[i] ?? ''}
            onChange={(e) => handleChange(i, e.target.value)}
            rows={2}
          />
        </div>
      ))}

      <div className="transcript-save-row">
        <button
          type="button"
          className="btn-secondary"
          onClick={handleSave}
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
