import { test, expect, type Browser, type Page } from '@playwright/test'
import path from 'path'
import fs from 'fs'

const FIXTURE = path.resolve(__dirname, '../../backend/tests/fixtures/sample.mp4')
// sample.mp4 is a tone with no speech, so it never produces transcript text —
// use a real-speech fixture to exercise actual transcript editing.
const SPEECH_FIXTURE = path.resolve(
  __dirname,
  '../../backend/tests/fixtures/real_speech_synthetic.mp4',
)
const PIPELINE_TIMEOUT = 600_000
const STYLE_COUNT = 10
const hasFixture = fs.existsSync(FIXTURE)
const hasSpeechFixture = fs.existsSync(SPEECH_FIXTURE)

// Skip every test if the fixture hasn't been generated yet. beforeAll hooks below
// also early-return on a missing fixture, since they run before this beforeEach.
test.beforeEach(async ({}, testInfo) => {
  if (!hasFixture) {
    testInfo.skip(
      true,
      'Test fixture not found. Run: bash scripts/create_test_fixture.sh',
    )
  }
})

// ---------------------------------------------------------------------------
// Idle UI state — the landing screen is just the upload zone now; style choice
// moved into the editor that appears after transcription.
// ---------------------------------------------------------------------------

test.describe('Idle state', () => {
  test('shows the upload zone', async ({ page }) => {
    await page.goto('/')
    await expect(page.getByText('Tap to select a video')).toBeVisible()
    await expect(page.getByText(/MP4, MOV/i)).toBeVisible()
  })

  test('upload zone is keyboard-accessible', async ({ page }) => {
    await page.goto('/')
    const zone = page.locator('.upload-zone')
    await expect(zone).toHaveAttribute('role', 'button')
    await expect(zone).toHaveAttribute('tabindex', '0')
  })

  test('does not show the style picker until a video is uploaded', async ({ page }) => {
    await page.goto('/')
    await expect(page.getByText('Caption Style')).toHaveCount(0)
    await expect(page.locator('.style-card')).toHaveCount(0)
  })

  test('shows the remove-silences checkbox, unchecked by default', async ({ page }) => {
    await page.goto('/')
    const checkbox = page.getByLabel(/Remove silences/i)
    await expect(checkbox).toBeVisible()
    await expect(checkbox).not.toBeChecked()
  })
})

// ---------------------------------------------------------------------------
// Editing phase — after upload + transcription, the preview editor appears with
// the style picker. Drives phase 1 of the pipeline but stops before rendering.
// ---------------------------------------------------------------------------

test.describe('Editing phase', () => {
  let page: Page

  test.beforeAll(async ({ browser }: { browser: Browser }) => {
    if (!hasFixture) return
    page = await browser.newPage()
    await page.goto('http://localhost')
    await page.locator('input[type="file"]').setInputFiles(FIXTURE)
    // Wait for transcription to finish and the editor to appear.
    await expect(
      page.getByRole('button', { name: /Render with this style/i }),
    ).toBeVisible({ timeout: PIPELINE_TIMEOUT })
  })

  test.afterAll(async () => {
    await page?.close()
  })

  test('shows the style picker with all styles', async () => {
    await expect(page.getByText('Caption Style')).toBeVisible()
    await expect(page.locator('.style-card')).toHaveCount(STYLE_COUNT)
  })

  test('has exactly one style pre-selected', async () => {
    await expect(page.locator('.style-card.selected')).toHaveCount(1)
  })

  test('clicking a style card selects it', async () => {
    const neonCard = page.getByRole('button', { name: /Neon/i })
    await neonCard.click()
    await expect(neonCard).toHaveClass(/selected/)
  })

  test('shows the source video preview', async () => {
    await expect(page.locator('video.preview-video')).toBeVisible()
  })

  test('shows the Style and Transcript sections, both open by default', async () => {
    await expect(page.getByRole('button', { name: 'Caption Style' })).toHaveAttribute(
      'aria-expanded',
      'true',
    )
    await expect(page.getByRole('button', { name: 'Transcript' })).toHaveAttribute(
      'aria-expanded',
      'true',
    )
    await expect(page.locator('.style-grid')).toBeVisible()
  })

  test('Style and Transcript sections collapse and expand independently', async () => {
    const styleHeader = page.getByRole('button', { name: 'Caption Style' })
    const transcriptHeader = page.getByRole('button', { name: 'Transcript' })

    await transcriptHeader.click()
    await expect(transcriptHeader).toHaveAttribute('aria-expanded', 'false')
    // Collapsing Transcript must not affect Style.
    await expect(styleHeader).toHaveAttribute('aria-expanded', 'true')
    await expect(page.locator('.style-grid')).toBeVisible()

    await styleHeader.click()
    await expect(styleHeader).toHaveAttribute('aria-expanded', 'false')
    await expect(page.locator('.style-grid')).not.toBeVisible()

    // Restore both open for any later test in this shared-page block.
    await transcriptHeader.click()
    await styleHeader.click()
    await expect(transcriptHeader).toHaveAttribute('aria-expanded', 'true')
    await expect(styleHeader).toHaveAttribute('aria-expanded', 'true')
  })
})

