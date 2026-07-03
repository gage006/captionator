# Transcript Segment Deletion + Overlay Click-to-Edit

## Context

Chosen as the next feature: extend transcript editing. Users can already fix Whisper mis-transcriptions in a side-panel `TranscriptEditor` (shipped in v1.5: `PUT /api/jobs/{id}/transcript`, positional text edits, even word re-timing). What's missing, and what this feature adds:

1. **Segment deletion** — drop filler/false-positive captions. The current API contract rejects any change in segment count (`400` on count mismatch), so deletion needs a contract extension.
2. **Click-to-edit on the video overlay** — click the caption block in the preview to edit the currently shown segment inline. Today the block only drags (`onPointerDown={startMove}` consumes the gesture).
3. Both surfaces (panel + overlay) must edit **one shared draft**, saved through the same PUT endpoint.

User decisions: text edits + deletion (no split/merge/re-timing in v1); both UI surfaces; server-side persistence (render pipeline stays untouched — it reads `transcript.json`).

## Design decisions

- **Deletion contract:** add `delete: bool = False` to `TranscriptEditSegment` (`backend/app/schemas.py`). Client always echoes the full last-received list with flags; the strict `len(req.segments) == len(stored)` check **stays** — it's the staleness guard. After a delete-save the stored count shrinks, so a stale resubmission gets a clean 400 instead of deleting the wrong segment by position. Fully backward compatible (old payloads behave identically).
- **Delete all → 400** `"Cannot delete every segment — at least one caption must remain."` (empty transcript would break downloads/Copy AI Prompt and produce a captionless burn). Client disables the Delete button on the last survivor.
- **Shared draft:** new `frontend/src/hooks/useTranscriptDraft.ts` hook owned by `PreviewEditor`; draft = `{text, deleted}[]` positional against `segments`. `TranscriptEditor` becomes a controlled component (drop its local state + `onBusyChange` plumbing; PreviewEditor reads `busy` from the hook). `App.tsx` keeps owning server-truth `segments`. `save()` PUTs the draft, then adopts the response as the new baseline (handles the shrunken count).
- **Click vs drag:** 5px threshold. `onUp` (PreviewEditor.tsx:103, currently ignores the event) receives the `PointerEvent`; if `drag.mode === 'move'` and `hypot(dx, dy) < 5` → treat as click: pause video, open inline edit for the displayed segment. Also add a matching **dead zone in `onMove`** so sub-threshold movement doesn't nudge `position` during a click. Edit UI: caption-text span swaps for a `<textarea className="caption-edit-textarea">` inheriting the caption font; Enter/blur commits to the draft, Esc cancels; `startMove` early-returns while editing. Edit is pinned to the segment index it opened on.
- **Deleted segments in preview:** active-segment lookup skips draft-deleted indices (overlay shows the existing gap fallback); panel shows pending deletions dimmed/struck-through with an **Undo** button until saved.

## API contract (extended)

Request — count must equal stored count; deleted rows' text is ignored:
```json
{"segments": [{"text": "Hello world"}, {"text": "um, uh", "delete": true}]}
```
Response `200` — surviving segments only, full timing (client adopts as new baseline). Errors: `400` count mismatch (existing), `400` all deleted (new), `409` not ready (existing). Non-deleted segments keep the exact existing pass-through/re-timing logic (no timing shifts for survivors).

## Tasks (TDD, matching repo plan style)

### Task 1: Backend deletion support
Files: `backend/tests/test_transcript_editing.py`, `backend/app/schemas.py`, `backend/app/routers/jobs.py`, `CLAUDE.md`

1. Failing tests (reuse `speech_video` fixture pattern):
   - `test_deleting_a_segment_drops_it_and_rejects_stale_resubmission`: all-delete PUT → 400; delete segment 0 → 200 with n−1 segments, survivor timing byte-identical; GET confirms persistence; `/download/{id}/txt` unaffected pre-render is N/A — instead confirm GET transcript; re-PUT the stale n-count payload → 400.
   - `test_deleted_segment_is_absent_from_rendered_captions`: delete → render `classic` → fetch `.ass`, assert no Dialogue line has the deleted text and Dialogue count matches survivors.
2. Run → verify FAIL (pydantic currently ignores unknown `delete` field).
3. `schemas.py`: `TranscriptEditSegment` gains `delete: bool = False`.
4. `jobs.py` `update_transcript` (lines 83–112): `if edit.delete: continue` at top of the zip loop; after the loop, `400` if `not updated`; log gains `deleted=%d`.
5. Rebuild backend+worker (`docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build backend worker`), tests pass; full `pytest backend/tests/ -v` green (existing tests unchanged).
6. CLAUDE.md: `routers/jobs.py` row currently omits the PUT endpoint — rewrite the row to list all routes incl. the edit contract.
7. Commit: `feat: support deleting transcript segments via a delete flag on the edit endpoint`

