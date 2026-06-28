#!/usr/bin/env bash
# Create the labels the state machine uses. Run once against your repo.
# Requires the GitHub CLI: gh auth login, then:  ./scripts/setup_labels.sh owner/repo
set -euo pipefail
REPO="${1:?usage: setup_labels.sh owner/repo}"

mk() { gh label create "$1" --repo "$REPO" --color "$2" --description "$3" --force; }

# Trigger
mk "pipeline"          "0e8a16" "Hand this issue to the orchestrator"

# Flow state (exactly one at a time; the bot manages these)
mk "flow:summarize"    "fbca04" "Bot: drafting the summary"
mk "flow:clarify"      "fbca04" "Bot: waiting on your clarification reply"
mk "flow:approval"     "d93f0b" "Bot: waiting for your approval"
mk "flow:spec"         "1d76db" "Bot: writing specs + work items"
mk "flow:implement"    "1d76db" "Bot: implementing in a branch"
mk "flow:review"       "1d76db" "Bot: cross-model code review"
mk "flow:merge"        "0e8a16" "Bot: ready to merge"
mk "flow:done"         "ededed" "Bot: finished"

# Human signals (YOU add these)
mk "approve"           "0e8a16" "You: approve the summary -> start spec"
mk "human:resolved"    "5319e7" "You: answered a judgement call -> resume"

# Bot flags
mk "human:needed"      "b60205" "Bot: paused on a judgement call (your turn)"
mk "blocked:budget"    "c5def5" "Bot: all model pools tapped; will retry on reset"

echo "labels created on $REPO"
