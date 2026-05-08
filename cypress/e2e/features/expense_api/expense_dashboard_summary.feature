@expense-dashboard
Feature: Expense dashboard summary API contract
  The expense dashboard screen consumes GET
  /api/method/expense_tracker.api.get_dashboard_summary.
  These scenarios validate legacy and preset modes used by the UI.

  Environment (Cypress env or shell):
    EXPENSE_SERVICE_URL  - default http://localhost:9004
    EXPENSE_FRAPPE_URL   - Frappe login endpoint (default http://localhost:8000)
    ADMIN_PASSWORD       - Administrator password (default "admin")

  Background:
    Given I login to the expense service as "Administrator"

  Scenario: Legacy dashboard without period returns baseline fields
    When I GET the dashboard summary
    Then the expense API last response status should be 200
    And the dashboard response should include keys:
      | total_spend      |
      | gst_total        |
      | currency         |
      | period           |
      | breakdown        |
      | recent_expenses  |

  Scenario: Week preset returns trend and weekly cashflow buckets
    When I GET the dashboard summary with query "period=week"
    Then the expense API last response status should be 200
    And the dashboard preset should be "week"
    And the dashboard response should include keys:
      | compare_period_label |
      | trend_pct            |
      | previous_period_total |
      | top_category         |
      | from_date            |
      | to_date              |
      | cashflow             |
      | cashflow_stats       |
      | breakdown_top4       |
    And the dashboard cashflow should have bucket count 7
    And each dashboard breakdown row should include pct and color

  Scenario: Month preset returns month segments
    When I GET the dashboard summary with query "period=month"
    Then the expense API last response status should be 200
    And the dashboard preset should be "month"
    And the dashboard cashflow should have bucket count 4

  Scenario: Year preset returns monthly buckets
    When I GET the dashboard summary with query "period=year"
    Then the expense API last response status should be 200
    And the dashboard preset should be "year"
    And the dashboard cashflow should have bucket count 12

  Scenario: Period is normalized case-insensitively
    When I GET the dashboard summary with query "period=WEEK"
    Then the expense API last response status should be 200
    And the dashboard preset should be "week"

  Scenario: Empty period stays in legacy mode
    When I GET the dashboard summary with query "period="
    Then the expense API last response status should be 200
    And the dashboard response should not include keys:
      | preset   |
      | cashflow |

  Scenario: Invalid period returns validation error
    When I GET the dashboard summary with query "period=quarter"
    Then the expense API last response status should be one of "400, 417, 422"
