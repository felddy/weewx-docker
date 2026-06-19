CONTAINER_VERSION := $(shell tr -d '[:space:]' < src/version.txt)

# Container image repository.
IMAGE := ghcr.io/felddy/weewx

# GitHub repository and the branch-protection ruleset managed as code.
# The ruleset ID is resolved at run time by name, so it is not hardcoded.
# See .github/rulesets/README.md for what the JSON contains (and a decoder
# for the GitHub App IDs it references).
REPO := felddy/weewx-docker
RULESET_FILE := .github/rulesets/development.json

.PHONY: guard-version guard-gh build test version github-output help release apply-ruleset export-ruleset

## guard-version: fail loudly if the version source is missing or empty.
guard-version:
	@test -n "$(CONTAINER_VERSION)" || { echo "ERROR: src/version.txt missing or empty" >&2; exit 1; }

## build: build the container image tagged with the CONTAINER_VERSION.
build: guard-version
	docker buildx build --build-arg CONTAINER_VERSION=$(CONTAINER_VERSION) --load --tag $(IMAGE):$(CONTAINER_VERSION) .

## test: run the test suite against the built container image.
test: guard-version
	uv run --group dev pytest tests/ --image-tag $(IMAGE):$(CONTAINER_VERSION)

## version: print the derived CONTAINER_VERSION.
version: guard-version
	@echo "Container : $(CONTAINER_VERSION)"

## github-output: print key=value lines for appending to $GITHUB_OUTPUT.
github-output: guard-version
	@echo "container_version=$(CONTAINER_VERSION)"

## release: set an explicit version and commit (make release VERSION=x.y.z).
release: guard-version
	@test -n "$(VERSION)" || { echo "ERROR: VERSION is required (make release VERSION=x.y.z)" >&2; exit 1; }
	./bump-version set $(VERSION)

## guard-gh: fail loudly if the GitHub CLI is unavailable.
guard-gh:
	@command -v gh >/dev/null 2>&1 || { echo "ERROR: gh (GitHub CLI) is required" >&2; exit 1; }

## apply-ruleset: create or update the ruleset from RULESET_FILE (ID resolved by name).
apply-ruleset: guard-gh
	@test -f "$(RULESET_FILE)" || { echo "ERROR: $(RULESET_FILE) not found" >&2; exit 1; }
	@name=$$(jq -r '.name' "$(RULESET_FILE)"); \
	id=$$(gh api "repos/$(REPO)/rulesets" --jq ".[] | select(.name == \"$$name\") | .id" | head -n1); \
	if [ -n "$$id" ]; then \
	  echo "Updating ruleset '$$name' (id $$id) in $(REPO)"; \
	  gh api --method PUT "repos/$(REPO)/rulesets/$$id" --input "$(RULESET_FILE)" >/dev/null; \
	else \
	  echo "Creating ruleset '$$name' in $(REPO)"; \
	  gh api --method POST "repos/$(REPO)/rulesets" --input "$(RULESET_FILE)" >/dev/null; \
	fi; \
	echo "Done."

## export-ruleset: overwrite RULESET_FILE with the live ruleset (read-only fields stripped).
export-ruleset: guard-gh
	@test -f "$(RULESET_FILE)" || { echo "ERROR: $(RULESET_FILE) not found" >&2; exit 1; }
	@name=$$(jq -r '.name' "$(RULESET_FILE)"); \
	id=$$(gh api "repos/$(REPO)/rulesets" --jq ".[] | select(.name == \"$$name\") | .id" | head -n1); \
	if [ -z "$$id" ]; then echo "ERROR: no ruleset named '$$name' in $(REPO)" >&2; exit 1; fi; \
	gh api "repos/$(REPO)/rulesets/$$id" \
	  | jq '{name, target, enforcement, conditions, bypass_actors, rules}' > "$(RULESET_FILE).tmp"; \
	mv "$(RULESET_FILE).tmp" "$(RULESET_FILE)"; \
	echo "Wrote $(RULESET_FILE) from ruleset '$$name' (id $$id)"

## help: list the developer-invocable targets.
help:
	@echo "Available targets:"
	@echo "  apply-ruleset  Create/update the branch protection ruleset (resolved by name)."
	@echo "  build          Build the container image tagged with the CONTAINER_VERSION."
	@echo "  export-ruleset Overwrite the ruleset JSON file from the live ruleset."
	@echo "  github-output  Print key=value lines for CI."
	@echo "  help           Show this help message."
	@echo "  release        Set VERSION and commit (make release VERSION=x.y.z)."
	@echo "  test           Run the test suite (uv run pytest tests/)."
	@echo "  version        Print the derived CONTAINER_VERSION."
