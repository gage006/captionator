import { useState, useEffect, useRef } from 'react'
import { getJobStatus } from '../api/client'
import type { JobStatus, JobStatusValue } from '../types'

const DEFAULT_TERMINAL: JobStatusValue[] = ['complete', 'failed', 'expired']

/**
 * Polls a job's status every 2s until it reaches a terminal state, then stops.
 *
 * `terminalStatuses` is configurable so the same hook drives both phases of the
 * flow: the transcribe phase stops at `ready`, the render phase stops at
 * `complete`. Passing a different jobId or terminal set restarts polling, which
 * is how the render phase resumes after the user saves.
 */
export function useJobPolling(
  jobId: string | null,
  terminalStatuses: JobStatusValue[] = DEFAULT_TERMINAL,
): JobStatus | null {
  const [status, setStatus] = useState<JobStatus | null>(null)
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const terminalKey = terminalStatuses.join(',')

  useEffect(() => {
    if (!jobId) {
      setStatus(null)
      return
    }

    const terminals = terminalKey.split(',') as JobStatusValue[]
    const poll = async () => {
      try {
        const data = await getJobStatus(jobId)
        setStatus(data)
        if (terminals.includes(data.status)) {
          if (intervalRef.current) clearInterval(intervalRef.current)
        }
      } catch (err) {
        console.error('Polling error:', err)
      }
    }

    poll()
    intervalRef.current = setInterval(poll, 2000)

    return () => {
      if (intervalRef.current) clearInterval(intervalRef.current)
    }
  }, [jobId, terminalKey])

  return status
}
