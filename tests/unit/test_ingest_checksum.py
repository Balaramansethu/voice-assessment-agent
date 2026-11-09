"""Pure helper coverage for KB ingest checksum semantics."""

from app.rag.ingest import _checksum, parse_frontmatter


BODY = "This body text is byte-identical between both inputs.\n"

CANDIDATE_VISIBLE = f"""---
type: rubric
visibility: candidate
title: Frontmatter Checksum
---
{BODY}"""

INTERNAL_VISIBLE = CANDIDATE_VISIBLE.replace(
    "visibility: candidate",
    "visibility: internal",
)


def test_full_file_checksum_changes_when_only_frontmatter_changes() -> None:
    assert _checksum(CANDIDATE_VISIBLE) != _checksum(INTERNAL_VISIBLE)


def test_checksum_is_stable_for_same_input_bytes() -> None:
    assert _checksum(CANDIDATE_VISIBLE) == _checksum(CANDIDATE_VISIBLE)
    assert _checksum(INTERNAL_VISIBLE) == _checksum(INTERNAL_VISIBLE)


def test_parse_frontmatter_returns_metadata_and_body_for_full_file_inputs() -> None:
    candidate_meta, candidate_body = parse_frontmatter(CANDIDATE_VISIBLE)
    internal_meta, internal_body = parse_frontmatter(INTERNAL_VISIBLE)

    assert candidate_meta["visibility"] == "candidate"
    assert internal_meta["visibility"] == "internal"
    assert candidate_meta["type"] == internal_meta["type"] == "rubric"
    assert candidate_meta["title"] == internal_meta["title"] == "Frontmatter Checksum"
    assert candidate_body == internal_body == BODY
    assert CANDIDATE_VISIBLE != INTERNAL_VISIBLE
