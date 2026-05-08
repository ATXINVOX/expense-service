@expense-api @financial-dashboard @kong
Feature: Financial dashboard API access
  Access and response validation for expense financial dashboard endpoint.

  Scenario: Authenticated user can fetch financial dashboard
    Given I am an authenticated user
    When I request the financial dashboard data
    Then the expense API last response status should be 200
    And the response body should have field "totals"
    And the response body should have field "daily"
    And the response body should have field "from_date"
    And the response body should have field "to_date"

  Scenario: Financial dashboard rejects request without token
    Given I do not have an authentication token
    When I request the financial dashboard data
    Then the dashboard API should return unauthorized

  Scenario: Financial dashboard rejects invalid token
    Given I have an invalid authentication token
    When I request the financial dashboard data
    Then the dashboard API should return unauthorized
