/// <reference types="cypress" />
import { Given, When, Then } from "@badeball/cypress-cucumber-preprocessor";

/** Shared state between steps in a scenario */
const state = {
  lastResponse: null,
  storedInvoiceName: null,
  sid: null,
  csrfToken: null,
  authToken: null,
  authMode: "none", // none | sid | bearer
  dashboardPresetOverride: null,
  loggedInUser: null,
  userTenantId: null,
};

function serviceBaseUrl() {
  return Cypress.env("EXPENSE_SERVICE_URL") || "http://localhost:9004";
}

function normalizeGatewayUrl(urlString) {
  try {
    const parsed = new URL(String(urlString).trim());
    const host = (parsed.hostname || "").toLowerCase();
    // invoice_tracker routes must go through Kong; central-site host points to wrong app.
    if (host.includes("central-site")) {
      return `${parsed.protocol}//kong:8000`;
    }
    return `${parsed.protocol}//${parsed.host}`;
  } catch (_e) {
    return String(urlString).trim();
  }
}

function defaultGatewayBaseUrl() {
  const explicit =
    Cypress.env("API_BASE_URL") || Cypress.env("EXPENSE_GATEWAY_URL") || Cypress.env("EXPENSE_FRAPPE_URL");
  if (explicit && String(explicit).trim()) {
    return normalizeGatewayUrl(explicit);
  }

  // CI often runs Cypress inside a container where localhost:8000 is not reachable.
  // Derive Kong host from Cypress baseUrl and force port 8000.
  const baseUrl = Cypress.config("baseUrl");
  if (baseUrl) {
    try {
      const parsed = new URL(baseUrl);
      return normalizeGatewayUrl(`${parsed.protocol}//${parsed.hostname}:8000`);
    } catch (_e) {
      // fall through to local default
    }
  }

  return "http://localhost:8000";
}

function frappeBaseUrl() {
  return defaultGatewayBaseUrl();
}

function gatewayBaseUrl() {
  return defaultGatewayBaseUrl();
}

