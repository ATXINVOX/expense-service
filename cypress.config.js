const { defineConfig } = require("cypress");
const createBundler = require("@bahmutov/cypress-esbuild-preprocessor");
const {
  addCucumberPreprocessorPlugin,
} = require("@badeball/cypress-cucumber-preprocessor");
const {
  createEsbuildPlugin,
} = require("@badeball/cypress-cucumber-preprocessor/esbuild");

function deriveGatewayUrl(serviceUrl) {
  try {
    const parsed = new URL(serviceUrl);
    const host = (parsed.hostname || "").toLowerCase();
    // In CI/container networks, expense service often resolves to dev-central-site.
    // Kong is a separate service and should be addressed by service name.
    if (host.includes("central-site")) {
      return `${parsed.protocol}//kong:8000`;
    }
    return `${parsed.protocol}//${parsed.hostname}:8000`;
  } catch (_e) {
    return "http://localhost:8000";
  }
}

const expenseServiceUrl = process.env.CYPRESS_EXPENSE_SERVICE_URL || "http://localhost:9004";
const expenseGatewayUrl =
  process.env.CYPRESS_API_BASE_URL ||
  process.env.API_BASE_URL ||
  process.env.CYPRESS_EXPENSE_GATEWAY_URL ||
  deriveGatewayUrl(expenseServiceUrl);

module.exports = defineConfig({
  e2e: {
    // CYPRESS_EXPENSE_SERVICE_URL is set by docker-compose; fall back to
    // localhost:9004 for local runs without Docker.
    baseUrl: expenseServiceUrl,
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
    EXPENSE_SERVICE_URL: expenseServiceUrl,
    EXPENSE_GATEWAY_URL: expenseGatewayUrl,
    EXPENSE_FRAPPE_URL: expenseGatewayUrl,
    API_BASE_URL: expenseGatewayUrl,
    EXPENSE_TEST_SID: "",
    EXPENSE_TEST_COMPANY: process.env.CYPRESS_EXPENSE_TEST_COMPANY || "Acme Pty Ltd",
  },
  video: false,
  screenshotOnRunFailure: false,
});
