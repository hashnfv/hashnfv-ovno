#!/usr/bin/make
PYTHON := /usr/bin/env python

clean:
	rm -rf .coverage .tox .testrepository trusty .unit-state.db
	find . -iname '*.pyc' -delete

lint:
	@tox -e pep8

test:
	@# Bundletester expects unit tests here.
	tox -e py27

functional_test:
	@echo Starting Amulet tests...
	@tests/setup/00-setup
	@juju test -v -p AMULET_HTTP_PROXY,AMULET_OS_VIP --timeout 2700

bin/charm_helpers_sync.py:
	@mkdir -p bin
	@bzr cat lp:charm-helpers/tools/charm_helpers_sync/charm_helpers_sync.py \
        > bin/charm_helpers_sync.py

sync: bin/charm_helpers_sync.py
	@$(PYTHON) bin/charm_helpers_sync.py -c charm-helpers-hooks.yaml
	@$(PYTHON) bin/charm_helpers_sync.py -c charm-helpers-tests.yaml

publish: lint test
	bzr push lp:charms/ceilometer
	bzr push lp:charms/trusty/ceilometer
