# Convenience targets. On Windows without `make`, run the underlying commands directly
# (see README) or use `python scripts/...`.

.PHONY: install install-extra synthetic demo test schema clean

install:
	pip install -r requirements.txt

install-extra: install
	pip install -r requirements-extra.txt

synthetic:
	python scripts/gen_synthetic.py

demo: synthetic
	python scripts/run_demo.py

test:
	pytest -q

schema:
	python -c "from resume_matcher.inference.schema import write_json_schema; print(write_json_schema())"

clean:
	python -c "import shutil,glob,os; [shutil.rmtree(p,ignore_errors=True) for p in glob.glob('**/__pycache__',recursive=True)]"