// ---------------------------------------------------------------------------
// Transcript editing — needs a fixture with real speech, since sample.mp4
// produces no transcript text to edit.
// ---------------------------------------------------------------------------

test.describe('Transcript editing', () => {
  test.beforeEach(async ({}, testInfo) => {
    if (!hasSpeechFixture) {
      testInfo.skip(
        true,
        'Speech fixture not found. Run: bash scripts/create_real_speech_fixtures.sh',
      )
    }
  })

  test('editing a segment and saving persists the new text', async ({ page }) => {
    await page.goto('/')

    // Capture the job_id so we can verify server-side persistence directly —
    // the app has no URL/localStorage state to restore from on reload, so a
    // page reload can't be used to prove the save round-tripped to the API.
    const uploadResponse = page.waitForResponse((r) => r.url().includes('/api/upload'))
    await page.locator('input[type="file"]').setInputFiles(SPEECH_FIXTURE)
    const jobId = (await (await uploadResponse).json()).job_id as string
    expect(jobId).toMatch(/^[a-f0-9]{32}$/)

    await expect(
      page.getByRole('button', { name: /Render with this style/i }),
    ).toBeVisible({ timeout: PIPELINE_TIMEOUT })

    const firstTextarea = page.locator('.transcript-textarea').first()
    await expect(firstTextarea).toBeVisible()
    const original = await firstTextarea.inputValue()
    expect(original.length).toBeGreaterThan(0)

    const saveBtn = page.getByRole('button', { name: /Save Transcript/i })
    await expect(saveBtn).toBeDisabled() // nothing edited yet

    await firstTextarea.fill('Edited by the e2e test.')
    await expect(saveBtn).toBeEnabled()
    await saveBtn.click()

    await expect(page.locator('.transcript-status-ok')).toHaveText('Saved')
    await expect(firstTextarea).toHaveValue('Edited by the e2e test.')

    // Confirm the edit actually persisted server-side, not just local state.
    const persisted = await (
      await page.request.get(`/api/jobs/${jobId}/transcript`)
    ).json()
    expect(persisted.segments[0].text).toBe('Edited by the e2e test.')
  })

  test('deleting a segment in the panel persists after save', async ({ page }) => {
    await page.goto('/')

    const uploadResponse = page.waitForResponse((r) => r.url().includes('/api/upload'))
    await page.locator('input[type="file"]').setInputFiles(SPEECH_FIXTURE)
    const jobId = (await (await uploadResponse).json()).job_id as string

    const renderBtn = page.getByRole('button', { name: /Render with this style/i })
    await expect(renderBtn).toBeVisible({ timeout: PIPELINE_TIMEOUT })

    const rows = page.locator('.transcript-row')
    const rowCount = await rows.count()
    test.skip(rowCount < 2, 'fixture produced only one segment this run; nothing to delete')

    const saveBtn = page.getByRole('button', { name: /Save Transcript/i })
    await expect(saveBtn).toBeDisabled()

    // Flag the first segment for deletion: row dims, its textarea locks, the
    // button flips to Undo, and the pending change gates rendering.
    await page.getByRole('button', { name: 'Delete segment 1' }).click()
    await expect(rows.first()).toHaveClass(/deleted/)
    await expect(rows.first().locator('.transcript-textarea')).toBeDisabled()
    await expect(saveBtn).toBeEnabled()
    await expect(renderBtn).toBeDisabled()

    // Undo restores a clean slate.
    await page.getByRole('button', { name: 'Undo delete' }).click()
    await expect(rows.first()).not.toHaveClass(/deleted/)
    await expect(saveBtn).toBeDisabled()
    await expect(renderBtn).toBeEnabled()

    // Delete for real and save: the row disappears and the API agrees.
    await page.getByRole('button', { name: 'Delete segment 1' }).click()
    await saveBtn.click()
    await expect(page.locator('.transcript-status-ok')).toHaveText('Saved')
    await expect(rows).toHaveCount(rowCount - 1)

    const persisted = await (
      await page.request.get(`/api/jobs/${jobId}/transcript`)
    ).json()
    expect(persisted.segments.length).toBe(rowCount - 1)
  })

  test('Render button is disabled while a transcript edit is unsaved', async ({ page }) => {
    await page.goto('/')
    await page.locator('input[type="file"]').setInputFiles(SPEECH_FIXTURE)

    const renderBtn = page.getByRole('button', { name: /Render with this style/i })
    await expect(renderBtn).toBeVisible({ timeout: PIPELINE_TIMEOUT })
    await expect(renderBtn).toBeEnabled()

    const firstTextarea = page.locator('.transcript-textarea').first()
    await firstTextarea.fill('Edited but not saved yet.')
    await expect(renderBtn).toBeDisabled()

    const saveBtn = page.getByRole('button', { name: /Save Transcript/i })
    await saveBtn.click()
    await expect(page.locator('.transcript-status-ok')).toHaveText('Saved')
    await expect(renderBtn).toBeEnabled()
  })
})

