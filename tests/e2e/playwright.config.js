const { defineConfig } = require('@playwright/test');
const path = require('path');

module.exports = defineConfig({
  testDir: '.',
  testMatch: '**/*.spec.js',
  timeout: 60_000,
  retries: process.env.CI ? 2 : 0,
  reporter: process.env.CI ? 'github' : 'list',

  use: {
    baseURL: 'http://localhost:3456',
    trace: 'retain-on-failure',
    launchOptions: {
      slowMo: 300,
    },
  },

  webServer: {
    command: `npx serve ${path.join(__dirname, '../../ui')} --listen 3456 --no-clipboard`,
    url: 'http://localhost:3456',
    reuseExistingServer: !process.env.CI,
  },
});
