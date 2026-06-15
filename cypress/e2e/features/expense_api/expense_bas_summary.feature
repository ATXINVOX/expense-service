@expense-api @bas-summary
Feature: BAS summary API contract
  The GST/BAS mobile screen consumes GET
  /api/method/expense_tracker.api.get_bas_summary.

  Background:
    Given I login to the expense service as "Administrator"

  Scenario: Quarter preset returns BAS summary fields
    When I GET the BAS summary with query "period=quarter"
    Then the expense API last response status should be 200
    And the BAS summary response should include keys:
      | company          |
      | period           |
      | preset           |
      | from_date        |
      | to_date          |
      | currency         |
      | g1               |
      | gst_collected_1a |
      | gst_paid_1b      |
      | net_gst          |
      | gst_to_pay       |
      | gst_refund       |

  Scenario: Month preset returns month bounds
    When I GET the BAS summary with query "period=month"
    Then the expense API last response status should be 200
    And the BAS summary preset should be "month"

  Scenario: Invalid period returns validation error
    When I GET the BAS summary with query "period=year"
    Then the expense API last response status should be 400

  Scenario: Custom period requires both dates
    When I GET the BAS summary with query "period=custom"
    Then the expense API last response status should be 400
