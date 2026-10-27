# Demo mode (docs/demo.md). The real stack is still `docker compose up --build -d`.
DEMO := docker compose -p siliconflow-demo --env-file .env.demo -f docker-compose.yml -f docker-compose.demo.yml

.PHONY: demo demo-reset demo-down demo-replay demo-fixtures

## Start the demo stack, then open http://localhost:8080 and click "Start tour".
demo: .env.demo
	$(DEMO) up --build -d --wait
	@echo
	@echo "SiliconFlow demo mode is up: open http://localhost:8080 and click \"Start tour\"."
	@echo "It replays recorded inputs through the real backend, in its own database (sfci_demo)."

## Delete every demo build and artifact, and start again from empty.
demo-reset: .env.demo
	$(DEMO) down --volumes
	$(DEMO) up --build -d --wait

## Stop the demo stack, keeping its data.
demo-down: .env.demo
	$(DEMO) down

## Replay every scenario without the UI: make demo-replay SPEED=4
demo-replay:
	python3 scripts/demo_replay.py --speed $(or $(SPEED),2)

## Rebuild demo/fixtures from their sources.
demo-fixtures:
	python3 demo/build_fixtures.py

# A Postgres password for the demo project only; never the real stack's.
.env.demo:
	@printf 'POSTGRES_PASSWORD=%s\n' "$$(openssl rand -hex 24)" > .env.demo
	@echo "wrote .env.demo"