### Task 2: Frontend shared draft + panel deletion
Files: `e2e/tests/upload-flow.spec.ts`, new `frontend/src/hooks/useTranscriptDraft.ts`, `frontend/src/api/client.ts`, `frontend/src/components/TranscriptEditor.tsx`, `frontend/src/components/PreviewEditor.tsx`, `frontend/src/App.css`

1. Failing Playwright test in the existing `Transcript editing` describe: Delete button → row `.deleted`, textarea disabled, button reads `Undo`, Save enabled, Render disabled; Undo reverts; delete + Save → row count −1 and API GET returns n−1 segments.
2. `api/client.ts`: `updateTranscript(jobId, edits: {text: string; delete?: boolean}[])`.
3. New `useTranscriptDraft(jobId, segments, onSegmentsChange)` → `{draft, setText, toggleDeleted, save, dirty, busy, saveState, errorMessage, survivorCount}`. Port TranscriptEditor's resync-on-jobId + dirty + save logic verbatim; `dirty` also true when any `deleted` flag set; save rebuilds draft from PUT response.
4. `TranscriptEditor` → controlled props `{segments, draft, onTextChange, onToggleDeleted, onSave, dirty, saveState, errorMessage, survivorCount}`; per-row Delete/Undo button (aria-label `Delete segment {i+1}` / `Undo delete`); disable Delete on last survivor (`title="At least one caption must remain"`); deleted rows dimmed + disabled textarea.
5. `PreviewEditor`: instantiate hook, drop `transcriptBusy`, Render disables on hook `busy`; active-segment lookup becomes `findIndex` skipping deleted; `displayText` reads from draft (previews unsaved edits), falls back to first non-deleted draft text, then `'Caption preview'`.
6. CSS: `.transcript-row.deleted`, `.transcript-delete-btn`.
7. Rebuild nginx image, new + existing Playwright `Transcript editing` tests pass.
8. Commit: `feat: transcript segment deletion in the editor panel via a shared draft`

### Task 3: Overlay click-to-edit
Files: `e2e/tests/upload-flow.spec.ts`, `frontend/src/components/PreviewEditor.tsx`, `frontend/src/App.css`

1. Failing Playwright tests: (a) click block → `.caption-edit-textarea` appears; type + Enter → overlay and panel textarea show same text (shared draft), Save enabled; Save persists (API GET). (b) Escape cancels cleanly. (c) Regression: mouse.down + move 40px + up → no textarea, block `left/top` changed.
2. Implement: `editingIndex` state, `editValue`, `videoRef`, `displayIndexRef` (window-listener effect has empty deps, so ref carries the displayed index); `startMove` early-return while editing; `onUp(e)` click detection (<5px) → pause + open edit; dead zone in `onMove` (no `setPosition` until movement ≥ 5px); textarea swaps in for caption text (stopPropagation on pointerdown, autoFocus, Enter commits via `setText(editingIndex, editValue)`, Esc cancels, blur commits). Update `preview-hint` copy to mention click-to-edit.
3. CSS: `.caption-edit-textarea` (transparent, inherited font, centered).
4. Rebuild nginx, full Playwright suite green.
5. Commit: `feat: click the preview caption to edit the current segment inline`

### Task 4: Docs, version, full verification

1. Copy this plan to `docs/superpowers/plans/2026-07-02-transcript-deletion-overlay-edit.md` (repo convention) as part of the first commit.
2. CLAUDE.md frontend module map: update `PreviewEditor.tsx` row; **add missing `TranscriptEditor.tsx` row**; add `hooks/useTranscriptDraft.ts` row; extend `api/client.ts` row.
3. Bump `frontend/package.json` 1.6.1 → 1.7.0 (repo convention).
4. Full verification: `bash scripts/run-e2e.sh` — everything green.
5. Commits: `docs: ...` + `Bump frontend package version to 1.7.0`.

## Edge cases

- Delete all → 400 server-side; client prevents via disabled last-survivor button.
- Double-save / stale tab → count check 400s; hook re-baselines from every PUT response.
- Render race → unchanged: unsaved deletions set `dirty` → Render disabled; PUT stays gated on `status == "ready"`; render CAS untouched.
- Timing integrity → deleted segments skipped before re-timing; survivors byte-identical unless text changed (preserves the chunking-isolation property the existing backend test guards).
- Playback inside a deleted segment → gap fallback; edit can't open there (lookup skips deleted).
- Edit + delete same row → server ignores text on deleted rows.
- Playback advancing mid-edit → edit pinned to its index; video paused on entry.

## Verification

- Backend: `pytest backend/tests/test_transcript_editing.py -v` then full `pytest backend/tests/ -v` against the running stack.
- Frontend/E2E: `npx playwright test` in `e2e/` (new deletion + click-to-edit tests plus existing suite).
- Final: `bash scripts/run-e2e.sh` one-shot, then manual sanity pass in the browser (delete a segment, click-edit the overlay, render, download, confirm captions).
