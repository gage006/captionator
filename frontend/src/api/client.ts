import axios from 'axios'
import type {
  StyleInfo,
  JobStatus,
  UploadResponse,
  TranscriptResponse,
  RenderRequest,
} from '../types'

const api = axios.create({ baseURL: '/api' })

export async function fetchStyles(): Promise<StyleInfo[]> {
  const res = await api.get<StyleInfo[]>('/styles')
  return res.data
}

export async function getJobStatus(jobId: string): Promise<JobStatus> {
  const res = await api.get<JobStatus>(`/jobs/${jobId}`)
  return res.data
}

export async function getTranscript(jobId: string): Promise<TranscriptResponse> {
  const res = await api.get<TranscriptResponse>(`/jobs/${jobId}/transcript`)
  return res.data
}

export async function updateTranscript(
  jobId: string,
  texts: string[],
): Promise<TranscriptResponse> {
  const res = await api.put<TranscriptResponse>(`/jobs/${jobId}/transcript`, {
    segments: texts.map((text) => ({ text })),
  })
  return res.data
}

export async function renderJob(
  jobId: string,
  req: RenderRequest,
): Promise<JobStatus> {
  const res = await api.post<JobStatus>(`/jobs/${jobId}/render`, req)
  return res.data
}

/** URL the <video> element loads for the preview scrubber. */
export function sourceVideoUrl(jobId: string): string {
  return `/api/preview/${jobId}/source`
}

export async function fetchTranscriptText(jobId: string): Promise<string> {
  const res = await api.get<string>(`/download/${jobId}/txt`, {
    responseType: 'text',
  })
  return res.data
}

export function uploadVideo(
  file: File,
  language: string,
  removeSilences: boolean,
  onProgress: (pct: number) => void,
): Promise<UploadResponse> {
  return new Promise((resolve, reject) => {
    const formData = new FormData()
    formData.append('file', file)
    formData.append('language', language)
    formData.append('remove_silences', String(removeSilences))

    const xhr = new XMLHttpRequest()
    xhr.open('POST', '/api/upload')

    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) {
        onProgress(Math.round((e.loaded / e.total) * 100))
      }
    }

    xhr.onload = () => {
      if (xhr.status === 202) {
        resolve(JSON.parse(xhr.responseText) as UploadResponse)
      } else {
        let detail = xhr.responseText
        try {
          detail = JSON.parse(xhr.responseText).detail ?? detail
        } catch {}
        reject(new Error(`Upload failed (${xhr.status}): ${detail}`))
      }
    }

    xhr.onerror = () => reject(new Error('Network error during upload'))
    xhr.send(formData)
  })
}
