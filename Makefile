# Development checks (same commands as .github/workflows/ci.yml)
# and wrappers for core/bin/mfactory.
#
#   make check
#   make help
#   make create ISSUE=12
#   make cli ARGS="--help"
#
# Extra flags go in ARGS. Switch a boolean flag on with VAR=1.

ifneq ($(wildcard .venv/bin/python),)
  PYTHON ?= .venv/bin/python
else
  PYTHON ?= python3
endif

MFACTORY := $(PYTHON) core/bin/mfactory
TOOL ?= claude

.DEFAULT_GOAL := help
.DELETE_ON_ERROR:

.PHONY: help check lint syntax test sample-dashboard \
	cli version \
	install init upgrade uninstall doctor status sync sync-check \
	config graph models skills \
	queue create next done answer close-issue close-job summary route checks \
	dashboard start-dashboard stop hook \
	workspace workspace-init workspace-add workspace-remove workspace-list workspace-queue

define need
	@test -n "$($(1))" || { echo "usage: make $(2)"; exit 1; }
endef

help: ## list checks and mfactory targets
	@echo "checks (same as CI)"
	@echo "  make check                 ruff, bash -n install.sh, pytest"
	@echo "  make lint                  ruff check ."
	@echo "  make syntax                bash -n install.sh"
	@echo "  make test                  pytest. ARGS=\"-k name\""
	@echo "  make sample-dashboard      dashboard with made-up jobs"
	@echo ""
	@echo "mfactory  ($(MFACTORY))"
	@echo "  make cli ARGS=\"--help\"     any CLI invocation"
	@echo "  make version"
	@echo "  make install               TOOL=claude  or  TOOLS=\"claude cursor\"  PERSONAL=1"
	@echo "  make init                  same command as install"
	@echo "  make upgrade               package command and every project in the workspace"
	@echo "                             SOURCE=.          install from this tree"
	@echo "                             SELF=1            global CLI only"
	@echo "                             ALL=1             workspace projects only"
	@echo "  make uninstall             CONFIG=1 LABELS=1"
	@echo "  make doctor                SKIP_CHECKS=1"
	@echo "  make status                ALL=1"
	@echo "  make sync"
	@echo "  make sync-check            sync --check (exit 1 if generated files differ)"
	@echo "  make config                JSON=1"
	@echo "  make graph                 JSON=1"
	@echo "  make models"
	@echo "  make skills"
	@echo "  make queue"
	@echo "  make create ISSUE=12"
	@echo "  make next JOB=004"
	@echo "  make done JOB=004          ARGS=--reply reads stdin"
	@echo "  make answer JOB=004 OUTCOME=approve TEXT=\"...\""
	@echo "  make summary               JOBS=\"004 005\""
	@echo "  make route JOB=004"
	@echo "  make checks                run [checks].commands in this directory"
	@echo "  make dashboard             PORT=4747 NO_OPEN=1"
	@echo "  make start-dashboard       same flags, returns"
	@echo "  make stop"
	@echo "  make workspace             ARGS=\"list\""
	@echo "  make workspace-init"
	@echo "  make workspace-add         DIR=../other (default: this repository)"
	@echo "  make workspace-remove      DIR=../other (default: this repository)"
	@echo "  make workspace-list"
	@echo "  make workspace-queue       CONCURRENCY=3"

check: lint syntax test ## ruff, bash -n install.sh, pytest

lint: ## ruff check .
	$(PYTHON) -m ruff check .

syntax: ## bash -n install.sh
	bash -n install.sh

test: ## pytest. ARGS="-k name"
	$(PYTHON) -m pytest $(ARGS)

sample-dashboard: ## dashboard with made-up jobs
	$(PYTHON) tests/sample_dashboard.py $(ARGS)

cli: ## raw CLI: make cli ARGS="queue"
	$(MFACTORY) $(ARGS)

version: ## mfactory version
	$(MFACTORY) version $(ARGS)

install: ## mfactory install. TOOL=claude or TOOLS="claude cursor". PERSONAL=1
	$(MFACTORY) install $(foreach t,$(if $(TOOLS),$(TOOLS),$(TOOL)),--tool $(t)) $(if $(PERSONAL),--personal) $(ARGS)

init: ## mfactory init (same as install). PERSONAL=1
	$(MFACTORY) init $(foreach t,$(if $(TOOLS),$(TOOLS),$(TOOL)),--tool $(t)) $(if $(PERSONAL),--personal) $(ARGS)

