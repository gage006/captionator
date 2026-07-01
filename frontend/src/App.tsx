import { useState, useEffect, useRef } from 'react'
import { fetchStyles, uploadVideo, getTranscript, getJobStatus, renderJob } from './api/client'
import { useJobPolling } from './hooks/useJobPolling'
import { UploadZone } from './components/UploadZone'
import { PreviewEditor } from './components/PreviewEditor'
import { ProgressTracker } from './components/ProgressTracker'
import { DownloadPanel } from './components/DownloadPanel'
import type {
  StyleInfo,
  TranscriptSegment,
  RenderRequest,
  JobStatusValue,
} from './types'
import './App.css'

type AppState =
  | 'idle'
  | 'uploading'
  | 'transcribing'
  | 'editing'
  | 'rendering'
  | 'complete'
  | 'error'

export default function App() {
  const [styles, setStyles] = useState<StyleInfo[]>([])
  const [appState, setAppState] = useState<AppState>('idle')
  const [uploadProgress, setUploadProgress] = useState(0)
  const [jobId, setJobId] = useState<string | null>(null)
  const [segments, setSegments] = useState<TranscriptSegment[]>([])
  const [errorMessage, setErrorMessage] = useState('')
  const [silenceRemovedSeconds, setSilenceRemovedSeconds] = useState<number | null>(null)
  const loadingTranscriptRef = useRef(false)

  // The flow polls in two phases: while transcribing (stop at `ready`) and while
  // rendering (stop at `complete`). Outside those phases we don't poll.
  const waiting = appState === 'transcribing' || appState === 'rendering'
  const terminal: JobStatusValue[] =
    appState === 'transcribing'
      ? ['ready', 'failed', 'expired']
      : ['complete', 'failed', 'expired']
  const jobStatus = useJobPolling(waiting ? jobId : null, terminal)

  useEffect(() => {
    fetchStyles().then(setStyles).catch(console.error)
  }, [])

  // React to job status updates for whichever phase we're in.
  useEffect(() => {
    if (!jobStatus || !jobId) return

    if (appState === 'transcribing') {
      if (jobStatus.status === 'ready' && !loadingTranscriptRef.current) {
        loadingTranscriptRef.current = true
        getTranscript(jobId)
          .then((t) => {
            setSegments(t.segments)
            setAppState('editing')
          })
          .catch((err) => {
            setErrorMessage(
              err instanceof Error ? err.message : 'Could not load transcript.',
            )
            setAppState('error')
          })
      } else if (jobStatus.status === 'failed' || jobStatus.status === 'expired') {
        setErrorMessage(jobStatus.error ?? 'Transcription failed.')
        setAppState('error')
      }
    } else if (appState === 'rendering') {
      if (jobStatus.status === 'complete') {
        // Capture before the polling hook resets its status to null once
        // `waiting` flips false on the next render.
        setSilenceRemovedSeconds(jobStatus.silence_removed_seconds)
        setAppState('complete')
      } else if (jobStatus.status === 'failed' || jobStatus.status === 'expired') {
        setErrorMessage(jobStatus.error ?? 'Rendering failed.')
        setAppState('error')
      }
    }
  }, [jobStatus, appState, jobId])

  const handleUpload = async (file: File, removeSilences: boolean) => {
    setAppState('uploading')
    setUploadProgress(0)
    try {
      const res = await uploadVideo(file, 'auto', removeSilences, setUploadProgress)
      setJobId(res.job_id)
      setAppState('transcribing')
      // A new job is a new "page" worth returning to — push so Back leaves it
      // on the history stack, distinct from whatever was there before.
      window.history.pushState({}, '', `?job=${res.job_id}`)
    } catch (err) {
      setErrorMessage(err instanceof Error ? err.message : 'Upload failed.')
      setAppState('error')
    }
  }

  const handleSave = async (req: RenderRequest) => {
    if (!jobId) return
    try {
      await renderJob(jobId, req)
      setAppState('rendering')
    } catch (err) {
      setErrorMessage(err instanceof Error ? err.message : 'Could not start render.')
      setAppState('error')
    }
  }

  // Pure state reset, no history side effect — this is what a popstate-driven
  // "back to idle" uses, since the URL has already changed by the time we hear
  // about it and pushing/replacing again would corrupt the back/forward stack.
  const resetState = () => {
    setAppState('idle')
    setJobId(null)
    setUploadProgress(0)
    setSegments([])
    setErrorMessage('')
    setSilenceRemovedSeconds(null)
    loadingTranscriptRef.current = false
  }

  // User-initiated reset ("Process another video" / "Try again"): also pushes
  // a fresh idle entry, so pressing Back from idle returns to this job's URL
  // and rehydrate() restores it.
  const reset = () => {
    resetState()
    window.history.pushState({}, '', window.location.pathname)
  }

  // Re-derive UI state from the job's current server-side status. Used both on
  // first mount (the URL already has a ?job=... from a previous visit) and on
  // browser back/forward — the URL, not React state, is the source of truth
  // for "which job am I looking at," so this is the one place that turns a
  // job id back into the right screen.
  const rehydrate = async (id: string) => {
    setJobId(id)
    try {
      const status = await getJobStatus(id)
      if (status.status === 'transcribing') {
        setAppState('transcribing')
      } else if (status.status === 'ready') {
        const t = await getTranscript(id)
        setSegments(t.segments)
        setAppState('editing')
      } else if (status.status === 'rendering') {
        setAppState('rendering')
      } else if (status.status === 'complete') {
        setSilenceRemovedSeconds(status.silence_removed_seconds)
        setAppState('complete')
      } else {
        // failed / expired
        setErrorMessage(status.error ?? 'This job is no longer available.')
        setAppState('error')
      }
    } catch {
      // Most likely a 404 — the job was cleaned up after the retention window.
      // Surface it rather than silently bouncing to idle, but drop the dead
      // job id from the URL so a refresh doesn't repeat the failed lookup.
      setErrorMessage('This video session could not be found — it may have expired.')
      setAppState('error')
      window.history.replaceState({}, '', window.location.pathname)
    }
  }

  // On mount, pick up a job id already in the URL (deep link / refresh).
  // On back/forward, re-sync from whatever job id the URL now has — or back
  // to idle if there isn't one. Intentionally empty deps: rehydrate/resetState
  // only call stable setters, so the closure captured at mount never goes stale.
  useEffect(() => {
    const initialId = new URLSearchParams(window.location.search).get('job')
    if (initialId) rehydrate(initialId)

    const onPopState = () => {
      const id = new URLSearchParams(window.location.search).get('job')
      if (id) {
        rehydrate(id)
      } else {
        resetState()
      }
    }
    window.addEventListener('popstate', onPopState)
    return () => window.removeEventListener('popstate', onPopState)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return (
    <div className="app">
      <header className="app-header">
        <h1>Captionator</h1>
        <p>Upload a video — preview, place, and burn in captions.</p>
      </header>

      {appState === 'idle' && <UploadZone onFile={handleUpload} />}

      {appState === 'uploading' && (
        <div className="upload-progress-panel">
          <p>Uploading video…</p>
          <div className="progress-bar-row">
            <progress value={uploadProgress} max={100} />
            <span className="progress-pct">{uploadProgress}%</span>
          </div>
        </div>
      )}

      {appState === 'transcribing' && (
        <div className="polling-panel">
          {jobStatus ? (
            <ProgressTracker status={jobStatus} />
          ) : (
            <div className="loading-text">Transcribing…</div>
          )}
        </div>
      )}

      {appState === 'editing' && jobId && (
        <PreviewEditor
          jobId={jobId}
          styles={styles}
          segments={segments}
          onSegmentsChange={setSegments}
          onSave={handleSave}
        />
      )}

      {appState === 'rendering' && (
        <div className="polling-panel">
          {jobStatus ? (
            <ProgressTracker status={jobStatus} />
          ) : (
            <div className="loading-text">Rendering…</div>
          )}
        </div>
      )}

      {appState === 'complete' && jobId && (
        <>
          <DownloadPanel jobId={jobId} silenceRemovedSeconds={silenceRemovedSeconds} />
          <button className="btn-ghost" onClick={reset}>
            Process another video
          </button>
        </>
      )}

      {appState === 'error' && (
        <div className="error-panel">
          <div className="error-icon">⚠</div>
          <p>{errorMessage}</p>
          <button className="btn-secondary" onClick={reset}>
            Try again
          </button>
        </div>
      )}
    </div>
  )
}
