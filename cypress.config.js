const { defineConfig } = require("cypress");
const createBundler = require("@bahmutov/cypress-esbuild-preprocessor");
const {
  addCucumberPreprocessorPlugin,
} = require("@badeball/cypress-cucumber-preprocessor");
const {
  createEsbuildPlugin,
} = require("@badeball/cypress-cucumber-preprocessor/esbuild");

module.exports = defineConfig({
  e2e: {
    // CYPRESS_EXPENSE_SERVICE_URL is set by docker-compose; fall back to
    // localhost:9004 for local runs without Docker.
    baseUrl: process.env.CYPRESS_EXPENSE_SERVICE_URL || "http://localhost:9004",
    specPattern: "cypress/e2e/features/**/*.feature",
    supportFile: false,
    async setupNodeEvents(on, config) {
      await addCucumberPreprocessorPlugin(on, config);
      on(
        "file:preprocessor",
        createBundler({ plugins: [createEsbuildPlugin(config)] })
      );
      return config;
    },
  },
  env: {
    EXPENSE_SERVICE_URL: process.env.CYPRESS_EXPENSE_SERVICE_URL || "http://localhost:9004",
    EXPENSE_TEST_SID: "",
    EXPENSE_TEST_COMPANY: process.env.CYPRESS_EXPENSE_TEST_COMPANY || "Acme Pty Ltd",
  },
  video: false,
  screenshotOnRunFailure: false,
});
