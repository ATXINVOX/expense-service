@expense-api @dashboard-security @kong
Feature: Dashboard security and access
  Dashboard API must be accessed through Kong with authentication.

  Environment (Cypress env or shell):
    API_BASE_URL         — Kong public API base URL
    AUTH_TOKEN_URL       — auth provider token endpoint (OAuth2 password grant)
    AUTH_CLIENT_ID       — OAuth client id
    AUTH_CLIENT_SECRET   — OAuth client secret
    TEST_USERNAME        — test user login
    TEST_PASSWORD        — test user password
    TEST_COMPANY         — optional company override
    TEST_INVOICE_AMOUNT  — optional amount override (default 4000)

  Scenario: Dashboard shows invoice metrics for authenticated user
    Given I am an authenticated user
    And required invoice test data exists
    And a sales invoice is created for the test user
    And the sales invoice is submitted
    When I request the dashboard data
    Then the dashboard API should return success
    And the dashboard response should contain summary metrics
    And the dashboard should show the created invoice amount

  Scenario: Dashboard API rejects request without token
    Given I do not have an authentication token
    When I request the dashboard data
    Then the dashboard API should return unauthorized

  Scenario: Dashboard API rejects invalid token
    Given I have an invalid authentication token
    When I request the dashboard data
    Then the dashboard API should return unauthorized
