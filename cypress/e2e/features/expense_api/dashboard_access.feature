@expense-api @dashboard-security @kong
Feature: Dashboard security and access
  Dashboard API must be accessed through Kong with authentication.

  Environment (Cypress env or shell):
    EXPENSE_GATEWAY_URL      — Kong base URL (default EXPENSE_FRAPPE_URL, then http://localhost:8000)
    TEST_USER_EMAIL          — login email for dashboard tests
    TEST_USER_PASSWORD       — login password for dashboard tests

  Scenario: Dashboard API is accessible with valid token
    Given I am an authenticated user
    When I request the dashboard data
    Then the dashboard API should return success
    And the dashboard response should contain summary metrics

  Scenario: Dashboard API rejects request without token
    Given I do not have an authentication token
    When I request the dashboard data
    Then the dashboard API should return unauthorized

  Scenario: Dashboard API rejects invalid token
    Given I have an invalid authentication token
    When I request the dashboard data
    Then the dashboard API should return unauthorized
