# Expense Service

A microservice for tracking business expenses without the `Employee` overhead of HRMS. It uses `Journal Entry` internally to provide a clean accounting trail.

## Features
- **Simplified Expense API**: Submit expenses with just category, amount, date, and description.
- **Tenant-Aware CRUD**: Automatic isolation based on user context.
- **Accounting Integration**: Transparently creates `Journal Entry` (Cash/Bank) records in ERPNext.

## API Endpoints
### Resource API
- `GET  /api/resource/journal-entry`   - List user expenses
- `POST /api/resource/journal-entry`  - Create new expense (accepts simplified JSON)
- `GET  /api/resource/expense-claim-type` - Fetch categories

### JSON Schema (Simplified)
```json
{
  "category": "Fuel",
  "amount": 50.00,
  "posting_date": "2024-03-29",
  "user_remark": "Visit to client site",
  "company": "Acme Corp"
}
```

## Development
```bash
# Run tests
pytest
```
