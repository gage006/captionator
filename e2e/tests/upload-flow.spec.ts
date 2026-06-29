import { test, expect, type Browser, type Page } from '@playwright/test'
import path from 'path'
import fs from 'fs'

const FIXTURE = path.resolve(__dirname, '../../backend/tests/fixtures/sample.mp4')
const PIPELINE_TIMEOUT = 600_000
const STYLE_COUNT = 9
const hasFixture = fs.existsSync(FIXTURE)

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

  test('shows all four download links', async () => {
    await expect(sharedPage.getByText('Download Video (MP4)')).toBeVisible()
    await expect(sharedPage.getByText('Download Subtitles (SRT)')).toBeVisible()
    await expect(sharedPage.getByText('Download Transcript (TXT)')).toBeVisible()
    await expect(sharedPage.getByText('Download ASS Subtitles')).toBeVisible()
  })

  test('download links point to the /api/download route', async () => {
    const videoLink = sharedPage.getByText('Download Video (MP4)')
    const href = await videoLink.getAttribute('href')
    // Job ids are now full uuid4 hex (32 chars).
    expect(href).toMatch(/^\/api\/download\/[a-f0-9]{32}\/video$/)
  })

  test('video download link triggers a file download', async () => {
    const [download] = await Promise.all([
      sharedPage.waitForEvent('download'),
      sharedPage.getByText('Download Video (MP4)').click(),
    ])
    expect(download.suggestedFilename()).toBe('captionated.mp4')
  })

  test('SRT download link triggers a file download', async () => {
    const [download] = await Promise.all([
      sharedPage.waitForEvent('download'),
      sharedPage.getByText('Download Subtitles (SRT)').click(),
    ])
    expect(download.suggestedFilename()).toBe('transcript.srt')
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
