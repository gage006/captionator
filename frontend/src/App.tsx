import { useState, useEffect, useRef } from 'react'
import { fetchStyles, uploadVideo, getTranscript, renderJob } from './api/client'
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
        setAppState('complete')
      } else if (jobStatus.status === 'failed' || jobStatus.status === 'expired') {
        setErrorMessage(jobStatus.error ?? 'Rendering failed.')
        setAppState('error')
      }
    }
  }, [jobStatus, appState, jobId])

  const handleUpload = async (file: File) => {
    setAppState('uploading')
    setUploadProgress(0)
    try {
      const res = await uploadVideo(file, 'auto', setUploadProgress)
      setJobId(res.job_id)
      setAppState('transcribing')
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

  const reset = () => {
    setAppState('idle')
    setJobId(null)
    setUploadProgress(0)
    setSegments([])
    setErrorMessage('')
    loadingTranscriptRef.current = false
  }

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
          <DownloadPanel jobId={jobId} />
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