upgrade: ## global CLI and every project in the workspace. SELF=1 only the CLI. ALL=1 only projects. SOURCE=. uses this tree
	$(MFACTORY) upgrade $(if $(SELF),--self) $(if $(ALL),--all) $(if $(REF),--version $(REF)) $(if $(SOURCE),--source $(SOURCE)) $(ARGS)

uninstall: ## mfactory uninstall. CONFIG=1 LABELS=1
	$(MFACTORY) uninstall $(if $(CONFIG),--config) $(if $(LABELS),--labels) $(ARGS)

doctor: ## mfactory doctor. SKIP_CHECKS=1
	$(MFACTORY) doctor $(if $(SKIP_CHECKS),--skip-checks) $(ARGS)

status: ## mfactory status. ALL=1
	$(MFACTORY) status $(if $(ALL),--all) $(ARGS)

sync: ## mfactory sync
	$(MFACTORY) sync $(ARGS)

sync-check: ## mfactory sync --check
	$(MFACTORY) sync --check $(ARGS)

config: ## mfactory config show. JSON=1
	$(MFACTORY) config show $(if $(JSON),--json) $(ARGS)

graph: ## mfactory graph. JSON=1
	$(MFACTORY) graph $(if $(JSON),--json) $(ARGS)

models: ## mfactory models
	$(MFACTORY) models $(ARGS)

skills: ## mfactory skills
	$(MFACTORY) skills $(ARGS)

queue: ## mfactory queue
	$(MFACTORY) queue $(ARGS)

create: ## mfactory create ISSUE=12
	$(call need,ISSUE,create ISSUE=12)
	$(MFACTORY) create "$(ISSUE)" $(ARGS)

next: ## mfactory next JOB=004
	$(call need,JOB,next JOB=004)
	$(MFACTORY) next "$(JOB)" $(ARGS)

done: ## mfactory done JOB=004. ARGS=--reply reads stdin
	$(call need,JOB,done JOB=004)
	$(MFACTORY) done "$(JOB)" $(ARGS)

answer: ## mfactory answer JOB=004 OUTCOME=approve TEXT="..."
	$(call need,JOB,answer JOB=004 OUTCOME=approve)
	$(call need,OUTCOME,answer JOB=004 OUTCOME=approve)
	$(MFACTORY) answer "$(JOB)" "$(OUTCOME)" $(if $(TEXT),"$(TEXT)") $(ARGS)

close-issue: ## mfactory close issue JOB=004 (ARGS=--not-planned)
	$(call need,JOB,close-issue JOB=004)
	$(MFACTORY) close issue "$(JOB)" $(ARGS)

close-job: ## mfactory close job JOB=004
	$(call need,JOB,close-job JOB=004)
	$(MFACTORY) close job "$(JOB)" $(ARGS)

summary: ## mfactory summary. JOBS="004 005"
	$(MFACTORY) summary $(JOBS) $(ARGS)

route: ## mfactory route JOB=004
	$(call need,JOB,route JOB=004)
	$(MFACTORY) route "$(JOB)" $(ARGS)

checks: ## mfactory checks
	$(MFACTORY) checks $(ARGS)

dashboard: ## mfactory dashboard. PORT=4747 NO_OPEN=1
	$(MFACTORY) dashboard $(if $(PORT),--port $(PORT)) $(if $(NO_OPEN),--no-open) $(ARGS)

start-dashboard: ## mfactory start dashboard. PORT= NO_OPEN=1
	$(MFACTORY) start dashboard $(if $(PORT),--port $(PORT)) $(if $(NO_OPEN),--no-open) $(ARGS)

stop: ## mfactory stop
	$(MFACTORY) stop $(ARGS)

hook: ## mfactory hook
	$(MFACTORY) hook $(ARGS)

workspace: ## mfactory workspace. ARGS="list"
	$(MFACTORY) workspace $(ARGS)

workspace-init: ## mfactory workspace init
	$(MFACTORY) workspace init $(ARGS)

workspace-add: ## mfactory workspace add. DIR=../other (default: this repository)
	$(MFACTORY) workspace add $(if $(DIR),"$(DIR)") $(ARGS)

workspace-remove: ## mfactory workspace remove. DIR=../other (default: this repository)
	$(MFACTORY) workspace remove $(if $(DIR),"$(DIR)") $(ARGS)

workspace-list: ## mfactory workspace list
	$(MFACTORY) workspace list $(ARGS)

workspace-queue: ## mfactory workspace queue. CONCURRENCY=3
	$(MFACTORY) workspace queue $(if $(CONCURRENCY),--concurrency $(CONCURRENCY)) $(ARGS)