// ---------------------------------------------------------------------------
// Overlay click-to-edit — clicking the caption block (without dragging) opens
// an inline editor for the currently displayed segment, feeding the same
// shared draft as the panel.
// ---------------------------------------------------------------------------

test.describe('Overlay click-to-edit', () => {
  test.beforeEach(async ({}, testInfo) => {
    if (!hasSpeechFixture) {
      testInfo.skip(
        true,
        'Speech fixture not found. Run: bash scripts/create_real_speech_fixtures.sh',
      )
    }
  })

  async function uploadAndWaitForEditor(page: Page): Promise<string> {
    await page.goto('/')
    const uploadResponse = page.waitForResponse((r) => r.url().includes('/api/upload'))
    await page.locator('input[type="file"]').setInputFiles(SPEECH_FIXTURE)
    const jobId = (await (await uploadResponse).json()).job_id as string
    await expect(
      page.getByRole('button', { name: /Render with this style/i }),
    ).toBeVisible({ timeout: PIPELINE_TIMEOUT })
    await expect(page.locator('.caption-block')).toBeVisible()
    return jobId
  }

  test('clicking the caption opens inline editing that feeds the shared draft', async ({
    page,
  }) => {
    const jobId = await uploadAndWaitForEditor(page)

    // The preview seeks into the first caption, so the overlay shows (and the
    // click edits) segment 1.
    await page.locator('.caption-block').click()
    const editArea = page.locator('.caption-edit-textarea')
    await expect(editArea).toBeVisible()

    await editArea.fill('Edited from the overlay.')
    await editArea.press('Enter')
    await expect(editArea).toHaveCount(0)

    // Committed to the shared draft: overlay preview + panel row both show it.
    await expect(page.locator('.caption-text')).toContainText('Edited from the overlay.')
    await expect(page.locator('.transcript-textarea').first()).toHaveValue(
      'Edited from the overlay.',
    )

    // Unsaved edit gates rendering; saving persists it server-side.
    const saveBtn = page.getByRole('button', { name: /Save Transcript/i })
    await expect(saveBtn).toBeEnabled()
    await expect(
      page.getByRole('button', { name: /Render with this style/i }),
    ).toBeDisabled()
    await saveBtn.click()
    await expect(page.locator('.transcript-status-ok')).toHaveText('Saved')
    const persisted = await (
      await page.request.get(`/api/jobs/${jobId}/transcript`)
    ).json()
    expect(persisted.segments[0].text).toBe('Edited from the overlay.')
  })

  test('Escape cancels an overlay edit without touching the draft', async ({ page }) => {
    await uploadAndWaitForEditor(page)

    const original = await page.locator('.transcript-textarea').first().inputValue()

    await page.locator('.caption-block').click()
    const editArea = page.locator('.caption-edit-textarea')
    await expect(editArea).toBeVisible()
    await editArea.fill('This text must be discarded.')
    await editArea.press('Escape')
    await expect(editArea).toHaveCount(0)

    await expect(page.locator('.transcript-textarea').first()).toHaveValue(original)
    await expect(page.getByRole('button', { name: /Save Transcript/i })).toBeDisabled()
  })

  test('dragging the caption still repositions it without opening the editor', async ({
    page,
  }) => {
    await uploadAndWaitForEditor(page)

    const block = page.locator('.caption-block')
    const before = await block.evaluate((el) => el.style.left + '|' + el.style.top)

    const box = await block.boundingBox()
    if (!box) throw new Error('caption block has no bounding box')
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2)
    await page.mouse.down()
    await page.mouse.move(box.x + box.width / 2 + 40, box.y + box.height / 2 - 30, {
      steps: 5,
    })
    await page.mouse.up()

    await expect(page.locator('.caption-edit-textarea')).toHaveCount(0)
    const after = await block.evaluate((el) => el.style.left + '|' + el.style.top)
    expect(after).not.toBe(before)
  })
})

