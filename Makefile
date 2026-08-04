.PHONY: test install install-project init doctor

test:
	python3 -m unittest discover -s tests -v

install:
	python3 bin/install.py --global

install-project:
	python3 bin/install.py --project .

init:
	python3 bin/brain.py init

doctor:
	python3 bin/brain.py doctor
