import { useState } from 'react'
import { fetchTranscriptText } from '../api/client'

interface Props {
  jobId: string
  silenceRemovedSeconds?: number | null
}

function buildAiPrompt(transcript: string): string {
  return `You are helping me write an Instagram caption for a video I just posted.

Below is the transcript of the video. Based on it, write:
1. A short, scroll-stopping Instagram caption (1-3 sentences, hook first)
2. 3-5 relevant hashtags
3. One optional call-to-action line (e.g. "Follow for more", "Comment your thoughts") — only if it fits naturally

Tone: match the energy of the video — don't force humor if it's serious content, don't be flat if it's upbeat.
Keep it concise. No generic filler like "Check out this amazing video!"
Do not use quotation marks around the caption itself.
Do not use em dashes.

Transcript:
"""
${transcript.trim()}
"""`
}

export function DownloadPanel({ jobId, silenceRemovedSeconds }: Props) {
  const base = `/api/download/${jobId}`
  const [copyState, setCopyState] = useState<'idle' | 'copying' | 'copied' | 'error'>('idle')

  const handleCopyPrompt = async () => {
    setCopyState('copying')
    try {
      const transcript = await fetchTranscriptText(jobId)
      await navigator.clipboard.writeText(buildAiPrompt(transcript))
      setCopyState('copied')
      setTimeout(() => setCopyState('idle'), 2000)
    } catch {
      setCopyState('error')
      setTimeout(() => setCopyState('idle'), 2000)
    }
  }

  const copyLabel = {
    idle: 'Copy AI Prompt',
    copying: 'Copying…',
    copied: 'Copied!',
    error: 'Copy failed',
  }[copyState]

  return (
    <div className="download-panel">
      <div className="success-icon">✓</div>
      <h2>Your video is ready!</h2>
      {silenceRemovedSeconds != null && silenceRemovedSeconds > 0 && (
        <p className="silence-removed-note">
          Removed {silenceRemovedSeconds.toFixed(1)}s of silence
        </p>
      )}
      <div className="download-buttons">
        <a
          href={`${base}/video`}
          download="captionated.mp4"
          className="btn-primary"
        >
          Download Video
        </a>
        <a
          href={`${base}/txt`}
          download="transcript.txt"
          className="btn-secondary"
        >
          Download Transcript
        </a>
        <button type="button" className="btn-secondary" onClick={handleCopyPrompt}>
          {copyLabel}
        </button>
      </div>
    </div>
  )
}
