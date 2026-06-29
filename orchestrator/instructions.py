"""Two-layer instruction loader: general (target repo CLAUDE.md) + per-stage.

Each LLM stage receives merged guidance:
- GENERAL layer: the target repo's CLAUDE.md, read at runtime from the per-issue checkout
- STAGE layer: per-stage CLAUDE.md files from instructions/<stage>/CLAUDE.md

The stage layer is appended AFTER the general layer so it is more authoritative on conflict.
Sub-steps reuse their parent's folder: spec_review -> spec, fix -> implement.
Missing files degrade to empty and never block a run.
"""

import os
import re
from . import config


_JSON_FENCE_PATTERN = re.compile(r"^```json\s*$", flags=re.MULTILINE)


def _read(path):
    """Read a file with strict UTF-8 decoding. Return '' on any error.

    This uses strict encoding so binary/corrupt CLAUDE.md files are skipped rather
    than injecting U+FFFD garbage. Never raises — missing or unreadable files degrade
    to empty guidance.
    """
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except (OSError, UnicodeDecodeError):
        return ""


def _neutralize_json_fences(text):
    """Rewrite ```json fence openers to plain ``` fences.

    This prevents instruction fences from satisfying runners._JSON_BLOCK, which would
    let an unclosed ```json in instruction text hijack the model's contract. The
    content survives; only the syntax-highlight hint is lost.
    """
    return _JSON_FENCE_PATTERN.sub("```", text)


def load(step, repo_path, inject_general=None):
    """Load and merge general + stage-specific instructions for one pipeline step.

    Args:
        step: The pipeline step key (e.g., "summarize", "spec", "implement", "review")
        repo_path: Path to the target repo checkout (for reading CLAUDE.md)
        inject_general: Whether to load the general layer; defaults to config.INSTRUCTIONS_INJECT_GENERAL

    Returns:
        The merged instruction text, or '' if both layers are empty/missing.
        Never raises; all I/O errors are swallowed and degrade to empty guidance.
    """
    # Resolve inject_general from config if not explicitly provided
    if inject_general is None:
        inject_general = config.INSTRUCTIONS_INJECT_GENERAL

    parts = []

    # General layer: target repo's CLAUDE.md (read at runtime per issue)
    if inject_general and repo_path:
        general_path = os.path.join(repo_path, "CLAUDE.md")
        general_text = _read(general_path)
        if general_text:
            general_text = _neutralize_json_fences(general_text)
            parts.append(f"## GENERAL GUIDANCE (from the target repo)\n\n{general_text}")

    # Stage layer: per-stage instructions from this orchestrator repo
    folder = config.INSTRUCTION_FOLDER_BY_STEP.get(step, step)
    stage_path = os.path.join(config.INSTRUCTIONS_DIR, folder, "CLAUDE.md")
    stage_text = _read(stage_path)
    if stage_text:
        stage_text = _neutralize_json_fences(stage_text)
        parts.append(f"## STAGE-SPECIFIC GUIDANCE ({step}) — overrides general on conflict\n\n{stage_text}")

    return "\n\n---\n\n".join(parts) if parts else ""
