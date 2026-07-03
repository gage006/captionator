import { useEffect, useState } from 'react'
import type { TranscriptSegment } from '../types'
import { updateTranscript } from '../api/client'

/** One editable row of the transcript, positional against `segments`. */
export interface DraftSegment {
  text: string
  deleted: boolean
}

export type SaveState = 'idle' | 'saving' | 'saved' | 'error'

/**
 * The single editable transcript draft shared by every edit surface (the
 * panel list and the on-video overlay editor). Owns dirtiness and the save
 * round-trip; `segments` stays the server truth owned by App.
 */
export function useTranscriptDraft(
  jobId: string,
  segments: TranscriptSegment[],
  onSegmentsChange: (segments: TranscriptSegment[]) => void,
) {
  const [draft, setDraft] = useState<DraftSegment[]>(() =>
    segments.map((s) => ({ text: s.text, deleted: false })),
  )
  const [saveState, setSaveState] = useState<SaveState>('idle')
  const [errorMessage, setErrorMessage] = useState('')

  // A new job means a fresh transcript — resync the editable copy. Keyed on
  // jobId only (not segments) so saving our own edits doesn't get clobbered
  // by the very segments update it just triggered.
  useEffect(() => {
    setDraft(segments.map((s) => ({ text: s.text, deleted: false })))
    setSaveState('idle')
  }, [jobId]) // eslint-disable-line react-hooks/exhaustive-deps

  // Locally the draft always has one row per segment (deletion only flags a
  // row; save rebuilds both sides together), so a length mismatch can only
  // mean a new baseline arrived outside the save path (e.g. the transcript
  // fetch resolving after mount) — never in-flight edits worth preserving.
  if (draft.length !== segments.length) {
    setDraft(segments.map((s) => ({ text: s.text, deleted: false })))
  }

  const setText = (index: number, text: string) => {
    setDraft((prev) => prev.map((d, i) => (i === index ? { ...d, text } : d)))
    setSaveState('idle')
  }

  const toggleDeleted = (index: number) => {
    setDraft((prev) =>
      prev.map((d, i) => (i === index ? { ...d, deleted: !d.deleted } : d)),
    )
    setSaveState('idle')
  }

  const dirty = draft.some(
    (d, i) => d.deleted || d.text !== segments[i]?.text,
  )
  // Rendering while an edit is unsaved or mid-save can race the
  // transcript.json write that render_video reads from.
  const busy = dirty || saveState === 'saving'
  const survivorCount = draft.filter((d) => !d.deleted).length

  const save = async () => {
    setSaveState('saving')
    setErrorMessage('')
    try {
      const res = await updateTranscript(
        jobId,
        draft.map((d) => ({ text: d.text, delete: d.deleted || undefined })),
      )
      // Adopt the response as the new baseline — after a deletion the
      // surviving list is shorter than the one we sent.
      onSegmentsChange(res.segments)
      setDraft(res.segments.map((s) => ({ text: s.text, deleted: false })))
      setSaveState('saved')
    } catch (err) {
      setErrorMessage(
        err instanceof Error ? err.message : 'Could not save transcript.',
      )
      setSaveState('error')
    }
  }

  return {
    draft,
    setText,
    toggleDeleted,
    save,
    dirty,
    busy,
    saveState,
    errorMessage,
    survivorCount,
  }
}
