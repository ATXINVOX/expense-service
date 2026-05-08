/// <reference types="cypress" />
import { Given, When, Then } from "@badeball/cypress-cucumber-preprocessor";

/** Shared state between steps in a scenario */
const state = {
  lastResponse: null,
  storedInvoiceName: null,
  sid: null,
};

function serviceBaseUrl() {
  return Cypress.env("EXPENSE_SERVICE_URL") || "http://localhost:9004";
}

function frappeBaseUrl() {
  return Cypress.env("EXPENSE_FRAPPE_URL") || "http://localhost:8000";
}

function sessionHeaders() {
  return {
    "Content-Type": "application/json",
    Accept: "application/json",
    "X-Requested-With": "XMLHttpRequest",
    ...(state.sid ? { Cookie: `sid=${state.sid}`, "X-Frappe-SID": state.sid } : {}),
  };
}

function responsePayload() {
  const body = state.lastResponse?.body;
  if (!body || typeof body !== "object" || Array.isArray(body)) {
    return body;
  }
  // Some gateways wrap method responses as { message: { ... } }.
  if (body.message && typeof body.message === "object" && !Array.isArray(body.message)) {
    return body.message;
  }
  return body;
}

/** Calendar Y-m-d in the Cypress runner's local TZ (avoids hardcoded Gherkin dates missing Fiscal Year). */
function integrationPostingDate() {
  const fromEnv = Cypress.env("EXPENSE_POSTING_DATE");
  if (fromEnv && String(fromEnv).trim()) {
    return String(fromEnv).trim();
  }
  const d = new Date();
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

function applyIntegrationPostingDate(body) {
  if (!body || typeof body !== "object" || !("posting_date" in body)) {
    return;
  }
  Object.assign(body, { posting_date: integrationPostingDate() });
}

function postPurchaseInvoiceWithSession(body) {
  applyIntegrationPostingDate(body);
  cy.request({
    method: "POST",
    url: `${serviceBaseUrl()}/api/resource/Purchase%20Invoice`,
    headers: sessionHeaders(),
    body,
    failOnStatusCode: false,
  }).then((res) => {
    state.lastResponse = res;
  });
}

// ---------------------------------------------------------------------------
// Auth: login via Frappe session API and export SID
// ---------------------------------------------------------------------------

Given("I login to the expense service as {string}", (user) => {
  const envSid = Cypress.env("EXPENSE_TEST_SID");
  if (envSid && String(envSid).trim()) {
    state.sid = String(envSid).trim();
    cy.log(`Using EXPENSE_TEST_SID from environment for ${user}`);
    return;
  }
  const pwd = Cypress.env("ADMIN_PASSWORD") || "admin";
  _loginAsFrappeUser(user, pwd);
});

Given("I login to the expense service as {string} with password {string}", (user, pwd) => {
  const envSid = Cypress.env("EXPENSE_TEST_SID");
  if (envSid && String(envSid).trim()) {
    state.sid = String(envSid).trim();
    cy.log(`Using EXPENSE_TEST_SID from environment for ${user}`);
    return;
  }
  _loginAsFrappeUser(user, pwd);
});

function _loginAsFrappeUser(user, pwd) {
  // Reuse an already acquired SID across scenarios in the same spec run.
  // This avoids hammering login endpoints and hitting gateway rate limits.
  if (state.sid && state.sid !== "Guest") {
    cy.log(`Reusing existing SID for ${user}`);
    return;
  }

  const captureSid = (res) => {
    const bodySid = res.body?.sid;
    if (bodySid && bodySid !== "Guest") {
      state.sid = bodySid;
      cy.log(`Logged in as ${user} — SID from body`);
      return true;
    }

    const setCookie = [].concat(res.headers?.["set-cookie"] ?? []).join(";");
    const cookieMatch = setCookie.match(/\bsid=([^;,\s]+)/i);
    const cookieSid = cookieMatch?.[1];

    if (cookieSid && cookieSid !== "Guest") {
      state.sid = cookieSid;
      cy.log(`Logged in as ${user} — SID from Set-Cookie`);
      return true;
    }
    return false;
  };

  const assertCookieSid = () => {
    cy.getCookie("sid").then((cookie) => {
      const sid = cookie?.value;
      expect(sid, `Login as '${user}' must return a non-guest SID`).to.be.a("string").and.not.eq("Guest");
      state.sid = sid;
      cy.log(`Logged in as ${user} — SID from Cypress cookie jar`);
    });
  };

  cy.request({
    method: "POST",
    url: `${frappeBaseUrl()}/api/method/login`,
    headers: {
      "Content-Type": "application/json",
      Accept: "application/json",
    },
    body: { usr: user, pwd },
    failOnStatusCode: false,
  }).then((res) => {
    if (captureSid(res)) {
      return;
    }
    if (res.status === 429) {
      cy.log(`Login rate-limited for ${user}: ${JSON.stringify(res.body)}`);
      assertCookieSid();
      return;
    }
    // Fallback for environments that still expect form-urlencoded payloads.
    if (res.status === 415) {
      cy.request({
        method: "POST",
        url: `${frappeBaseUrl()}/api/method/login`,
        headers: {
          "Content-Type": "application/x-www-form-urlencoded",
          Accept: "application/json",
        },
        body: `usr=${encodeURIComponent(user)}&pwd=${encodeURIComponent(pwd)}`,
        failOnStatusCode: false,
      }).then((jsonRes) => {
        if (captureSid(jsonRes)) return;
        assertCookieSid();
      });
      return;
    }
    assertCookieSid();
  });
}

// Legacy step kept for backward compatibility with manual SID injection
Given("the expense API test session is configured", () => {
  const envSid = Cypress.env("EXPENSE_TEST_SID");
  if (envSid) {
    state.sid = envSid;
    cy.log("Using EXPENSE_TEST_SID from environment");
    return;
  }
  // Fall back to automatic login as Administrator
  _loginAsFrappeUser("Administrator", Cypress.env("ADMIN_PASSWORD") || "admin");
});

// ---------------------------------------------------------------------------
// Purchase Invoice: POST (create)
// ---------------------------------------------------------------------------

When("I POST a new Purchase Invoice for expense draft submit with:", (dataTable) => {
  const body = {};
  const company = Cypress.env("EXPENSE_TEST_COMPANY") || "Acme Pty Ltd";
  for (const row of dataTable.hashes()) {
    const key = row.field.trim();
    const val = row.value.trim();
    if (key === "items_json") {
      body.items = JSON.parse(val);
    } else {
      body[key] = val;
    }
  }
  if (!body.company) body.company = company;
  postPurchaseInvoiceWithSession(body);
});

When("I POST a new Purchase Invoice with body:", (docString) => {
  const body = JSON.parse(docString.trim());
  const company = Cypress.env("EXPENSE_TEST_COMPANY") || "Acme Pty Ltd";
  if (!body.company) body.company = company;
  postPurchaseInvoiceWithSession(body);
});

// ---------------------------------------------------------------------------
// Purchase Invoice: GET
// ---------------------------------------------------------------------------

When("I GET the stored Purchase Invoice", () => {
  const enc = encodeURIComponent(state.storedInvoiceName);
  cy.request({
    method: "GET",
    url: `${serviceBaseUrl()}/api/resource/Purchase%20Invoice/${enc}`,
    headers: sessionHeaders(),
    failOnStatusCode: false,
  }).then((res) => { state.lastResponse = res; });
});

// ---------------------------------------------------------------------------
// Purchase Invoice: DELETE
// ---------------------------------------------------------------------------

When("I DELETE the stored Purchase Invoice", () => {
  const enc = encodeURIComponent(state.storedInvoiceName);
  cy.request({
    method: "DELETE",
    url: `${serviceBaseUrl()}/api/resource/Purchase%20Invoice/${enc}`,
    headers: sessionHeaders(),
    failOnStatusCode: false,
  }).then((res) => { state.lastResponse = res; });
});

// ---------------------------------------------------------------------------
// Submit
// ---------------------------------------------------------------------------

When("I POST submit_purchase_invoice for the stored invoice name", () => {
  cy.request({
    method: "POST",
    url: `${serviceBaseUrl()}/api/method/frappe.client.submit`,
    headers: sessionHeaders(),
    body: { name: state.storedInvoiceName },
    failOnStatusCode: false,
  }).then((res) => {
    state.lastResponse = res;
    if (res.status !== 200) {
      cy.log(`submit failed: HTTP ${res.status} body=${JSON.stringify(res.body)}`);
    }
  });
});

When("I POST submit_purchase_invoice with JSON body:", (docString) => {
  const body = JSON.parse(docString.trim());
  cy.request({
    method: "POST",
    url: `${serviceBaseUrl()}/api/method/frappe.client.submit`,
    headers: sessionHeaders(),
    body,
    failOnStatusCode: false,
  }).then((res) => { state.lastResponse = res; });
});

// ---------------------------------------------------------------------------
// Dashboard
// ---------------------------------------------------------------------------

When("I GET the dashboard summary", () => {
  cy.request({
    method: "GET",
    url: `${serviceBaseUrl()}/api/method/expense_tracker.api.get_dashboard_summary`,
    headers: sessionHeaders(),
    failOnStatusCode: false,
  }).then((res) => { state.lastResponse = res; });
});

When("I GET the dashboard summary with query {string}", (queryString) => {
  const q = (queryString || "").trim();
  const base = `${serviceBaseUrl()}/api/method/expense_tracker.api.get_dashboard_summary`;
  const url = q ? `${base}?${q}` : base;
  cy.request({
    method: "GET",
    url,
    headers: sessionHeaders(),
    failOnStatusCode: false,
  }).then((res) => { state.lastResponse = res; });
});

// ---------------------------------------------------------------------------
// Financial dashboard (income vs expense, date-wise)
// ---------------------------------------------------------------------------

When("I GET the financial dashboard", () => {
  cy.request({
    method: "GET",
    url: `${serviceBaseUrl()}/api/method/expense_tracker.api.get_financial_dashboard`,
    headers: sessionHeaders(),
    failOnStatusCode: false,
  }).then((res) => { state.lastResponse = res; });
});

When("I GET the financial dashboard with query {string}", (queryString) => {
  const q = (queryString || "").trim();
  const base = `${serviceBaseUrl()}/api/method/expense_tracker.api.get_financial_dashboard`;
  const url = q ? `${base}?${q}` : base;
  cy.request({
    method: "GET",
    url,
    headers: sessionHeaders(),
    failOnStatusCode: false,
  }).then((res) => { state.lastResponse = res; });
});

// ---------------------------------------------------------------------------
// Assertions
// ---------------------------------------------------------------------------

Then("the expense API last response status should be {int}", (code) => {
  const expected = parseInt(code, 10);
  expect(
    state.lastResponse.status,
    `expected HTTP ${expected}, body=${JSON.stringify(state.lastResponse.body)}`,
  ).to.eq(expected);
});

Then("the expense API last response status should be one of {string}", (csv) => {
  const allowed = String(csv)
    .split(",")
    .map((v) => parseInt(v.trim(), 10))
    .filter((n) => Number.isInteger(n));
  expect(allowed.length, "at least one valid status code must be provided").to.be.greaterThan(0);
  expect(
    state.lastResponse.status,
    `expected one of [${allowed.join(", ")}], body=${JSON.stringify(state.lastResponse.body)}`,
  ).to.be.oneOf(allowed);
});

Then("I store the created Purchase Invoice name from the response", () => {
  const payload = responsePayload();
  const name = payload?.name;
  expect(name, "POST Purchase Invoice should return name").to.be.a("string").and.not.be.empty;
  state.storedInvoiceName = name;
  cy.log(`Stored invoice name: ${name}`);
});

Then("the stored invoice should have docstatus {int}", (ds) => {
  expect(responsePayload().docstatus).to.eq(parseInt(ds, 10));
});

Then("the stored invoice should have workflow status {string}", (st) => {
  expect(responsePayload().status).to.eq(st);
});

Then("the submit response should show success and status Submitted", () => {
  const payload = responsePayload();
  expect(payload.success).to.be.true;
  expect(payload.docstatus).to.eq(1);
});

Then("the delete response should show success", () => {
  expect(responsePayload().success).to.be.true;
});

Then("the response body should have field {string}", (field) => {
  expect(responsePayload()).to.have.property(field);
});

Then("the response body field {string} should not be empty", (field) => {
  const val = responsePayload()[field];
  expect(val, `${field} should not be empty`).to.exist.and.not.eq("");
});

Then("the response body field {string} should equal {string}", (field, expected) => {
  expect(String(responsePayload()[field])).to.eq(expected);
});

Then("the response body field {string} should be {int}", (field, expected) => {
  expect(responsePayload()[field]).to.eq(parseInt(expected, 10));
});

Then("the dashboard response should have a total_spend field", () => {
  const payload = responsePayload();
  expect(payload).to.have.property("total_spend");
  expect(payload.total_spend).to.be.a("number");
});

Then("the dashboard response should include keys:", (dataTable) => {
  const payload = responsePayload();
  const rows = dataTable.raw().flat().map((v) => String(v).trim()).filter(Boolean);
  for (const key of rows) {
    expect(payload, `dashboard payload should include '${key}'`).to.have.property(key);
  }
});

Then("the dashboard response should not include keys:", (dataTable) => {
  const payload = responsePayload();
  const rows = dataTable.raw().flat().map((v) => String(v).trim()).filter(Boolean);
  for (const key of rows) {
    expect(payload, `dashboard payload should not include '${key}'`).to.not.have.property(key);
  }
});

Then("the dashboard preset should be {string}", (preset) => {
  expect(responsePayload().preset).to.eq(preset);
});

Then("the dashboard cashflow should have bucket count {int}", (n) => {
  const expected = parseInt(n, 10);
  const payload = responsePayload();
  expect(payload).to.have.property("cashflow");
  expect(payload.cashflow).to.be.an("array").with.length(expected);
});

Then("each dashboard breakdown row should include pct and color", () => {
  const rows = responsePayload().breakdown || [];
  for (const row of rows) {
    expect(row).to.include.keys("item_group", "total", "pct", "color");
  }
});

Then("the financial dashboard response should expose analytics fields", () => {
  const b = responsePayload();
  expect(b, `body=${JSON.stringify(b)}`).to.be.an("object");
  expect(b).to.have.property("daily");
  expect(b.daily).to.be.an("array");
  expect(b).to.have.property("totals");
  expect(b.totals).to.include.keys("income", "expense", "net");
  expect(b).to.have.property("recent_activity");
  expect(b.recent_activity).to.be.an("array");
  expect(b).to.have.property("preset");
  expect(b).to.have.property("from_date");
  expect(b).to.have.property("to_date");
  expect(b).to.have.property("resource_api");
  expect(b).to.have.property("currency");
  expect(b).to.have.property("company");
});

Then("each daily row should include income expense and net", () => {
  const rows = responsePayload().daily || [];
  expect(
    rows.length,
    "daily should include at least one day for the selected period",
  ).to.be.at.least(1);
  for (const row of rows) {
    expect(row).to.include.keys("date", "income", "expense", "net");
  }
});

Then("the financial dashboard preset should be {string}", (preset) => {
  expect(responsePayload().preset).to.eq(preset);
});

Then("the financial dashboard daily length should be {int}", (n) => {
  const expected = parseInt(n, 10);
  expect(responsePayload().daily.length).to.eq(expected);
});

Then("the financial dashboard recent activity should have resource paths when non-empty", () => {
  const items = responsePayload().recent_activity || [];
  for (const row of items) {
    expect(row).to.have.property("resource_path");
    expect(String(row.resource_path)).to.match(/^\/api\/resource\//);
  }
});
