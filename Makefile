.PHONY: migrate migrate-status migrate-down migrate-bootstrap test-integration

# Apply pending SQL migrations against $DATABASE_URL.
migrate:
	python -m database.runner up

# Show which migrations are applied vs pending on the current database.
migrate-status:
	python -m database.runner status

# Roll back the most-recent migration (use LIMIT=N to roll back more).
migrate-down:
	python -m database.runner down $(if $(LIMIT),--limit $(LIMIT))

# One-time registration of migrations 001/002 that pre-date this runner.
# Idempotent: safe to re-run.
migrate-bootstrap:
	python -m database.runner bootstrap

# Run the testcontainers-backed integration suite (needs Docker).
test-integration:
	cd qa-automation/AI-Scoring && pytest tests/integration -v
