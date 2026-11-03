const playwright = process.env.PLAYWRIGHT_MODULE || 'playwright';
const { chromium } = require(playwright);
const { expect } = require(playwright + '/test');
(async () => {
  const browser = await chromium.launch({ channel: process.env.BROWSER_CHANNEL || 'msedge', headless: true });
  try {
    const context = await browser.newContext({ viewport: { width: 393, height: 851 }, isMobile: true, hasTouch: true, deviceScaleFactor: 2, userAgent: 'Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36' });
    const page = await context.newPage();
    await page.goto(process.env.JUDGING_TEST_URL);
    if (process.argv.includes('--before')) {
      const input = page.getByTestId('stNumberInput').first().locator('input');
      await input.waitFor();
      await input.fill('5');
      await expect(page.getByText('Заполнено: 0 / 7', { exact: false })).toBeVisible();
      await expect(page.getByRole('button', { name: 'Отправить и заблокировать', exact: true })).toBeDisabled();
      console.log('REPRODUCED_TYPED_SCORE_NOT_COMMITTED_AND_SUBMIT_DISABLED');
      await input.press('Enter');
      await expect(page.getByText('Заполнено: 1 / 7', { exact: false })).toBeVisible();
      console.log('OLD_FIELD_REQUIRES_EXPLICIT_COMMIT');
      return;
    }
    // Deliberately use touch only: never press Enter or type into score fields.
    await expect(page.getByText('Заполнено: 0 / 7', { exact: false })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Отправить и заблокировать', exact: true })).toBeDisabled();
    await expect(page.getByTestId('stNumberInput')).toHaveCount(0);
    for (let i = 0; i < 7; i++) {
      await page.getByTestId('stSelectbox').nth(i + 1).getByRole('combobox').tap();
      await page.getByRole('option', { name: '0', exact: true }).tap();
      await expect(page.getByText(`Заполнено: ${i + 1} / 7`, { exact: false })).toBeVisible();
    }
    await page.getByRole('button', { name: 'Отправить и заблокировать', exact: true }).tap();
    await expect(page.getByText('Карточка отправлена.', { exact: false })).toBeVisible();
    await expect(page.getByTestId('stSelectbox')).toHaveCount(1);
    console.log('MOBILE_TOUCH_ZERO_SCORES_SENT_WITHOUT_ENTER');

    await page.getByTestId('stSelectbox').first().getByRole('combobox').tap();
    await page.getByRole('option', { name: '1. Mobile work 1', exact: true }).tap();
    await expect(page.getByText('Заполнено: 0 / 7', { exact: false })).toBeVisible();
    for (let i = 0; i < 7; i++) {
      await page.getByTestId('stSelectbox').nth(i + 1).getByRole('combobox').tap();
      await page.getByRole('option', { name: String(i + 1), exact: true }).tap();
      await expect(page.getByText(`Заполнено: ${i + 1} / 7`, { exact: false })).toBeVisible();
    }
    await page.getByRole('button', { name: 'Сохранить черновик', exact: true }).tap();
    await expect(page.getByText('Сохранено:', { exact: false })).toBeVisible();
    await page.reload();
    await page.getByTestId('stSelectbox').first().getByRole('combobox').tap();
    await page.getByRole('option', { name: '1. Mobile work 1', exact: true }).tap();
    await expect(page.getByText('Заполнено: 7 / 7', { exact: false })).toBeVisible();
    await page.getByRole('button', { name: 'Отправить и заблокировать', exact: true }).tap();
    await expect(page.getByText('Карточка отправлена.', { exact: false })).toBeVisible();
    console.log('MOBILE_TOUCH_NONZERO_DRAFT_RELOADED_AND_SENT_WITHOUT_ENTER');
  } finally { await browser.close(); }
})().catch(e => { console.error(e.message); process.exit(1); });
