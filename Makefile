PYTHON ?= python3

.PHONY: install ingest run run-proactive run-debug eval clean help

help:
	@echo "ThreadMind — make targets:"
	@echo "  make install        install Python dependencies"
	@echo "  make ingest         build database from source data"
	@echo "  make run            run interactive agent (conservative prompt)"
	@echo "  make run-proactive  run interactive agent (proactive prompt)"
	@echo "  make run-debug      run agent showing all tool calls and results"
	@echo "  make eval           run 20-prompt benchmark (both prompt variants)"
	@echo "  make clean          delete database and output files"

install:
	$(PYTHON) -m pip install -r requirements.txt

ingest:
	$(PYTHON) -m app.ingest --rebuild

run:
	$(PYTHON) -m app.main --prompt-style conservative

run-proactive:
	$(PYTHON) -m app.main --prompt-style proactive

run-debug:
	$(PYTHON) -m app.main --debug

eval:
	$(PYTHON) -m app.eval

clean:
	rm -f threadmind.db
	rm -f outputs/actions.jsonl outputs/eval_results.json outputs/eval_results.csv