// ---------------------------------------------------------------------------
// Full pipeline — upload → transcribe → render → complete, then re-use the
// shared page for all download/reset assertions inside this describe block.
// ---------------------------------------------------------------------------

test.describe('Full pipeline', () => {
  let sharedPage: Page

  test.beforeAll(async ({ browser }: { browser: Browser }) => {
    if (!hasFixture) return
    sharedPage = await browser.newPage()
    await sharedPage.goto('http://localhost')

    // Inject the file directly into the hidden input — bypasses the OS file dialog.
    await sharedPage.locator('input[type="file"]').setInputFiles(FIXTURE)

    // Phase 1: wait for the editor, pick the lightest style, kick off the render.
    const renderBtn = sharedPage.getByRole('button', {
      name: /Render with this style/i,
    })
    await expect(renderBtn).toBeVisible({ timeout: PIPELINE_TIMEOUT })
    await sharedPage.getByRole('button', { name: /Minimal/i }).click()
    await renderBtn.click()

    // Phase 2: wait for the burn to finish — this is the slow part.
    await expect(sharedPage.getByText('Your video is ready!')).toBeVisible({
      timeout: PIPELINE_TIMEOUT,
    })
  })

  test.afterAll(async () => {
    await sharedPage?.close()
  })

  test('shows success heading and checkmark', async () => {
    await expect(sharedPage.getByText('Your video is ready!')).toBeVisible()
  })

  test('shows all three download panel actions', async () => {
    await expect(sharedPage.getByText('Download Video')).toBeVisible()
    await expect(sharedPage.getByText('Download Transcript')).toBeVisible()
    await expect(sharedPage.getByText('Copy AI Prompt')).toBeVisible()
  })

  test('download links point to the /api/download route', async () => {
    const videoLink = sharedPage.getByText('Download Video')
    const href = await videoLink.getAttribute('href')
    // Job ids are now full uuid4 hex (32 chars).
    expect(href).toMatch(/^\/api\/download\/[a-f0-9]{32}\/video$/)
  })

  test('video download link triggers a file download', async () => {
    const [download] = await Promise.all([
      sharedPage.waitForEvent('download'),
      sharedPage.getByText('Download Video').click(),
    ])
    expect(download.suggestedFilename()).toBe('captionated.mp4')
  })

  test('transcript download link triggers a file download', async () => {
    const [download] = await Promise.all([
      sharedPage.waitForEvent('download'),
      sharedPage.getByText('Download Transcript').click(),
    ])
    expect(download.suggestedFilename()).toBe('transcript.txt')
  })

  test('shows "Process another video" button', async () => {
    await expect(sharedPage.getByText('Process another video')).toBeVisible()
  })

  test('reset button returns to idle state', async () => {
    await sharedPage.getByText('Process another video').click()
    await expect(sharedPage.getByText('Tap to select a video')).toBeVisible()
    // Back at idle: the editor's style picker and the success panel are gone.
    await expect(sharedPage.getByText('Caption Style')).toHaveCount(0)
    await expect(sharedPage.getByText('Your video is ready!')).not.toBeVisible()
  })
})

