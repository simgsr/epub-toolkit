# EPUB toolkit -- split multi-book bundles, bring books up to EPUB 3, check them.
#
# macOS /usr/bin/make is GNU Make 3.81: there is no .ONESHELL and no
# .SHELLFLAGS, so every recipe line runs in its own shell -- keep a multi-step
# recipe on one line joined with '; \'.  Output names hold spaces and CJK
# ("徐皓峰 - 大日坛城.epub"), so iterate with shell globs, never make's
# $(wildcard)/$(basename), which word-split on spaces.

SHELL     := /bin/sh
PYTHON    ?= python3
EPUBCHECK ?= epubcheck

# where the scripts write; exported so epub_paths.py agrees with the shell
SPLIT     ?= split
REPORTS   ?= epubcheck-out
export EPUB_SPLIT_DIR  := $(SPLIT)
export EPUB_REPORT_DIR := $(REPORTS)

.DEFAULT_GOAL := help
.PHONY: help plan split-epub correct names names-fix verify content epubcheck \
        summarise check check-lite run clean clean-all

help:                ## show this list
	@echo 'EPUB toolkit.  Drop EPUBs in this folder, then:'
	@echo
	@echo '  make run         everything below, stopping at the first failure'
	@echo
	@echo '  make plan        show the split plan for the bundles here, write nothing'
	@echo '  make split-epub  split each multi-book bundle into $(SPLIT)/'
	@echo '  make correct     repair + upgrade top-level EPUBs and $(SPLIT)/ to EPUB 3'
	@echo '  make check       names + verify + content + epubcheck + summarise'
	@echo
	@echo '  make names       check every EPUB filename is "Author - Book name"'
	@echo '  make names-fix   rename the ones that are not'
	@echo '  make verify      structural checks on $(SPLIT)/*.epub'
	@echo '  make content     prove no text was lost (needs the source bundle here)'
	@echo '  make epubcheck   one JSON report per book into $(REPORTS)/'
	@echo '  make summarise   tally $(REPORTS)/*.json'
	@echo '  make check-lite  verify + content, skipping epubcheck'
	@echo
	@echo '  make clean       drop $(REPORTS)/ and caches; keeps $(SPLIT)/*.epub'
	@echo '  make clean-all   also remove $(SPLIT)/*.epub (the directory stays)'
	@echo
	@echo 'Finished books land in $(SPLIT)/ -- move them out yourself when happy.'
	@echo 'PYTHON=$(PYTHON)  (override with: make PYTHON=python3.14 run)'

plan:                ## show the split plan, write nothing
	$(PYTHON) split_epubs.py --list

split-epub:          ## split each multi-book bundle into $(SPLIT)/
	$(PYTHON) split_epubs.py

correct:             ## repair + upgrade top-level EPUBs and $(SPLIT)/ to EPUB 3
	$(PYTHON) normalise_epub.py --out $(SPLIT)

names:               ## check every EPUB is named "Author - Book name"
	$(PYTHON) check_filenames.py

names-fix:           ## rename the EPUBs that break the filename convention
	$(PYTHON) check_filenames.py --fix

verify:              ## structural checks on $(SPLIT)/*.epub
	$(PYTHON) verify_epubs.py

content:             ## prove no text was lost (needs the source bundle here)
	$(PYTHON) content_check.py

epubcheck:           ## one JSON report per book into $(REPORTS)/
	@rm -rf $(REPORTS); mkdir -p $(REPORTS); found=0; fail=0; \
	for f in $(SPLIT)/*.epub; do [ -e "$$f" ] || continue; found=1; \
	  b=$$(basename "$$f" .epub); \
	  echo "--- $$b"; \
	  $(EPUBCHECK) --json "$(REPORTS)/$$b.json" "$$f" || fail=1; \
	done; \
	[ $$found -eq 1 ] || echo "no EPUBs in $(SPLIT)/ to check"; \
	[ $$fail -eq 0 ] || { echo "epubcheck reported errors; see $(REPORTS)/"; exit 1; }

summarise:           ## tally $(REPORTS)/*.json
	$(PYTHON) summarise_epubcheck.py

check: names verify content epubcheck summarise   ## the full quality gate
check-lite: names verify content                  ## skip epubcheck

# recursive make gives a guaranteed serial order that aborts on the first
# non-zero exit, whatever -j says
run:                 ## everything, stopping at the first failure
	@$(MAKE) --no-print-directory split-epub
	@$(MAKE) --no-print-directory correct
	@$(MAKE) --no-print-directory check

clean:               ## drop reports and caches (keeps $(SPLIT)/*.epub)
	@rm -rf $(REPORTS) __pycache__ .ruff_cache
	@find . -name .DS_Store -delete 2>/dev/null || true
	@echo "cleaned $(REPORTS)/ and caches; $(SPLIT)/*.epub left alone"

clean-all: clean     ## also remove $(SPLIT)/*.epub, keeping the directory
	@rm -f $(SPLIT)/*.epub
	@echo "removed $(SPLIT)/*.epub (kept the $(SPLIT)/ directory)"
