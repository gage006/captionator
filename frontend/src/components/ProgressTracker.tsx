import type { JobStatus, JobStep } from '../types'

const STEPS: JobStep[] = ['uploading', 'transcribing', 'removing_silences', 'styling', 'burning']
const STEP_LABELS: Record<JobStep, string> = {
  uploading: 'Uploading',
  transcribing: 'Transcribing audio',
  removing_silences: 'Removing silences',
  preview_ready: 'Ready to edit',
  styling: 'Generating captions',
  burning: 'Burning into video',
  done: 'Complete',
}

interface Props {
  status: JobStatus
}

export function ProgressTracker({ status }: Props) {
  const currentIdx =
    status.step === 'done'
      ? STEPS.length
      : STEPS.indexOf(status.step as JobStep)

  return (
    <div className="progress-tracker">
      <div className="progress-bar-row">
        <progress value={status.progress} max={100} />
        <span className="progress-pct">{status.progress}%</span>
      </div>
      {status.silence_removed_seconds != null && status.silence_removed_seconds > 0 && (
        <p className="silence-removed-note">
          Removed {status.silence_removed_seconds.toFixed(1)}s of silence
        </p>
      )}
      <div className="steps">
        {STEPS.map((step, i) => {
          const state =
            i < currentIdx ? 'done' : i === currentIdx ? 'active' : 'pending'
          return (
            <div key={step} className={`step step-${state}`}>
              <div className="step-dot" />
              <span>{STEP_LABELS[step]}</span>
            </div>
          )
        })}
      </div>
    </div>
  )
}
