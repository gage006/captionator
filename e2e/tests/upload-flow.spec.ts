import { test, expect, type Browser, type Page } from '@playwright/test'
import path from 'path'
import fs from 'fs'

const FIXTURE = path.resolve(__dirname, '../../backend/tests/fixtures/sample.mp4')
const PIPELINE_TIMEOUT = 600_000
const STYLE_COUNT = 9

// Skip every test in this file if the fixture hasn't been generated yet.
test.beforeEach(async ({}, testInfo) => {
  if (!fs.existsSync(FIXTURE)) {
    testInfo.skip(
      true,
      'Test fixture not found. Run: bash scripts/create_test_fixture.sh',
    )
  }
})

// ---------------------------------------------------------------------------
// Idle UI state — fast tests that don't run the pipeline
// ---------------------------------------------------------------------------

test.describe('Idle state', () => {
  test('shows the style picker with all styles', async ({ page }) => {
    await page.goto('/')
    await expect(page.getByText('Caption Style')).toBeVisible()
    await expect(page.locator('.style-card')).toHaveCount(STYLE_COUNT)
  })

  test('has exactly one style pre-selected', async ({ page }) => {
    await page.goto('/')
    await expect(page.locator('.style-card.selected')).toHaveCount(1)
  })

  test('clicking a style card selects it', async ({ page }) => {
    await page.goto('/')
    const neonCard = page.getByRole('button', { name: /Neon/i })
    await neonCard.click()
    await expect(neonCard).toHaveClass(/selected/)
  })

  test('shows upload zone', async ({ page }) => {
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
})

// ---------------------------------------------------------------------------
// Full pipeline — runs the video through the processing stack once,
// then re-uses that shared page for all assertions inside this describe block.
// ---------------------------------------------------------------------------

test.describe('Full pipeline', () => {
  let sharedPage: Page

  test.beforeAll(async ({ browser }: { browser: Browser }) => {
    sharedPage = await browser.newPage()
    await sharedPage.goto('http://localhost')

    // Pick "Minimal" (lightest style, no compound codepath)
    await sharedPage.getByRole('button', { name: /Minimal/i }).click()

    // Inject the file directly into the hidden input — bypasses the OS file dialog
    await sharedPage.locator('input[type="file"]').setInputFiles(FIXTURE)

    // Wait for the pipeline to finish — this is the slow part
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
    expect(href).toMatch(/^\/api\/download\/[a-f0-9]{8}\/video$/)
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
    await expect(sharedPage.getByText('Caption Style')).toBeVisible()
    await expect(sharedPage.getByText('Tap to select a video')).toBeVisible()
    // Download panel must be gone
    await expect(sharedPage.getByText('Your video is ready!')).not.toBeVisible()
  })
})

// ---------------------------------------------------------------------------
// Progress tracker visibility during processing
// ---------------------------------------------------------------------------

test('progress tracker appears after upload', async ({ page }) => {
  await page.goto('/')
  await page.locator('input[type="file"]').setInputFiles(FIXTURE)

  // The progress-tracker component should appear once polling starts
  await expect(page.locator('.progress-tracker')).toBeVisible({ timeout: 30_000 })

  // Progress bar should show some value
  const progress = page.locator('progress')
  await expect(progress).toBeVisible()
})
