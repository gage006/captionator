import { useRef, useState } from 'react'

interface Props {
  onFile: (file: File, removeSilences: boolean) => void
}

export function UploadZone({ onFile }: Props) {
  const inputRef = useRef<HTMLInputElement>(null)
  const [removeSilences, setRemoveSilences] = useState(false)

  const handleChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (file) onFile(file, removeSilences)
    // Reset so the same file can be re-selected after an error
    e.target.value = ''
  }

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault()
    const file = e.dataTransfer.files[0]
    if (file) onFile(file, removeSilences)
  }

  return (
    <div className="upload-zone-wrapper">
      <div
        className="upload-zone"
        onDrop={handleDrop}
        onDragOver={(e) => e.preventDefault()}
        onClick={() => inputRef.current?.click()}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => e.key === 'Enter' && inputRef.current?.click()}
      >
        {/* No `capture` attr — lets iOS show Files + Photos picker */}
        <input
          ref={inputRef}
          type="file"
          accept="video/*"
          style={{ display: 'none' }}
          onChange={handleChange}
        />
        <div className="upload-icon">🎬</div>
        <p className="upload-label">Tap to select a video</p>
        <p className="upload-hint">MP4, MOV, or any video format</p>
      </div>
      <label className="upload-option">
        <input
          type="checkbox"
          checked={removeSilences}
          onChange={(e) => setRemoveSilences(e.target.checked)}
        />
        Remove silences
      </label>
    </div>
  )
}
