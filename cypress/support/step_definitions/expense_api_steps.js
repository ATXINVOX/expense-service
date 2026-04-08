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

// ---------------------------------------------------------------------------
// Auth: login via Frappe session API and export SID
// ---------------------------------------------------------------------------

Given("I login to the expense service as {string}", (user) => {
  const pwd = Cypress.env("ADMIN_PASSWORD") || "admin";
  _loginAsFrappeUser(user, pwd);
});

Given("I login to the expense service as {string} with password {string}", (user, pwd) => {
  _loginAsFrappeUser(user, pwd);
});

function _loginAsFrappeUser(user, pwd) {
  cy.request({
    method: "POST",
    url: `${frappeBaseUrl()}/api/method/login`,
    headers: {
      "Content-Type": "application/x-www-form-urlencoded",
      Accept: "application/json",
    },
    body: `usr=${encodeURIComponent(user)}&pwd=${encodeURIComponent(pwd)}`,
    failOnStatusCode: false,
  }).then((res) => {
    // Frappe v13/v14 returns { sid } in the JSON body.
    // Frappe v15/v16 sets the SID only as a Set-Cookie header.
    // Try body first, then fall back to the cookie jar that Cypress maintains.
    const bodySid = res.body?.sid;
    if (bodySid && bodySid !== "Guest") {
      state.sid = bodySid;
      cy.log(`Logged in as ${user} — SID from body`);
      return;
    }

    // Extract from Set-Cookie header
    const setCookie = [].concat(res.headers?.["set-cookie"] ?? []).join(";");
    const cookieMatch = setCookie.match(/\bsid=([^;,\s]+)/i);
    const cookieSid = cookieMatch?.[1];

    if (cookieSid && cookieSid !== "Guest") {
      state.sid = cookieSid;
      cy.log(`Logged in as ${user} — SID from Set-Cookie`);
      return;
    }

    // Last resort: ask Cypress for the persisted cookie
    cy.getCookie("sid").then((cookie) => {
      const sid = cookie?.value;
      expect(sid, `Login as '${user}' must return a non-guest SID`).to.be.a("string").and.not.eq("Guest");
      state.sid = sid;
      cy.log(`Logged in as ${user} — SID from Cypress cookie jar`);
    });
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

  cy.request({
    method: "POST",
    url: `${serviceBaseUrl()}/api/resource/Purchase%20Invoice`,
    headers: sessionHeaders(),
    body,
    failOnStatusCode: false,
  }).then((res) => { state.lastResponse = res; });
});

When("I POST a new Purchase Invoice with body:", (docString) => {
  const body = JSON.parse(docString.trim());
  const company = Cypress.env("EXPENSE_TEST_COMPANY") || "Acme Pty Ltd";
  if (!body.company) body.company = company;

  cy.request({
    method: "POST",
    url: `${serviceBaseUrl()}/api/resource/Purchase%20Invoice`,
    headers: sessionHeaders(),
    body,
    failOnStatusCode: false,
  }).then((res) => { state.lastResponse = res; });
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
  }).then((res) => { state.lastResponse = res; });
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

// ---------------------------------------------------------------------------
// Assertions
// ---------------------------------------------------------------------------

Then("the expense API last response status should be {int}", (code) => {
  expect(state.lastResponse.status).to.eq(parseInt(code, 10));
});

Then("I store the created Purchase Invoice name from the response", () => {
  const name = state.lastResponse.body?.name;
  expect(name, "POST Purchase Invoice should return name").to.be.a("string").and.not.be.empty;
  state.storedInvoiceName = name;
  cy.log(`Stored invoice name: ${name}`);
});

Then("the stored invoice should have docstatus {int}", (ds) => {
  expect(state.lastResponse.body.docstatus).to.eq(parseInt(ds, 10));
});

Then("the stored invoice should have workflow status {string}", (st) => {
  expect(state.lastResponse.body.status).to.eq(st);
});

Then("the submit response should show success and status Submitted", () => {
  expect(state.lastResponse.body.success).to.be.true;
  expect(state.lastResponse.body.status).to.eq("Submitted");
});

Then("the delete response should show success", () => {
  expect(state.lastResponse.body.success).to.be.true;
});

Then("the response body should have field {string}", (field) => {
  expect(state.lastResponse.body).to.have.property(field);
});

Then("the response body field {string} should not be empty", (field) => {
  const val = state.lastResponse.body[field];
  expect(val, `${field} should not be empty`).to.exist.and.not.eq("");
});

Then("the response body field {string} should equal {string}", (field, expected) => {
  expect(String(state.lastResponse.body[field])).to.eq(expected);
});

Then("the response body field {string} should be {int}", (field, expected) => {
  expect(state.lastResponse.body[field]).to.eq(parseInt(expected, 10));
});

Then("the dashboard response should have a total_spend field", () => {
  expect(state.lastResponse.body).to.have.property("total_spend");
  expect(state.lastResponse.body.total_spend).to.be.a("number");
});
