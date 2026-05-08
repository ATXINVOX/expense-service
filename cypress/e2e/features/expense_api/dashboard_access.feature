@expense-api @financial-dashboard
Feature: Financial dashboard API access
  Minimal fetch check for expense financial dashboard endpoint.

  Scenario: Fetch financial dashboard
    Given I login to the expense service as "Administrator" with password "admin"
    When I GET the financial dashboard
    Then the expense API last response status should be 200
    And the response body should have field "totals"
    And the response body should have field "daily"
    And the response body should have field "from_date"
    And the response body should have field "to_date"
