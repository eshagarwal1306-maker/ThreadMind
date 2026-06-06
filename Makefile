PYTHON ?= python3

.PHONY: install ingest run run-debug eval clean

install:
	$(PYTHON) -m pip install -r requirements.txt

ingest:
	$(PYTHON) -m app.ingest --rebuild

run:
	$(PYTHON) -m app.main

run-debug:
	$(PYTHON) -m app.main --debug

eval:
	$(PYTHON) -m app.eval

clean:
	rm -f threadmind.db
	rm -f outputs/actions.jsonl outputs/eval_results.json outputs/eval_results.csv
