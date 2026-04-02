Feature: Expense custom fields on Purchase Invoice
  The expense service stores the primary item name, item group, and item count
  as custom fields on the Purchase Invoice parent record. This allows the
  standard Frappe resource API (/api/resource/Purchase Invoice) to return all
  expense details in a single call — no custom API endpoint required.

  Background:
    Given I am logged in as a provisioned user
    And my company has a default chart of accounts

  Scenario: Single-item expense populates custom fields
    When I create a Purchase Invoice with:
      | field        | value             |
      | supplier     | Officeworks       |
      | posting_date | 2026-03-30        |
      | items        | [{"item_code":"Printer Paper A4","item_group":"Office Supplies","qty":2,"rate":25.00}] |
    Then the response status should be 200
    And the Purchase Invoice should have "expense_item_name" as "Printer Paper A4"
    And the Purchase Invoice should have "expense_item_group" as "Office Supplies"
    And the Purchase Invoice should have "expense_items_count" as 1

  Scenario: Multi-item expense uses first item for custom fields
    When I create a Purchase Invoice with:
      | field        | value             |
      | supplier     | BP                |
      | posting_date | 2026-03-30        |
      | items        | [{"item_code":"Fuel","item_group":"Travel","qty":1,"rate":80.00},{"item_code":"Toll","item_group":"Travel","qty":1,"rate":6.50}] |
    Then the response status should be 200
    And the Purchase Invoice should have "expense_item_name" as "Fuel"
    And the Purchase Invoice should have "expense_item_group" as "Travel"
    And the Purchase Invoice should have "expense_items_count" as 2

  Scenario: Resource API returns custom fields without custom endpoint
    Given I have created a Purchase Invoice with item "Coffee" in group "Meals"
    When I fetch Purchase Invoices via the resource API with fields:
      | field                   |
      | name                    |
      | company                 |
      | supplier                |
      | posting_date            |
      | grand_total             |
      | expense_item_name       |
      | expense_item_group      |
      | expense_items_count     |
    Then the response status should be 200
    And each invoice in the response should have "expense_item_name"
    And each invoice in the response should have "expense_item_group"
    And each invoice in the response should have "expense_items_count"

  Scenario: Item and Item Group records are auto-created
    When I create a Purchase Invoice with a new item "Ergonomic Chair" in group "Furniture"
    Then the response status should be 200
    And an Item named "Ergonomic Chair" should exist
    And an Item Group named "Furniture" should exist
    And the Purchase Invoice should have "expense_item_name" as "Ergonomic Chair"
    And the Purchase Invoice should have "expense_item_group" as "Furniture"

  Scenario: Expense without items sets empty custom fields
    When I create a Purchase Invoice with no items
    Then the Purchase Invoice should have "expense_item_name" as ""
    And the Purchase Invoice should have "expense_item_group" as ""
    And the Purchase Invoice should have "expense_items_count" as 0
