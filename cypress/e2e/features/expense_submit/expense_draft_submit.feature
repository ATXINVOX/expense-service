@expense-api
Feature: Expense draft and confirm submit
  Purchase Invoices are created as Draft (docstatus 0). After the user confirms,
  POST /api/method/frappe.client.submit promotes them to
  Submitted (docstatus 1, status Submitted).

  These scenarios require a running expense-service and a valid session.

  Environment (Cypress env or shell):
    EXPENSE_SERVICE_URL — default http://localhost:9004
    EXPENSE_TEST_SID    — Frappe session id (Cookie sid + X-Frappe-SID)
    EXPENSE_TEST_COMPANY — company name for POST body (optional)

  Background:
    Given the expense API test session is configured

  Scenario: Create expense stays draft until confirm submit
    When I POST a new Purchase Invoice for expense draft submit with:
      | field         | value                                           |
      | supplier      | BDD Test Supplier                               |
      | posting_date  | 2026-04-07                                      |
      | remarks       | BDD draft then submit                           |
      | items_json    | [{"item_code":"BDD-Item-1","qty":1,"rate":10}]  |
    Then the expense API last response status should be 201
    And I store the created Purchase Invoice name from the response
    When I GET the stored Purchase Invoice
    Then the expense API last response status should be 200
    And the stored invoice should have docstatus 0
    When I POST submit_purchase_invoice for the stored invoice name
    Then the expense API last response status should be 200
    And the submit response should show success and status Submitted
    When I GET the stored Purchase Invoice
    Then the stored invoice should have docstatus 1
    And the stored invoice should have workflow status "Submitted"

  Scenario: Submit without invoice name returns error
    When I POST submit_purchase_invoice with JSON body:
      """
      {}
      """
    Then the expense API last response status should be 400
