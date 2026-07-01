export interface StyleInfo {
  id: string
  label: string
  description: string
  preview_color: string
  /** Base ASS font size in video-height units; used to size the preview overlay. */
  base_font_size: number
}

export type JobStatusValue =
  | 'transcribing'
  | 'ready'
  | 'rendering'
  | 'complete'
  | 'failed'
  | 'expired'
export type JobStep =
  | 'uploading'
  | 'transcribing'
  | 'removing_silences'
  | 'preview_ready'
  | 'styling'
  | 'burning'
  | 'done'

export interface CaptionPlacement {
  position_x: number
  position_y: number
  scale: number
}

export interface JobStatus extends CaptionPlacement {
  job_id: string
  status: JobStatusValue
  step: JobStep
  progress: number
  style: string | null
  remove_silences: boolean
  silence_removed_seconds: number | null
  error: string | null
  created_at: string | null
  completed_at: string | null
}

export interface UploadResponse {
  job_id: string
  status: string
  message: string
}

export interface TranscriptWord {
  word: string
  start: number
  end: number
}

export interface TranscriptSegment {
  start: number
  end: number
  text: string
  words: TranscriptWord[]
}

export interface TranscriptResponse {
  segments: TranscriptSegment[]
}

export interface RenderRequest extends CaptionPlacement {
  style: string
}
