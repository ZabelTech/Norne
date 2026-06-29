"""Tests for the two-layer instruction loader."""
import os
import tempfile
from orchestrator import config, instructions
from orchestrator.runners import parse_structured


def test_general_and_stage_merged_with_stage_after_general(tmp_path, monkeypatch):
    """Both layers present: general first, stage last with precedence header."""
    # Point config to temp dir
    monkeypatch.setattr(config, "INSTRUCTIONS_DIR", str(tmp_path))
    monkeypatch.setattr(config, "INSTRUCTIONS_INJECT_GENERAL", True)

    # Create general file in target repo
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    general_file = repo_path / "CLAUDE.md"
    general_file.write_text("# General guidance\n\nFollow these rules.")

    # Create stage file
    stage_file = tmp_path / "summarize" / "CLAUDE.md"
    stage_file.parent.mkdir(parents=True)
    stage_file.write_text("# Stage guidance\n\nSummarize specifically.")

    result = instructions.load("summarize", str(repo_path))
    assert "## GENERAL GUIDANCE" in result
    assert "# General guidance" in result
    assert "Follow these rules." in result
    assert "## STAGE-SPECIFIC GUIDANCE (summarize)" in result
    assert "# Stage guidance" in result
    assert "Summarize specifically." in result
    # Stage text comes after general text
    assert result.index("## STAGE-SPECIFIC GUIDANCE") > result.index("## GENERAL GUIDANCE")


def test_missing_general_and_missing_stage_returns_empty(tmp_path, monkeypatch):
    """Both layers missing: returns empty string."""
    monkeypatch.setattr(config, "INSTRUCTIONS_DIR", str(tmp_path))
    monkeypatch.setattr(config, "INSTRUCTIONS_INJECT_GENERAL", True)

    repo_path = tmp_path / "repo"
    repo_path.mkdir()

    result = instructions.load("summarize", str(repo_path))
    assert result == ""


def test_missing_repo_path_returns_only_stage(tmp_path, monkeypatch):
    """No repo_path provided: only stage layer is loaded."""
    monkeypatch.setattr(config, "INSTRUCTIONS_DIR", str(tmp_path))
    monkeypatch.setattr(config, "INSTRUCTIONS_INJECT_GENERAL", True)

    stage_file = tmp_path / "summarize" / "CLAUDE.md"
    stage_file.parent.mkdir(parents=True)
    stage_file.write_text("# Stage only")

    result = instructions.load("summarize", "")
    assert "## STAGE-SPECIFIC GUIDANCE (summarize)" in result
    assert "# Stage only" in result
    assert "GENERAL GUIDANCE" not in result


def test_spec_review_maps_to_spec_folder(tmp_path, monkeypatch):
    """spec_review step resolves to the spec folder."""
    monkeypatch.setattr(config, "INSTRUCTIONS_DIR", str(tmp_path))
    monkeypatch.setattr(config, "INSTRUCTIONS_INJECT_GENERAL", False)

    # Create spec folder file (not spec_review)
    stage_file = tmp_path / "spec" / "CLAUDE.md"
    stage_file.parent.mkdir(parents=True)
    stage_file.write_text("# Spec guidance")

    result = instructions.load("spec_review", None)
    assert "# Spec guidance" in result


def test_fix_maps_to_implement_folder(tmp_path, monkeypatch):
    """fix step resolves to the implement folder."""
    monkeypatch.setattr(config, "INSTRUCTIONS_DIR", str(tmp_path))
    monkeypatch.setattr(config, "INSTRUCTIONS_INJECT_GENERAL", False)

    # Create implement folder file (not fix)
    stage_file = tmp_path / "implement" / "CLAUDE.md"
    stage_file.parent.mkdir(parents=True)
    stage_file.write_text("# Implement guidance")

    result = instructions.load("fix", None)
    assert "# Implement guidance" in result


def test_non_utf8_bytes_returns_empty_string(tmp_path, monkeypatch):
    """Non-UTF-8 bytes in general and stage files: load() returns '' (str), no exception."""
    monkeypatch.setattr(config, "INSTRUCTIONS_DIR", str(tmp_path))
    monkeypatch.setattr(config, "INSTRUCTIONS_INJECT_GENERAL", True)

    repo_path = tmp_path / "repo"
    repo_path.mkdir()

    # Create non-UTF-8 general file
    general_file = repo_path / "CLAUDE.md"
    general_file.write_bytes(b'\xff\xfe Non-UTF-8 content')

    # Create non-UTF-8 stage file
    stage_file = tmp_path / "summarize" / "CLAUDE.md"
    stage_file.parent.mkdir(parents=True)
    stage_file.write_bytes(b'\xff\xfe Non-UTF-8 stage')

    result = instructions.load("summarize", str(repo_path))
    assert result == ""
    assert isinstance(result, str)


