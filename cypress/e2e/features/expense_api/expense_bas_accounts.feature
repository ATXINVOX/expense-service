@expense-api @bas-accounts
Feature: Purchase Invoice BAS account mapping (expense-service)
  Verifies GST tax rows use account_1b from AU Simpler BAS Report Setup.

  Prerequisites:
    - AU Simpler BAS Report Setup for the test company (integration bootstrap)
    - cypress/fixtures/resolved_pi_bas_accounts.json with bas.account_1b

  Background:
    Given I login to the expense service as "Administrator"
    And AU Simpler BAS Report Setup accounts are available for expenses

  Scenario: POST with GST enriches tax account_head from BAS 1B
    When I POST a new Purchase Invoice with BAS enrichment and body:
      """
      {
        "supplier": "Cypress API Supplier",
        "posting_date": "2026-04-08",
        "remarks": "Cypress BAS GST mapping",
        "taxes_and_charges": "AU GST 10%",
        "items": [
          {"item_code": "Cypress-Item-1", "qty": 1, "rate": 300}
        ]
      }
      """
    Then the expense API last response status should be 201
    And I store the created Purchase Invoice name from the response
    And the created purchase invoice GST tax account should match BAS 1B
    When I DELETE the stored Purchase Invoice
    Then the expense API last response status should be 200
