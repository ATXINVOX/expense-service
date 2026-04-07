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
    baseUrl: "http://localhost:9004",
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
    EXPENSE_SERVICE_URL: "http://localhost:9004",
    EXPENSE_TEST_SID: "",
    EXPENSE_TEST_COMPANY: "Acme Pty Ltd",
  },
  video: false,
  screenshotOnRunFailure: false,
});
