PREFIX ?= $(HOME)/.local
PYTHON ?= /usr/bin/python3

.PHONY: run test check install uninstall
run:
	$(PYTHON) -m simplescheduler
test:
	$(PYTHON) -m unittest discover -s tests -v
check: test
	$(PYTHON) -m compileall -q simplescheduler
install:
	$(PYTHON) tools/install.py --prefix "$(PREFIX)"
uninstall:
	$(PYTHON) tools/install.py --prefix "$(PREFIX)" --uninstall