function sessionHeaders() {
  return {
    "Content-Type": "application/json",
    Accept: "application/json",
    "X-Requested-With": "XMLHttpRequest",
    ...(state.sid ? { Cookie: `sid=${state.sid}`, "X-Frappe-SID": state.sid } : {}),
  };
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

function dashboardPreset() {
  const raw = String(state.dashboardPresetOverride || Cypress.env("DASHBOARD_PRESET") || "last_month").trim();
  const allowed = ["last_week", "last_month", "last_year"];
  return allowed.includes(raw) ? raw : "last_month";
}

function formatYmdLocal(date) {
  const y = date.getFullYear();
  const m = String(date.getMonth() + 1).padStart(2, "0");
  const d = String(date.getDate()).padStart(2, "0");
  return `${y}-${m}-${d}`;
}

function addDaysYmd(ymd, days) {
  const [y, m, d] = String(ymd)
    .split("-")
    .map((v) => parseInt(v, 10));
  const dt = new Date(y, m - 1, d);
  dt.setDate(dt.getDate() + days);
  return formatYmdLocal(dt);
}

function assertDueDateOnOrAfterPostingDate(postingDate, dueDate, context = "invoice payload") {
  const postingTs = parseYmd(postingDate).getTime();
  const dueTs = parseYmd(dueDate).getTime();
  expect(
    Number.isFinite(postingTs) && Number.isFinite(dueTs) && dueTs >= postingTs,
    `${context}: due_date ${dueDate} must be same as or after posting_date ${postingDate}`,
  ).to.eq(true);
}

function assertPostingDateInsideDashboardWindow(postingDate, fromDate, toDate, context = "dashboard window") {
  const postingTs = parseYmd(postingDate).getTime();
  const fromTs = parseYmd(fromDate).getTime();
  const toTs = parseYmd(toDate).getTime();
  const inRange =
    Number.isFinite(postingTs) &&
    Number.isFinite(fromTs) &&
    Number.isFinite(toTs) &&
    postingTs >= fromTs &&
    postingTs <= toTs;
  if (!inRange) {
    throw new Error(
      `${context}: invoice posting_date is outside dashboard window: ${JSON.stringify({
        postingDate,
        dashboardFromDate: fromDate,
        dashboardToDate: toDate,
      })}`,
    );
  }
}

function isPostingDateInsideWindow(postingDate, fromDate, toDate) {
  const postingTs = parseYmd(postingDate).getTime();
  const fromTs = parseYmd(fromDate).getTime();
  const toTs = parseYmd(toDate).getTime();
  return (
    Number.isFinite(postingTs) &&
    Number.isFinite(fromTs) &&
    Number.isFinite(toTs) &&
    postingTs >= fromTs &&
    postingTs <= toTs
  );
}

function alignDashboardWindowToPostingDate(base, token, postingDate) {
  const presets = [dashboardPreset(), "last_week", "last_month", "last_year"].filter(
    (p, idx, arr) => arr.indexOf(p) === idx,
  );

  const tryAt = (idx) => {
    if (idx >= presets.length) return cy.wrap(null);
    const preset = presets[idx];
    return cy
      .request({
        method: "GET",
        url: `${base}/api/method/invoice_tracker.api.get_invoice_dashboard?preset=${encodeURIComponent(
          preset,
        )}&recent_limit=1`,
        headers: apiAuthHeaders(token),
        failOnStatusCode: false,
      })
      .then((res) => {
        if (res.status !== 200) return tryAt(idx + 1);
        const fromDate = String(res.body?.from_date || "");
        const toDate = String(res.body?.to_date || "");
        if (!fromDate || !toDate) return tryAt(idx + 1);
        if (!isPostingDateInsideWindow(postingDate, fromDate, toDate)) return tryAt(idx + 1);

        state.dashboardPresetOverride = preset;
        state.dashboardFromDate = fromDate;
        state.dashboardToDate = toDate;
        return preset;
      });
  };

  return tryAt(0);
}

function parseYmd(ymd) {
  const [y, m, d] = String(ymd)
    .split("-")
    .map((v) => parseInt(v, 10));
  return new Date(y, (m || 1) - 1, d || 1);
}

function dateInsideDashboardPreset() {
  const explicit = Cypress.env("DASHBOARD_TEST_POSTING_DATE");
  if (explicit && String(explicit).trim()) {
    return String(explicit).trim();
  }

  const now = new Date();
  const preset = String(dashboardPreset()).trim();

  if (preset === "last_month") {
    // Pick a stable date inside the dashboard window to ensure aggregation includes this invoice.
    return formatYmdLocal(new Date(now.getFullYear(), now.getMonth() - 1, 15));
  }
  if (preset === "last_week") {
    // Keep the synthetic invoice date inside backend "last_week" range.
    // Using now-7 days is consistently within the prior week window.
    return addDaysYmd(formatYmdLocal(now), -7);
  }
  return formatYmdLocal(now);
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

function bearerHeaders(token) {
  return {
    Accept: "application/json",
    "Content-Type": "application/json",
    Authorization: `Bearer ${token}`,
  };
}

function dashboardApiBaseUrl() {
  return defaultGatewayBaseUrl();
}

function authTokenUrl() {
  return Cypress.env("AUTH_TOKEN_URL") || "";
}

function requireEnv(name, fallback) {
  const v = Cypress.env(name);
  if (v != null && String(v).trim() !== "") return String(v).trim();
  if (fallback != null) return fallback;
  throw new Error(`Missing required env: ${name}`);
}

function buildResourceUrl(base, doctype) {
  return `${base}/api/resource/${encodeURIComponent(doctype)}`;
}

function getDataRows(body) {
  if (Array.isArray(body?.data)) return body.data;
  if (Array.isArray(body?.message)) return body.message;
  return [];
}

function getDocName(body) {
  return body?.data?.name || body?.name || body?.message?.name || "";
}

function readSalesInvoiceDoc(base, token, name, contextLabel = "Sales Invoice read") {
  const resourceUrl = `${buildResourceUrl(base, "Sales Invoice")}/${encodeURIComponent(name)}`;
  return cy
    .request({
      method: "GET",
      url: resourceUrl,
      headers: apiAuthHeaders(token),
      failOnStatusCode: false,
    })
    .then((res) => {
      if (res.status === 200) {
        return res.body?.data || res.body;
      }

      // Some roles can create/submit through method APIs but cannot read via /api/resource/<doctype>/<name>.
      if (![403, 404].includes(res.status)) {
        expect(
          res.status,
          `${contextLabel} failed. status=${res.status} body=${JSON.stringify(res.body)}`,
        ).to.eq(200);
      }

      return cy
        .request({
          method: "POST",
          url: `${base}/api/method/frappe.client.get`,
          headers: apiAuthHeaders(token),
          body: {
            doctype: "Sales Invoice",
            name,
          },
          failOnStatusCode: false,
        })
        .then((fallbackRes) => {
          expect(
            fallbackRes.status,
            `${contextLabel} fallback failed. resource status=${res.status} resource body=${JSON.stringify(
              res.body,
            )}; fallback status=${fallbackRes.status} fallback body=${JSON.stringify(fallbackRes.body)}`,
          ).to.eq(200);
          return fallbackRes.body?.message || fallbackRes.body?.data || fallbackRes.body;
        });
    });
}

function extractSidFromLoginResponse(res) {
  const bodySid = res.body?.sid || res.body?.message?.sid;
  if (bodySid && bodySid !== "Guest") return bodySid;
  const setCookie = [].concat(res.headers?.["set-cookie"] ?? []);
  for (const line of setCookie) {
    const m = String(line).match(/\bsid=([^;,\s]+)/i);
    if (m?.[1] && m[1] !== "Guest") return decodeURIComponent(m[1]);
  }
  return null;
}

function extractCsrfFromResponseBody(body) {
  return body?.csrf_token || body?.message?.csrf_token || null;
}

function trySessionLogin(apiBase, existingSidCookie, user, password) {
  return cy
    .request({
      method: "POST",
      url: `${apiBase}/api/method/login`,
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        Cookie: `sid=${existingSidCookie}`,
      },
      body: { email: user, password },
      failOnStatusCode: false,
    })
    .then((resEmailPwd) => {
      let sid = extractSidFromLoginResponse(resEmailPwd);
      let csrfToken = extractCsrfFromResponseBody(resEmailPwd.body);
      if (sid) {
        return {
          sid,
          csrfToken,
          emailPwdStatus: resEmailPwd.status,
          usrPwdStatus: null,
          emailPwdBody: resEmailPwd.body,
          usrPwdBody: null,
        };
      }

      return cy
        .request({
          method: "POST",
          url: `${apiBase}/api/method/login`,
          headers: {
            Accept: "application/json",
            "Content-Type": "application/json",
            Cookie: `sid=${existingSidCookie}`,
          },
          body: { usr: user, pwd: password },
          failOnStatusCode: false,
        })
        .then((resUsrPwd) => {
          sid = extractSidFromLoginResponse(resUsrPwd);
          csrfToken = csrfToken || extractCsrfFromResponseBody(resUsrPwd.body);
          return {
            sid: sid || null,
            csrfToken: csrfToken || null,
            emailPwdStatus: resEmailPwd.status,
            usrPwdStatus: resUsrPwd.status,
            emailPwdBody: resEmailPwd.body,
            usrPwdBody: resUsrPwd.body,
          };
        });
    });
}

function sidHeaders(sid) {
  const h = {
    Accept: "application/json",
    "Content-Type": "application/json",
    "X-Requested-With": "XMLHttpRequest",
    "X-Frappe-SID": sid,
    Cookie: `sid=${sid}`,
  };
  if (state.csrfToken) {
    h["X-Frappe-CSRF-Token"] = state.csrfToken;
  }
  return h;
}

function apiAuthHeaders(token = state.authToken) {
  if (state.authMode === "sid") return sidHeaders(token);
  return bearerHeaders(token);
}

function resolveCurrentUserTenantContext(apiBase) {
  const token = state.authToken;
  if (!token || state.authMode === "none") return cy.wrap(null);

  return cy
    .request({
      method: "GET",
      url: `${apiBase}/api/method/frappe.auth.get_logged_user`,
      headers: apiAuthHeaders(token),
      failOnStatusCode: false,
    })
    .then((userRes) => {
      if (userRes.status !== 200) return;
      const user = userRes.body?.message || userRes.body?.user || null;
      if (!user) return;
      state.loggedInUser = String(user);

      return cy
        .request({
          method: "POST",
          url: `${apiBase}/api/method/frappe.client.get_value`,
          headers: apiAuthHeaders(token),
          body: {
            doctype: "User",
            filters: { name: state.loggedInUser },
            fieldname: ["tenant_id"],
          },
          failOnStatusCode: false,
        })
        .then((tenantRes) => {
          if (tenantRes.status !== 200) return;
          const msg = tenantRes.body?.message || tenantRes.body?.data || {};
          const tenantId =
            msg?.tenant_id ||
            (typeof msg === "string" ? msg : null) ||
            Cypress.env("TEST_TENANT_ID") ||
            null;
          if (tenantId) state.userTenantId = String(tenantId);
        });
    });
}

function ensureSalesInvoiceTenantId(base, token, salesInvoiceName) {
  const tenantId = state.userTenantId || Cypress.env("TEST_TENANT_ID");
  if (!tenantId || !salesInvoiceName) return cy.wrap(null);
  return cy
    .request({
      method: "POST",
      url: `${base}/api/method/frappe.client.set_value`,
      headers: apiAuthHeaders(token),
      body: {
        doctype: "Sales Invoice",
        name: salesInvoiceName,
        fieldname: "tenant_id",
        value: String(tenantId),
      },
      failOnStatusCode: false,
    })
    .then((res) => {
      expect(
        res.status,
        `Unable to set tenant_id on Sales Invoice ${salesInvoiceName}. status=${res.status} body=${JSON.stringify(
          res.body,
        )}`,
      ).to.eq(200);

      return cy.request({
        method: "POST",
        url: `${base}/api/method/frappe.client.get_value`,
        headers: apiAuthHeaders(token),
        body: {
          doctype: "Sales Invoice",
          filters: { name: salesInvoiceName },
          fieldname: ["tenant_id"],
        },
        failOnStatusCode: false,
      });
    })
    .then((verifyRes) => {
      expect(
        verifyRes.status,
        `Unable to verify tenant_id on Sales Invoice ${salesInvoiceName}. status=${verifyRes.status} body=${JSON.stringify(
          verifyRes.body,
        )}`,
      ).to.eq(200);
      const got = verifyRes.body?.message?.tenant_id || verifyRes.body?.data?.tenant_id || null;
      expect(
        got,
        `Sales Invoice tenant_id mismatch for ${salesInvoiceName}. expected=${tenantId} actual=${got} body=${JSON.stringify(
          verifyRes.body,
        )}`,
      ).to.eq(String(tenantId));
    });
}

function ensureAdministratorTenantContext() {
  const apiBase = dashboardApiBaseUrl();
  const token = state.authToken;
  if (!token || state.authMode !== "sid") return cy.wrap(null);

  const headers = apiAuthHeaders(token);
  return cy
    .request({
      method: "GET",
      url: `${apiBase}/api/method/invoice_tracker.api.get_invoice_dashboard?preset=last_month&recent_limit=1`,
      headers,
      failOnStatusCode: false,
    })
    .then((probeRes) => {
      if (probeRes.status === 200) return;
      const msg = String(probeRes.body?.message || "");
      const tenantMissing =
        probeRes.status === 400 && /No tenant_id found in context/i.test(msg);
      if (!tenantMissing) return;

      const bootstrapTenantId = Cypress.env("TEST_TENANT_ID") || "expense-bdd-tenant-001";
      return cy
        .request({
          method: "POST",
          url: `${apiBase}/api/method/frappe.client.set_value`,
          headers,
          body: {
            doctype: "User",
            name: "Administrator",
            fieldname: "tenant_id",
            value: bootstrapTenantId,
          },
          failOnStatusCode: false,
        })
        .then((setRes) => {
          expect(
            setRes.status,
            `Failed to set Administrator tenant_id. status=${setRes.status} body=${JSON.stringify(setRes.body)}`,
          ).to.eq(200);

          return cy.request({
            method: "GET",
            url: `${apiBase}/api/method/invoice_tracker.api.get_invoice_dashboard?preset=last_month&recent_limit=1`,
            headers,
            failOnStatusCode: false,
          });
        })
        .then((verifyRes) => {
          expect(
            verifyRes.status,
            `Administrator tenant bootstrap did not take effect. status=${verifyRes.status} body=${JSON.stringify(
              verifyRes.body,
            )}`,
          ).to.eq(200);
        });
    });
}

Given("I am an authenticated user", () => {
  const apiBase = dashboardApiBaseUrl();
  const tokenUrl = authTokenUrl();
  const loginUser =
    Cypress.env("TEST_USERNAME") || Cypress.env("TEST_USER_EMAIL") || "thiruvarasu.u@datasirpi.com";
  const loginPassword =
    Cypress.env("TEST_PASSWORD") || Cypress.env("TEST_USER_PASSWORD") || "Str0ng!Pass#2026";
  expect(apiBase, "API_BASE_URL").to.be.a("string").and.not.be.empty;

  if (tokenUrl) {
    const payload = new URLSearchParams({
      grant_type: "password",
      client_id: requireEnv("AUTH_CLIENT_ID"),
      client_secret: requireEnv("AUTH_CLIENT_SECRET"),
      username: loginUser,
      password: loginPassword,
    }).toString();

    cy.request({
      method: "POST",
      url: tokenUrl,
      headers: {
        Accept: "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
      },
      body: payload,
      failOnStatusCode: false,
    }).then((res) => {
      expect(
        res.status,
        `Token generation failed. status=${res.status} body=${JSON.stringify(res.body)}`,
      ).to.eq(200);
      const token = res.body?.access_token || res.body?.token;
      expect(token, `No access_token in response: ${JSON.stringify(res.body)}`).to.be.a("string").and.not.be.empty;
      state.authToken = token;
      state.authMode = "bearer";
    });
    return;
  }

  // Default requested by user: Kong login at localhost:8000 with email/password JSON.
  const email = loginUser;
  const password = loginPassword;
  const fallbackUser = Cypress.env("FALLBACK_TEST_USER") || "Administrator";
  const fallbackPassword = Cypress.env("FALLBACK_TEST_PASSWORD") || "admin";
  const existingSidCookie =
    Cypress.env("LOGIN_SID_COOKIE") || "a4c5f7a282266fbf56a37a31a63c364c07e5cedcc1b1e5e60f8a5768";

  cy.then(() =>
    trySessionLogin(apiBase, existingSidCookie, email, password).then((primaryLogin) => {
      if (primaryLogin.sid) {
        state.authToken = primaryLogin.sid;
        state.authMode = "sid";
        state.sid = primaryLogin.sid;
        state.csrfToken = primaryLogin.csrfToken;
        state.loggedInUser = email;
        return;
      }

      if (String(email).trim().toLowerCase() === String(fallbackUser).trim().toLowerCase()) {
        expect(
          primaryLogin.sid,
          `Login failed for ${email}. email/pwd status=${primaryLogin.emailPwdStatus}, usr/pwd status=${primaryLogin.usrPwdStatus}, body=${JSON.stringify(
            primaryLogin.usrPwdBody || primaryLogin.emailPwdBody,
          )}`,
        ).to.be.a("string").and.not.be.empty;
        return;
      }

      return trySessionLogin(apiBase, existingSidCookie, fallbackUser, fallbackPassword).then(
        (fallbackLogin) => {
          expect(
            fallbackLogin.sid,
            `Login failed for primary user ${email} and fallback user ${fallbackUser}. primary email/pwd=${primaryLogin.emailPwdStatus} usr/pwd=${primaryLogin.usrPwdStatus}; fallback email/pwd=${fallbackLogin.emailPwdStatus} usr/pwd=${fallbackLogin.usrPwdStatus}; fallback body=${JSON.stringify(
              fallbackLogin.usrPwdBody || fallbackLogin.emailPwdBody,
            )}`,
          ).to.be.a("string").and.not.be.empty;
          state.authToken = fallbackLogin.sid;
          state.authMode = "sid";
          state.sid = fallbackLogin.sid;
          state.csrfToken = fallbackLogin.csrfToken;
          state.loggedInUser = fallbackUser;
        },
      );
    }),
  );

  // Some benches expose csrf_token via get_logged_user only.
  cy.then(() => {
    if (state.authMode !== "sid" || !state.sid || state.csrfToken) return;
    cy.request({
      method: "GET",
      url: `${apiBase}/api/method/frappe.auth.get_logged_user`,
      headers: sidHeaders(state.sid),
      failOnStatusCode: false,
    }).then((sessRes) => {
      if (sessRes.status === 200) {
        state.csrfToken = extractCsrfFromResponseBody(sessRes.body) || state.csrfToken;
      }
    });
  });

  cy.then(() => {
    if (String(state.loggedInUser || "").trim().toLowerCase() !== "administrator") return;
    return ensureAdministratorTenantContext();
  });

  cy.then(() => resolveCurrentUserTenantContext(apiBase));
});

Given("required invoice test data exists", () => {
  const base = dashboardApiBaseUrl();
  const token = state.authToken;
  expect(token, "auth token must be set by auth step").to.be.a("string").and.not.be.empty;

  const uniqueId = Date.now();
  state.invoiceAmount = Number(Cypress.env("TEST_INVOICE_AMOUNT") || 4000);
  state.customerName = `BDD_TEST_CUSTOMER_${uniqueId}`;
  state.itemCode = `BDD_TEST_ITEM_${uniqueId}`;

  // Resolve company from env or tenant-first company list.
  const presetCompany = Cypress.env("TEST_COMPANY");
  if (presetCompany && String(presetCompany).trim()) {
    state.companyName = String(presetCompany).trim();
  } else {
    // Prefer the same company context used by invoice dashboard itself.
    cy.request({
      method: "GET",
      url: `${base}/api/method/invoice_tracker.api.get_invoice_dashboard?preset=last_week&recent_limit=1`,
      headers: apiAuthHeaders(token),
      failOnStatusCode: false,
    }).then((dashRes) => {
      const dashCompany = dashRes.body?.company;
      if (dashRes.status === 200 && dashCompany) {
        state.companyName = String(dashCompany).trim();
        return;
      }

      cy.request({
      method: "GET",
      url: `${buildResourceUrl(base, "Company")}?fields=${encodeURIComponent('["name"]')}&limit_page_length=1`,
      headers: apiAuthHeaders(token),
      failOnStatusCode: false,
      }).then((res) => {
        expect(
          res.status,
          `Company lookup failed. dashboard status=${dashRes.status} dashboard body=${JSON.stringify(
            dashRes.body,
          )}; resource status=${res.status} resource body=${JSON.stringify(res.body)}`,
        ).to.eq(200);
        const rows = getDataRows(res.body);
        expect(rows.length, "No Company visible for token tenant").to.be.greaterThan(0);
        state.companyName = rows[0].name;
      });
    });
  }

  cy.then(() => {
    expect(state.companyName, "companyName").to.be.a("string").and.not.be.empty;
  });

  // Create customer through existing API.
  cy.then(() => {
    const customerFields = encodeURIComponent('["name","customer_name"]');
    cy.request({
      method: "POST",
      url: buildResourceUrl(base, "Customer"),
      headers: apiAuthHeaders(token),
      body: {
        customer_name: state.customerName,
        customer_type: "Company",
        customer_group: "All Customer Groups",
        territory: "All Territories",
      },
      failOnStatusCode: false,
    }).then((res) => {
      if ([200, 201].includes(res.status)) {
        state.customerId = getDocName(res.body) || state.customerName;
        expect(state.customerId, "customer id/name").to.be.a("string").and.not.be.empty;
        return;
      }

      // Kong route for /api/resource/Customer may target a service that does not expose this write path.
      // Fallback to central Frappe method via Kong (/api/method/...).
      cy.request({
        method: "POST",
        url: `${base}/api/method/frappe.client.insert`,
        headers: apiAuthHeaders(token),
        body: {
          doc: {
            doctype: "Customer",
            customer_name: state.customerName,
            customer_type: "Company",
            customer_group: "All Customer Groups",
            territory: "All Territories",
          },
        },
        failOnStatusCode: false,
      }).then((fallbackRes) => {
        if ([200, 201].includes(fallbackRes.status)) {
          state.customerId =
            fallbackRes.body?.message?.name || fallbackRes.body?.data?.name || state.customerName;
          expect(state.customerId, "customer id/name").to.be.a("string").and.not.be.empty;
          return;
        }

        // If create permission is missing, continue with an existing accessible Customer.
        cy.request({
          method: "POST",
          url: `${base}/api/method/frappe.client.get_list`,
          headers: apiAuthHeaders(token),
          body: {
            doctype: "Customer",
            fields: ["name", "customer_name"],
            limit_page_length: 1,
            order_by: "modified desc",
          },
          failOnStatusCode: false,
        }).then((listRes) => {
          expect(
            listRes.status,
            `Customer create denied and fallback lookup failed. resource=${res.status} body=${JSON.stringify(
              res.body,
            )}; insert=${fallbackRes.status} body=${JSON.stringify(
              fallbackRes.body,
            )}; list=${listRes.status} body=${JSON.stringify(listRes.body)}`,
          ).to.eq(200);
          const rows = Array.isArray(listRes.body?.message) ? listRes.body.message : getDataRows(listRes.body);
          expect(
            rows.length,
            `No accessible Customer found after create denied. list body=${JSON.stringify(listRes.body)}`,
          ).to.be.greaterThan(0);
          state.customerId = rows[0].name || rows[0].customer_name;
          expect(state.customerId, "customer fallback id").to.be.a("string").and.not.be.empty;
        });
      });
    });
  });

  // Create item through existing API.
  cy.then(() => {
    const itemFields = encodeURIComponent('["name","item_code"]');
    cy.request({
      method: "POST",
      url: buildResourceUrl(base, "Item"),
      headers: apiAuthHeaders(token),
      body: {
        item_code: state.itemCode,
        item_name: state.itemCode,
        item_group: "All Item Groups",
        stock_uom: "Nos",
        is_stock_item: 0,
        is_sales_item: 1,
      },
      failOnStatusCode: false,
    }).then((res) => {
      if ([200, 201].includes(res.status)) {
        state.itemId = getDocName(res.body) || state.itemCode;
        expect(state.itemId, "item id/code").to.be.a("string").and.not.be.empty;
        return;
      }

      // Fallback via standard Frappe insert method through Kong.
      cy.request({
        method: "POST",
        url: `${base}/api/method/frappe.client.insert`,
        headers: apiAuthHeaders(token),
        body: {
          doc: {
            doctype: "Item",
            item_code: state.itemCode,
            item_name: state.itemCode,
            item_group: "All Item Groups",
            stock_uom: "Nos",
            is_stock_item: 0,
            is_sales_item: 1,
          },
        },
        failOnStatusCode: false,
      }).then((fallbackRes) => {
        if ([200, 201].includes(fallbackRes.status)) {
          state.itemId =
            fallbackRes.body?.message?.name || fallbackRes.body?.data?.name || state.itemCode;
          expect(state.itemId, "item id/code").to.be.a("string").and.not.be.empty;
          return;
        }

        // If create permission is missing, continue with an existing accessible Item.
        cy.request({
          method: "POST",
          url: `${base}/api/method/frappe.client.get_list`,
          headers: apiAuthHeaders(token),
          body: {
            doctype: "Item",
            fields: ["name", "item_code"],
            limit_page_length: 1,
            order_by: "modified desc",
          },
          failOnStatusCode: false,
        }).then((listRes) => {
          expect(
            listRes.status,
            `Item create denied and fallback lookup failed. resource=${res.status} body=${JSON.stringify(
              res.body,
            )}; insert=${fallbackRes.status} body=${JSON.stringify(
              fallbackRes.body,
            )}; list=${listRes.status} body=${JSON.stringify(listRes.body)}`,
          ).to.eq(200);
          const rows = Array.isArray(listRes.body?.message) ? listRes.body.message : getDataRows(listRes.body);
          expect(
            rows.length,
            `No accessible Item found after create denied. list body=${JSON.stringify(listRes.body)}`,
          ).to.be.greaterThan(0);
          state.itemId = rows[0].name || rows[0].item_code;
          expect(state.itemId, "item fallback id").to.be.a("string").and.not.be.empty;
        });
      });
    });
  });

  // Resolve mandatory receivable and income accounts through APIs.
  cy.then(() => {
    const company = state.companyName;
    const receivableFilters = encodeURIComponent(
      JSON.stringify([["company", "=", company], ["account_type", "=", "Receivable"], ["is_group", "=", 0]]),
    );
    const incomeFilters = encodeURIComponent(
      JSON.stringify([["company", "=", company], ["root_type", "=", "Income"], ["is_group", "=", 0]]),
    );
    const fields = encodeURIComponent('["name"]');

    cy.request({
      method: "GET",
      url: `${buildResourceUrl(base, "Account")}?filters=${receivableFilters}&fields=${fields}&limit_page_length=1`,
      headers: apiAuthHeaders(token),
      failOnStatusCode: false,
    }).then((res) => {
      expect(
        res.status,
        `Receivable account lookup failed. status=${res.status} body=${JSON.stringify(res.body)}`,
      ).to.eq(200);
      const rows = getDataRows(res.body);
      expect(rows.length, "No Receivable account found").to.be.greaterThan(0);
      state.debitToAccount = rows[0].name;
    });

    cy.request({
      method: "GET",
      url: `${buildResourceUrl(base, "Account")}?filters=${incomeFilters}&fields=${fields}&limit_page_length=1`,
      headers: apiAuthHeaders(token),
      failOnStatusCode: false,
    }).then((res) => {
      expect(
        res.status,
        `Income account lookup failed. status=${res.status} body=${JSON.stringify(res.body)}`,
      ).to.eq(200);
      const rows = getDataRows(res.body);
      expect(rows.length, "No Income account found").to.be.greaterThan(0);
      state.incomeAccount = rows[0].name;
    });
  });
});

Given("a sales invoice is created for the test user", () => {
  const base = dashboardApiBaseUrl();
  const token = state.authToken;
  expect(token, "auth token").to.be.a("string").and.not.be.empty;

  cy.then(() => {
    const createInvoice = (body) =>
      cy
        .request({
          method: "POST",
          url: buildResourceUrl(base, "Sales Invoice"),
          headers: apiAuthHeaders(token),
          body,
          failOnStatusCode: false,
        })
        .then((resourceRes) => {
          if ([200, 201].includes(resourceRes.status)) {
            state.salesInvoiceCreatePath = "resource";
            return resourceRes;
          }
          // Fallback path via Frappe method API (may route differently through Kong).
          return cy
            .request({
              method: "POST",
              url: `${base}/api/method/frappe.client.insert`,
              headers: apiAuthHeaders(token),
              body: {
                doc: {
                  doctype: "Sales Invoice",
                  ...body,
                },
              },
              failOnStatusCode: false,
            })
            .then((methodRes) => {
              state.salesInvoiceCreatePath = "frappe.client.insert";
              state.salesInvoiceCreateFallback = {
                resourceStatus: resourceRes.status,
                resourceBody: resourceRes.body,
              };
              return methodRes;
            });
        });

    const presetForScenario = dashboardPreset();
    cy.request({
      method: "GET",
      url: `${base}/api/method/invoice_tracker.api.get_invoice_dashboard?preset=${encodeURIComponent(
        presetForScenario,
      )}&recent_limit=1`,
      headers: apiAuthHeaders(token),
      failOnStatusCode: false,
    }).then((dashRes) => {
      expect(
        dashRes.status,
        `Initial dashboard read failed for preset ${presetForScenario}. body=${JSON.stringify(dashRes.body)}`,
      ).to.eq(200);
      state.dashboardPresetOverride = presetForScenario;
      state.dashboardFromDate = String(dashRes.body?.from_date || "");
      state.dashboardToDate = String(dashRes.body?.to_date || "");
      expect(
        state.dashboardFromDate,
        `Dashboard from_date missing for preset ${presetForScenario}. body=${JSON.stringify(dashRes.body)}`,
      ).to.be.a("string").and.not.be.empty;
      expect(
        state.dashboardToDate,
        `Dashboard to_date missing for preset ${presetForScenario}. body=${JSON.stringify(dashRes.body)}`,
      ).to.be.a("string").and.not.be.empty;

      const postingDate = state.dashboardToDate;
      state.invoicePostingDate = postingDate;
      assertPostingDateInsideDashboardWindow(
        postingDate,
        state.dashboardFromDate,
        state.dashboardToDate,
        "Sales Invoice create preflight",
      );

      const dueDate = addDaysYmd(postingDate, 7);
      state.invoiceDueDate = dueDate;
      const createBody = {
        customer: state.customerId || state.customerName,
        company: state.companyName,
        posting_date: postingDate,
        due_date: dueDate,
        set_posting_time: 1,
        ignore_default_payment_terms_template: 1,
        ...(state.userTenantId ? { tenant_id: state.userTenantId } : {}),
        debit_to: state.debitToAccount,
        items: [
          {
            item_code: state.itemId || state.itemCode,
            qty: 1,
            rate: state.invoiceAmount,
            income_account: state.incomeAccount,
          },
        ],
      };
      assertDueDateOnOrAfterPostingDate(createBody.posting_date, createBody.due_date, "createBody");

      return createInvoice(createBody).then((res) => {
      const message = String(res.body?.message || "");
      const dueBeforePosting =
        res.status === 400 && /Due Date cannot be before Posting Date/i.test(message);

      if (dueBeforePosting) {
        const fallbackPosting = state.dashboardToDate;
        assertPostingDateInsideDashboardWindow(
          fallbackPosting,
          state.dashboardFromDate,
          state.dashboardToDate,
          "Sales Invoice fallback preflight",
        );
        const fallbackBody = {
          ...createBody,
          posting_date: fallbackPosting,
          due_date: addDaysYmd(fallbackPosting, 7),
          ignore_default_payment_terms_template: 1,
        };
        assertDueDateOnOrAfterPostingDate(
          fallbackBody.posting_date,
          fallbackBody.due_date,
          "fallbackBody",
        );
        return createInvoice(fallbackBody).then((retryRes) => {
          expect(
            [200, 201],
            `Sales Invoice create retry failed. status=${retryRes.status} body=${JSON.stringify(
              retryRes.body,
            )}`,
          ).to.include(retryRes.status);
          state.salesInvoiceName = getDocName(retryRes.body);
          expect(state.salesInvoiceName, "sales invoice name").to.be.a("string").and.not.be.empty;

          // Read back the created invoice to capture the effective grand_total from server-side calculations.
          ensureSalesInvoiceTenantId(base, token, state.salesInvoiceName).then(() =>
            readSalesInvoiceDoc(base, token, state.salesInvoiceName, "Sales Invoice GET after create").then((doc) => {
            state.createdInvoiceDoc = doc;
            state.createdInvoiceAmount = Number(doc?.grand_total || state.invoiceAmount || 0);
            state.effectiveInvoicePostingDate = doc?.posting_date || fallbackBody.posting_date;
            expect(
              doc?.posting_date,
              `Sales Invoice API ignored requested posting_date. requested=${fallbackBody.posting_date}, actual=${
                doc?.posting_date
              }, doc=${JSON.stringify(doc)}`,
            ).to.eq(fallbackBody.posting_date);
            expect(
              state.createdInvoiceAmount,
              `Created invoice amount invalid. doc=${JSON.stringify(doc)}`,
            ).to.be.greaterThan(0);
            return alignDashboardWindowToPostingDate(base, token, state.effectiveInvoicePostingDate).then(
              (matchedPreset) => {
                expect(
                  matchedPreset,
                  `Sales Invoice create effective posting_date not covered by supported dashboard presets. posting_date=${state.effectiveInvoicePostingDate}`,
                ).to.be.a("string").and.not.be.empty;
              },
            );
            }),
          );
        });
      }

      expect(
        [200, 201],
        `Sales Invoice create failed. status=${res.status} body=${JSON.stringify(res.body)}`,
      ).to.include(res.status);
      state.salesInvoiceName = getDocName(res.body);
      expect(state.salesInvoiceName, "sales invoice name").to.be.a("string").and.not.be.empty;

      // Read back the created invoice to capture the effective grand_total from server-side calculations.
      ensureSalesInvoiceTenantId(base, token, state.salesInvoiceName).then(() =>
        readSalesInvoiceDoc(base, token, state.salesInvoiceName, "Sales Invoice GET after create").then((doc) => {
        state.createdInvoiceDoc = doc;
        state.createdInvoiceAmount = Number(doc?.grand_total || state.invoiceAmount || 0);
        state.effectiveInvoicePostingDate = doc?.posting_date || createBody.posting_date;
        expect(
          doc?.posting_date,
          `Sales Invoice API ignored requested posting_date. requested=${createBody.posting_date}, actual=${
            doc?.posting_date
          }, doc=${JSON.stringify(doc)}`,
        ).to.eq(createBody.posting_date);
        expect(
          state.createdInvoiceAmount,
          `Created invoice amount invalid. doc=${JSON.stringify(doc)}`,
        ).to.be.greaterThan(0);
        return alignDashboardWindowToPostingDate(base, token, state.effectiveInvoicePostingDate).then(
          (matchedPreset) => {
            expect(
              matchedPreset,
              `Sales Invoice create effective posting_date not covered by supported dashboard presets. posting_date=${state.effectiveInvoicePostingDate}`,
            ).to.be.a("string").and.not.be.empty;
          },
        );
        }),
      );
    });
    });
  });
});

Given("the sales invoice is submitted", () => {
  const base = dashboardApiBaseUrl();
  const token = state.authToken;
  expect(state.salesInvoiceName, "salesInvoiceName").to.be.a("string").and.not.be.empty;

  const submitViaInvoiceService = () =>
    cy.request({
      method: "POST",
      url: `${base}/api/method/invoice_tracker.api.submit_sales_invoice`,
      headers: apiAuthHeaders(token),
      body: { name: state.salesInvoiceName },
      failOnStatusCode: false,
    });

  const submitViaFrappeClient = (doc) =>
    cy.request({
      method: "POST",
      url: `${base}/api/method/frappe.client.submit`,
      headers: apiAuthHeaders(token),
      body: {
        doc,
      },
      failOnStatusCode: false,
    });

  submitViaInvoiceService().then((res) => {
    if (res.status === 200) {
      state.submittedInvoiceResponse = res.body;
      return;
    }

    return readSalesInvoiceDoc(base, token, state.salesInvoiceName, "Sales Invoice GET before fallback submit").then(
      (latestDoc) => {
        const fallbackPayload = {
          ...latestDoc,
          doctype: "Sales Invoice",
          name: state.salesInvoiceName,
        };
        return submitViaFrappeClient(fallbackPayload).then((fallbackRes) => {
          if (fallbackRes.status === 200) {
            state.submittedInvoiceResponse = fallbackRes.body;
            return;
          }

          const isTimestampMismatch =
            fallbackRes.status === 417 &&
            /TimestampMismatchError/i.test(
              JSON.stringify(fallbackRes.body?.exception || fallbackRes.body?.exc || fallbackRes.body),
            );

          if (!isTimestampMismatch) {
            expect(
              fallbackRes.status,
              `Invoice submit failed. invoice-service status=${res.status} body=${JSON.stringify(
                res.body,
              )}; frappe.client.submit status=${fallbackRes.status} body=${JSON.stringify(fallbackRes.body)}`,
            ).to.eq(200);
            return;
          }

          // Re-read latest doc and retry once to handle optimistic-lock races.
          return readSalesInvoiceDoc(
            base,
            token,
            state.salesInvoiceName,
            "Sales Invoice GET before fallback submit retry",
          ).then((freshDoc) => {
            const retryPayload = {
              ...freshDoc,
              doctype: "Sales Invoice",
              name: state.salesInvoiceName,
            };
            return submitViaFrappeClient(retryPayload).then((retryRes) => {
              expect(
                retryRes.status,
                `Invoice submit failed after retry. invoice-service status=${res.status} body=${JSON.stringify(
                  res.body,
                )}; first frappe.client.submit status=${fallbackRes.status} body=${JSON.stringify(
                  fallbackRes.body,
                )}; retry status=${retryRes.status} body=${JSON.stringify(retryRes.body)}`,
              ).to.eq(200);
              state.submittedInvoiceResponse = retryRes.body;
            });
          });
        });
      },
    );
  }).then(() => {

    // Verify invoice is submitted and keep latest amount/status for dashboard assertions.
    readSalesInvoiceDoc(base, token, state.salesInvoiceName, "Sales Invoice GET after submit").then((doc) => {
      state.submittedInvoiceDoc = doc;
      expect(doc?.docstatus, `Invoice not submitted. doc=${JSON.stringify(doc)}`).to.eq(1);
      state.createdInvoiceAmount = Number(doc?.grand_total || state.createdInvoiceAmount || 0);
      state.effectiveInvoicePostingDate = doc?.posting_date || state.effectiveInvoicePostingDate;
      expect(state.createdInvoiceAmount, "submitted invoice amount").to.be.greaterThan(0);

      const visibilityFilters = encodeURIComponent(
        JSON.stringify([
          ["name", "=", state.salesInvoiceName],
          ["docstatus", "=", 1],
          ["company", "=", state.companyName],
        ]),
      );
      const visibilityFields = encodeURIComponent(
        JSON.stringify([
          "name",
          "company",
          "posting_date",
          "due_date",
          "docstatus",
          "status",
          "grand_total",
          "outstanding_amount",
        ]),
      );
      cy.request({
        method: "GET",
        url: `${buildResourceUrl(base, "Sales Invoice")}?filters=${visibilityFilters}&fields=${visibilityFields}&limit_page_length=5`,
        headers: apiAuthHeaders(token),
        failOnStatusCode: false,
      }).then((listRes) => {
        if (listRes.status === 200) {
          const rows = getDataRows(listRes.body);
          expect(
            rows.length,
            `Submitted invoice is not visible through Sales Invoice list API. invoice=${
              state.salesInvoiceName
            }, body=${JSON.stringify(listRes.body)}`,
          ).to.be.greaterThan(0);
          state.submittedInvoiceVisibility = rows[0];
          return;
        }

        // Fallback for roles that cannot list via /api/resource but can query via frappe.client.get_list
        cy.request({
          method: "POST",
          url: `${base}/api/method/frappe.client.get_list`,
          headers: apiAuthHeaders(token),
          body: {
            doctype: "Sales Invoice",
            filters: [
              ["name", "=", state.salesInvoiceName],
              ["docstatus", "=", 1],
              ["company", "=", state.companyName],
            ],
            fields: [
              "name",
              "company",
              "posting_date",
              "due_date",
              "docstatus",
              "status",
              "grand_total",
              "outstanding_amount",
            ],
            limit_page_length: 5,
          },
          failOnStatusCode: false,
        }).then((fallbackListRes) => {
          expect(
            fallbackListRes.status,
            `Submitted invoice visibility check failed. resource status=${listRes.status} resource body=${JSON.stringify(
              listRes.body,
            )}; fallback status=${fallbackListRes.status} fallback body=${JSON.stringify(fallbackListRes.body)}`,
          ).to.eq(200);
          const rows = Array.isArray(fallbackListRes.body?.message)
            ? fallbackListRes.body.message
            : getDataRows(fallbackListRes.body);
          expect(
            rows.length,
            `Submitted invoice is not visible through fallback Sales Invoice list API. invoice=${
              state.salesInvoiceName
            }, body=${JSON.stringify(fallbackListRes.body)}`,
          ).to.be.greaterThan(0);
          state.submittedInvoiceVisibility = rows[0];
        });
      });

      return alignDashboardWindowToPostingDate(
        base,
        token,
        state.effectiveInvoicePostingDate || state.invoicePostingDate,
      ).then((matchedPreset) => {
        expect(
          matchedPreset,
          `Sales Invoice submit effective posting_date not covered by supported dashboard presets. posting_date=${
            state.effectiveInvoicePostingDate || state.invoicePostingDate
          }`,
        ).to.be.a("string").and.not.be.empty;
      });
    });
  });
});

Given("I do not have an authentication token", () => {
  state.authToken = null;
  state.sid = null;
  state.csrfToken = null;
  state.authMode = "none";
});

Given("I have an invalid authentication token", () => {
  state.authToken = "invalid-token";
  state.sid = null;
  state.csrfToken = null;
  state.authMode = "bearer";
});

When("I request the dashboard data", () => {
  const base = dashboardApiBaseUrl();
  const preset = dashboardPreset();
  const headers = {
    Accept: "application/json",
  };
  if (state.authToken && state.authMode === "bearer") {
    headers.Authorization = `Bearer ${state.authToken}`;
  } else if (state.authToken && state.authMode === "sid") {
    headers["X-Frappe-SID"] = state.authToken;
    headers.Cookie = `sid=${state.authToken}`;
  }

  cy.request({
    method: "GET",
    url: `${base}/api/method/invoice_tracker.api.get_invoice_dashboard?preset=${encodeURIComponent(
      preset,
    )}&recent_limit=50`,
    headers,
    failOnStatusCode: false,
  }).then((res) => {
    state.lastResponse = res;
  });
});

When("I request the financial dashboard data", () => {
  const base = dashboardApiBaseUrl();
  const headers = {
    Accept: "application/json",
  };
  if (state.authToken && state.authMode === "bearer") {
    headers.Authorization = `Bearer ${state.authToken}`;
  } else if (state.authToken && state.authMode === "sid") {
    headers["X-Frappe-SID"] = state.authToken;
    headers.Cookie = `sid=${state.authToken}`;
  }

  cy.request({
    method: "GET",
    url: `${base}/api/method/expense_tracker.api.get_financial_dashboard?preset=last_7_days`,
    headers,
    failOnStatusCode: false,
  }).then((res) => {
    state.lastResponse = res;
  });
});

Then("the dashboard API should return success", () => {
  expect(
    state.lastResponse.status,
    `expected HTTP 200, body=${JSON.stringify(state.lastResponse.body)}`,
  ).to.eq(200);
});

Then("the dashboard response should contain summary metrics", () => {
  const b = state.lastResponse.body;
  expect(b, JSON.stringify(b)).to.be.an("object");
  expect(b).to.have.property("totals");
  expect(b.totals).to.include.keys(
    "total_invoiced",
    "total_paid",
    "total_outstanding",
    "total_overdue",
  );
  expect(b).to.have.property("recent_invoices");
  expect(b.recent_invoices).to.be.an("array");
});

Then("the dashboard should show the created invoice amount", () => {
  const createdAmount = Number(state.createdInvoiceAmount || state.invoiceAmount || 0);
  const expectedInvoiceDate = state.effectiveInvoicePostingDate || state.invoicePostingDate;
  const base = dashboardApiBaseUrl();
  const preset = dashboardPreset();
  const headers = { Accept: "application/json" };
  if (state.authToken && state.authMode === "bearer") {
    headers.Authorization = `Bearer ${state.authToken}`;
  } else if (state.authToken && state.authMode === "sid") {
    headers["X-Frappe-SID"] = state.authToken;
    headers.Cookie = `sid=${state.authToken}`;
  }

  const fetchDashboard = () =>
    cy.request({
      method: "GET",
      url: `${base}/api/method/invoice_tracker.api.get_invoice_dashboard?preset=${encodeURIComponent(
        preset,
      )}&recent_limit=50`,
      headers,
      failOnStatusCode: false,
    });

  const assertWithRetry = (retriesLeft = 5) =>
    fetchDashboard().then((res) => {
      state.lastResponse = res;
      expect(
        res.status,
        `dashboard request failed while validating created invoice. body=${JSON.stringify(res.body)}`,
      ).to.eq(200);

      const body = res.body || {};
      const totals = body.totals || {};
      expect(
        totals.total_invoiced,
        `dashboard totals missing/invalid: ${JSON.stringify(body)}`,
      ).to.be.a("number");

      const totalInvoiced = Number(totals.total_invoiced || 0);
      if (expectedInvoiceDate && body.from_date && body.to_date) {
        const inv = parseYmd(expectedInvoiceDate).getTime();
        const from = parseYmd(body.from_date).getTime();
        const to = parseYmd(body.to_date).getTime();
        const inRange = inv >= from && inv <= to;
        if (!inRange) {
          throw new Error(
            `invoice posting_date is outside dashboard window: ${JSON.stringify({
              invoicePostingDate: expectedInvoiceDate,
              dashboardFromDate: body.from_date,
              dashboardToDate: body.to_date,
              preset,
              salesInvoiceName: state.salesInvoiceName,
            })}`,
          );
        }
      }

      const recent = body.recent_invoices || [];
      const matched = state.salesInvoiceName
        ? recent.find((r) => r?.name === state.salesInvoiceName)
        : null;
      const matchedAmount = Number(matched?.grand_total || 0);

      const satisfied =
        (state.salesInvoiceName ? Boolean(matched) : true) &&
        totalInvoiced >= createdAmount &&
        (!matched || matchedAmount > 0);

      if (satisfied) {
        return;
      }

      if (retriesLeft <= 0) {
        const names = recent.map((r) => r?.name).filter(Boolean);
        throw new Error(
          `dashboard did not reflect created submitted invoice within retry window: ${JSON.stringify({
            total_invoiced: totalInvoiced,
            createdAmount,
            salesInvoiceName: state.salesInvoiceName,
            salesInvoiceCreatePath: state.salesInvoiceCreatePath,
            salesInvoiceCreateFallback: state.salesInvoiceCreateFallback,
            createdInvoice: {
              company: state.createdInvoiceDoc?.company,
              posting_date: state.createdInvoiceDoc?.posting_date,
              due_date: state.createdInvoiceDoc?.due_date,
              docstatus: state.createdInvoiceDoc?.docstatus,
              grand_total: state.createdInvoiceDoc?.grand_total,
            },
            submittedInvoice: {
              company: state.submittedInvoiceDoc?.company,
              posting_date: state.submittedInvoiceDoc?.posting_date,
              due_date: state.submittedInvoiceDoc?.due_date,
              docstatus: state.submittedInvoiceDoc?.docstatus,
              grand_total: state.submittedInvoiceDoc?.grand_total,
            },
            submittedInvoiceVisibility: state.submittedInvoiceVisibility || null,
            recentInvoiceNames: names,
            response: body,
          })}`,
        );
      }

      cy.wait(1000);
      return assertWithRetry(retriesLeft - 1);
    });

  return assertWithRetry();
});

Then("the dashboard API should return unauthorized", () => {
  expect(
    state.lastResponse.status,
    `expected unauthorized, body=${JSON.stringify(state.lastResponse.body)}`,
  ).to.eq(401);
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
  expect(state.lastResponse.body.docstatus).to.eq(1);
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

Then("the financial dashboard response should expose analytics fields", () => {
  const b = state.lastResponse.body;
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
  const rows = state.lastResponse.body.daily || [];
  expect(
    rows.length,
    "daily should include at least one day for the selected period",
  ).to.be.at.least(1);
  for (const row of rows) {
    expect(row).to.include.keys("date", "income", "expense", "net");
  }
});

Then("the financial dashboard preset should be {string}", (preset) => {
  expect(state.lastResponse.body.preset).to.eq(preset);
});

Then("the financial dashboard daily length should be {int}", (n) => {
  const expected = parseInt(n, 10);
  expect(state.lastResponse.body.daily.length).to.eq(expected);
});

Then("the financial dashboard recent activity should have resource paths when non-empty", () => {
  const items = state.lastResponse.body.recent_activity || [];
  for (const row of items) {
    expect(row).to.have.property("resource_path");
    expect(String(row.resource_path)).to.match(/^\/api\/resource\//);
  }
});
