interface Props {
  jobId: string
}

export function DownloadPanel({ jobId }: Props) {
  const base = `/api/download/${jobId}`
  return (
    <div className="download-panel">
      <div className="success-icon">✓</div>
      <h2>Your video is ready!</h2>
      <div className="download-buttons">
        <a
          href={`${base}/video`}
          download="captionated.mp4"
          className="btn-primary"
        >
          Download Video (MP4)
        </a>
        <a
          href={`${base}/srt`}
          download="transcript.srt"
          className="btn-secondary"
        >
          Download Subtitles (SRT)
        </a>
        <a
          href={`${base}/txt`}
          download="transcript.txt"
          className="btn-secondary"
        >
          Download Transcript (TXT)
        </a>
        <a
          href={`${base}/ass`}
          download="captions.ass"
          className="btn-secondary"
        >
          Download ASS Subtitles
        </a>
      </div>
    </div>
  )
}