// ---------------------------------------------------------------------------
// Browser back/forward — the job id lives in the URL so navigating away and
// pressing Back restores the job instead of dead-ending at a blank idle page.
// ---------------------------------------------------------------------------

test.describe('Browser navigation persistence', () => {
  test('pressing Back after "Process another video" returns to the finished job', async ({
    page,
  }) => {
    await page.goto('/')
    await expect(page).toHaveURL(/\/$/)

    await page.locator('input[type="file"]').setInputFiles(FIXTURE)
    await expect(page).toHaveURL(/\?job=[a-f0-9]{32}/)

    const renderBtn = page.getByRole('button', { name: /Render with this style/i })
    await expect(renderBtn).toBeVisible({ timeout: PIPELINE_TIMEOUT })
    await renderBtn.click()
    await expect(page.getByText('Your video is ready!')).toBeVisible({
      timeout: PIPELINE_TIMEOUT,
    })
    const jobUrl = page.url()

    // Leave for a new "page" the way a user does via the reset button.
    await page.getByText('Process another video').click()
    await expect(page.getByText('Tap to select a video')).toBeVisible()
    await expect(page).toHaveURL(/\/$/)

    // Pressing Back must not be a dead end — it should restore the finished job.
    await page.goBack()
    await expect(page).toHaveURL(jobUrl)
    await expect(page.getByText('Your video is ready!')).toBeVisible({ timeout: 15_000 })

    // Forward returns to idle again.
    await page.goForward()
    await expect(page.getByText('Tap to select a video')).toBeVisible()
  })

  test('reloading mid-edit restores the same job from the URL', async ({ page }) => {
    await page.goto('/')
    await page.locator('input[type="file"]').setInputFiles(FIXTURE)
    await expect(page.locator('.progress-tracker, .preview-editor')).toBeVisible({
      timeout: PIPELINE_TIMEOUT,
    })
    const jobUrl = page.url()
    expect(jobUrl).toMatch(/\?job=[a-f0-9]{32}/)

    await page.reload()
    await expect(page).toHaveURL(jobUrl)
    await expect(page.locator('.progress-tracker, .preview-editor')).toBeVisible({
      timeout: 15_000,
    })
  })
})

// ---------------------------------------------------------------------------
// Processing UI appears after upload
// ---------------------------------------------------------------------------

test('processing UI appears after upload', async ({ page }) => {
  await page.goto('/')
  await page.locator('input[type="file"]').setInputFiles(FIXTURE)

  // After upload we leave idle: either the transcription progress tracker shows,
  // or (for a very short clip) we've already advanced to the preview editor.
  await expect(page.locator('.progress-tracker, .preview-editor')).toBeVisible({
    timeout: 30_000,
  })
})
