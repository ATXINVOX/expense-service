@expense-api
Feature: Expense full API lifecycle
  Covers the complete Purchase Invoice lifecycle through the expense-service HTTP API:
  create → enrich → submit → dashboard aggregation → delete.

  All requests use a session SID obtained by logging in via the Frappe login API
  (POST /api/method/login). No pre-configured SID is required.

  Environment (Cypress env or shell):
    EXPENSE_SERVICE_URL  — default http://localhost:9004
    EXPENSE_FRAPPE_URL   — Frappe login endpoint (default http://localhost:8000)
    EXPENSE_TEST_COMPANY — company name injected into each POST body
    ADMIN_PASSWORD       — Administrator password (default "admin")

  Background:
    Given I login to the expense service as "Administrator"

  # ---------------------------------------------------------------------------
  # 1. Create → Enrichment check
  # ---------------------------------------------------------------------------

  Scenario: POST creates a draft invoice and controller enriches it
    When I POST a new Purchase Invoice with body:
      """
      {
        "supplier": "Cypress API Supplier",
        "posting_date": "2026-04-08",
        "remarks": "Cypress full flow - create",
        "items": [
          {"item_code": "Cypress-Item-1", "qty": 2, "rate": 500}
        ]
      }
      """
    Then the expense API last response status should be 201
    And I store the created Purchase Invoice name from the response
    When I GET the stored Purchase Invoice
    Then the expense API last response status should be 200
    And the stored invoice should have docstatus 0
    And the stored invoice should have workflow status "Draft"
    And the response body should have field "grand_total"

  # ---------------------------------------------------------------------------
  # 2. Submit
  # ---------------------------------------------------------------------------

  Scenario: Submit a draft invoice promotes it to Submitted
    When I POST a new Purchase Invoice with body:
      """
      {
        "supplier": "Cypress API Supplier",
        "posting_date": "2026-04-08",
        "remarks": "Cypress full flow - submit",
        "items": [
          {"item_code": "Cypress-Item-1", "qty": 1, "rate": 750}
        ]
      }
      """
    Then the expense API last response status should be 201
    And I store the created Purchase Invoice name from the response
    When I POST submit_purchase_invoice for the stored invoice name
    Then the expense API last response status should be 200
    And the submit response should show success and status Submitted
    When I GET the stored Purchase Invoice
    Then the stored invoice should have docstatus 1
    And the stored invoice should have workflow status "Submitted"

  # ---------------------------------------------------------------------------
  # 3. Delete a draft
  # ---------------------------------------------------------------------------

  Scenario: Delete a draft invoice removes it
    When I POST a new Purchase Invoice with body:
      """
      {
        "supplier": "Cypress API Supplier",
        "posting_date": "2026-04-08",
        "remarks": "Cypress full flow - delete draft",
        "items": [
          {"item_code": "Cypress-Item-1", "qty": 1, "rate": 100}
        ]
      }
      """
    Then the expense API last response status should be 201
    And I store the created Purchase Invoice name from the response
    When I DELETE the stored Purchase Invoice
    Then the expense API last response status should be 200
    And the delete response should show success

  # ---------------------------------------------------------------------------
  # 4. Dashboard aggregation
  # ---------------------------------------------------------------------------

  Scenario: Dashboard summary returns numeric total_spend
    When I GET the dashboard summary
    Then the expense API last response status should be 200
    And the dashboard response should have a total_spend field

  # ---------------------------------------------------------------------------
  # 5. Missing name → 400
  # ---------------------------------------------------------------------------

  Scenario: Submit without invoice name returns 400
    When I POST submit_purchase_invoice with JSON body:
      """
      {}
      """
    Then the expense API last response status should be 400
