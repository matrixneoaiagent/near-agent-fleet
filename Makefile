.PHONY: test status route demo serve clean

test:
	python3 -m unittest discover -s tests -v

status:
	python3 fleet.py status

route:
	@test -n "$(BRIEF)" || (echo 'usage: make route BRIEF="five vertical clips"' >&2; exit 1)
	python3 fleet.py route "$(BRIEF)"

demo:
	./demo.sh

serve:
	python3 fleet.py serve --port 8780

clean:
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