def test_instructions_inject_general_false_skips_general_layer(tmp_path, monkeypatch):
    """INSTRUCTIONS_INJECT_GENERAL=false: general text absent, stage text present."""
    monkeypatch.setattr(config, "INSTRUCTIONS_DIR", str(tmp_path))
    monkeypatch.setattr(config, "INSTRUCTIONS_INJECT_GENERAL", False)

    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    general_file = repo_path / "CLAUDE.md"
    general_file.write_text("# General guidance")

    stage_file = tmp_path / "summarize" / "CLAUDE.md"
    stage_file.parent.mkdir(parents=True)
    stage_file.write_text("# Stage guidance")

    result = instructions.load("summarize", str(repo_path))
    assert "GENERAL GUIDANCE" not in result
    assert "# General guidance" not in result
    assert "## STAGE-SPECIFIC GUIDANCE (summarize)" in result
    assert "# Stage guidance" in result


def test_fence_neutralization_removes_json_info_string(tmp_path, monkeypatch):
    """A CLAUDE.md containing a ```json fence -> the loaded output contains no ```json tag."""
    monkeypatch.setattr(config, "INSTRUCTIONS_DIR", str(tmp_path))
    monkeypatch.setattr(config, "INSTRUCTIONS_INJECT_GENERAL", False)

    stage_file = tmp_path / "summarize" / "CLAUDE.md"
    stage_file.parent.mkdir(parents=True)
    stage_file.write_text("# Example\n\n```json\n{\"key\": \"value\"}\n```\n\nEnd")

    result = instructions.load("summarize", None)
    # The fence opener should be neutralized to plain ```
    assert "```json\n" not in result
    assert "```\n" in result
    # Content survives
    assert '{"key": "value"}' in result
    assert "End" in result


def test_contract_protection_with_unclosed_json_fence_in_general(tmp_path, monkeypatch):
    """An unclosed ```json in the general layer does NOT break the contract."""
    monkeypatch.setattr(config, "INSTRUCTIONS_DIR", str(tmp_path))
    monkeypatch.setattr(config, "INSTRUCTIONS_INJECT_GENERAL", True)

    repo_path = tmp_path / "repo"
    repo_path.mkdir()

    # General layer with UNCLOSED ```json fence
    general_file = repo_path / "CLAUDE.md"
    general_file.write_text("# Guidance\n\n```json\n{\"sample\": \"data\"\n\nMore text")

    # Stage layer after it
    stage_file = tmp_path / "summarize" / "CLAUDE.md"
    stage_file.parent.mkdir(parents=True)
    stage_file.write_text("# Stage")

    # Load instructions
    instruction_text = instructions.load("summarize", str(repo_path))

    # Append a valid trailing ```json contract
    full_prompt = instruction_text + "\n\nEnd with exactly one json block:\n```json\n{\"status\": \"done\"}\n```"

    # parse_structured should return the contract dict, not {}
    contract_dict = {"status": "done"}
    result = parse_structured(full_prompt)
    assert result == contract_dict


def test_load_returns_str_not_none_on_missing_files(tmp_path, monkeypatch):
    """load() always returns a string, never None, even when files are missing."""
    monkeypatch.setattr(config, "INSTRUCTIONS_DIR", str(tmp_path))
    monkeypatch.setattr(config, "INSTRUCTIONS_INJECT_GENERAL", True)

    repo_path = tmp_path / "repo"
    repo_path.mkdir()

    result = instructions.load("summarize", str(repo_path))
    assert result == ""
    assert isinstance(result, str)


def test_stage_folder_missing_returns_only_general(tmp_path, monkeypatch):
    """Stage folder missing: only general layer is returned."""
    monkeypatch.setattr(config, "INSTRUCTIONS_DIR", str(tmp_path))
    monkeypatch.setattr(config, "INSTRUCTIONS_INJECT_GENERAL", True)

    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    general_file = repo_path / "CLAUDE.md"
    general_file.write_text("# General only")

    result = instructions.load("summarize", str(repo_path))
    assert "## GENERAL GUIDANCE" in result
    assert "# General only" in result
    assert "STAGE-SPECIFIC" not in result


def test_inject_general_none_defaults_to_config(tmp_path, monkeypatch):
    """inject_general=None uses config.INSTRUCTIONS_INJECT_GENERAL."""
    monkeypatch.setattr(config, "INSTRUCTIONS_INJECT_GENERAL", False)

    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    general_file = repo_path / "CLAUDE.md"
    general_file.write_text("# General")

    # Not passing inject_general, should default to config (False)
    result = instructions.load("summarize", str(repo_path), inject_general=None)
    assert "GENERAL GUIDANCE" not in result

    # Explicit True should override
    result = instructions.load("summarize", str(repo_path), inject_general=True)
    assert "GENERAL GUIDANCE" in result
