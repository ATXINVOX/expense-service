/// <reference types="cypress" />
import { Given, When, Then } from "@badeball/cypress-cucumber-preprocessor";

/** Shared state between steps in a scenario */
const state = {
  lastResponse: null,
  storedInvoiceName: null,
};

function serviceBaseUrl() {
  return Cypress.env("EXPENSE_SERVICE_URL") || "http://localhost:9004";
}

function sessionHeaders() {
  const sid = Cypress.env("EXPENSE_TEST_SID");
  return {
    "Content-Type": "application/json",
    Accept: "application/json",
    "X-Requested-With": "XMLHttpRequest",
    ...(sid ? { Cookie: `sid=${sid}`, "X-Frappe-SID": sid } : {}),
  };
}

Given("the expense API test session is configured", () => {
  const sid = Cypress.env("EXPENSE_TEST_SID");
  expect(
    sid,
    "Set EXPENSE_TEST_SID (e.g. npm run cy:api -- --env EXPENSE_TEST_SID=your_sid)"
  )
    .to.be.a("string")
    .and.not.be.empty;
});

When("I POST a new Purchase Invoice for expense draft submit with:", (dataTable) => {
  const body = {};
  const company =
    Cypress.env("EXPENSE_TEST_COMPANY") || "Acme Pty Ltd";
  for (const row of dataTable.hashes()) {
    const key = row.field.trim();
    const val = row.value.trim();
    if (key === "items_json") {
      body.items = JSON.parse(val);
    } else {
      body[key] = val;
    }
  }
  if (!body.company) {
    body.company = company;
  }

  cy.request({
    method: "POST",
    url: `${serviceBaseUrl()}/api/resource/Purchase%20Invoice`,
    headers: sessionHeaders(),
    body,
    failOnStatusCode: false,
  }).then((res) => {
    state.lastResponse = res;
  });
});

Then("the expense API last response status should be {int}", (code) => {
  expect(state.lastResponse.status).to.eq(parseInt(code, 10));
});

Then("I store the created Purchase Invoice name from the response", () => {
  const name = state.lastResponse.body?.name;
  expect(name, "POST Purchase Invoice should return name").to.be.a("string").and.not.be
    .empty;
  state.storedInvoiceName = name;
});

When("I GET the stored Purchase Invoice", () => {
  const enc = encodeURIComponent(state.storedInvoiceName);
  cy.request({
    method: "GET",
    url: `${serviceBaseUrl()}/api/resource/Purchase%20Invoice/${enc}`,
    headers: sessionHeaders(),
    failOnStatusCode: false,
  }).then((res) => {
    state.lastResponse = res;
  });
});

Then("the stored invoice should have docstatus {int}", (ds) => {
  const b = state.lastResponse.body;
  expect(b.docstatus).to.eq(parseInt(ds, 10));
});

Then("the stored invoice should have workflow status {string}", (st) => {
  expect(state.lastResponse.body.status).to.eq(st);
});

When("I POST submit_purchase_invoice for the stored invoice name", () => {
  cy.request({
    method: "POST",
    url: `${serviceBaseUrl()}/api/method/expense_tracker.api.submit_purchase_invoice`,
    headers: sessionHeaders(),
    body: { name: state.storedInvoiceName },
    failOnStatusCode: false,
  }).then((res) => {
    state.lastResponse = res;
  });
});

When("I POST submit_purchase_invoice with JSON body:", (docString) => {
  const body = JSON.parse(docString.trim());
  cy.request({
    method: "POST",
    url: `${serviceBaseUrl()}/api/method/expense_tracker.api.submit_purchase_invoice`,
    headers: sessionHeaders(),
    body,
    failOnStatusCode: false,
  }).then((res) => {
    state.lastResponse = res;
  });
});

Then("the submit response should show success and status Submitted", () => {
  expect(state.lastResponse.body.success).to.be.true;
  expect(state.lastResponse.body.status).to.eq("Submitted");
});
