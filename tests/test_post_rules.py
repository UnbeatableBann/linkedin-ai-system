"""
tests/test_post_rules.py
─────────────────────────
Tests for LinkedIn post rules enforcement.

Covers:
  - Preamble stripping
  - Wrapper stripping
  - Character limit truncation
  - Blank line normalisation
  - check_post_length metadata
"""

from app.content.post_rules import LINKEDIN_MAX_CHARS, check_post_length, enforce_post_rules


class TestPreambleStripping:
    def test_strips_heres_your_post(self):
        content = "Here's your post:\n\nActual post content here."
        result = enforce_post_rules(content)
        assert result == "Actual post content here."

    def test_strips_sure_here(self):
        content = "Sure! Here's a LinkedIn post for you:\n\nReal content."
        result = enforce_post_rules(content)
        assert result == "Real content."

    def test_strips_of_course(self):
        content = "Of course, here's a draft:\n\nPost body."
        result = enforce_post_rules(content)
        assert result == "Post body."

    def test_strips_ive_written(self):
        content = "I've written a post for you:\n\nThe actual post."
        result = enforce_post_rules(content)
        assert result == "The actual post."

    def test_does_not_strip_real_opening(self):
        content = "Most product launches fail.\n\nHere's why..."
        result = enforce_post_rules(content)
        assert result.startswith("Most product launches fail.")

    def test_does_not_strip_short_posts(self):
        content = "Short post content."
        result = enforce_post_rules(content)
        assert result == "Short post content."

    def test_handles_empty_string(self):
        assert enforce_post_rules("") == ""

    def test_handles_whitespace_only(self):
        assert enforce_post_rules("   \n\n  ") == ""


class TestWrapperStripping:
    def test_strips_double_quotes(self):
        content = '"This is the post content."'
        result = enforce_post_rules(content)
        assert result == "This is the post content."

    def test_strips_single_quotes(self):
        content = "'Post content here.'"
        result = enforce_post_rules(content)
        assert result == "Post content here."

    def test_does_not_strip_quotes_in_middle(self):
        content = 'He said "this is important" and I agree.'
        result = enforce_post_rules(content)
        assert '"this is important"' in result

    def test_strips_code_fence(self):
        content = "```\nPost content inside fence.\n```"
        result = enforce_post_rules(content)
        assert result == "Post content inside fence."


class TestCharacterLimit:
    def test_short_content_unchanged(self):
        content = "A" * 100
        result = enforce_post_rules(content)
        assert len(result) <= 100

    def test_content_at_limit_unchanged(self):
        content = "A" * LINKEDIN_MAX_CHARS
        result = enforce_post_rules(content)
        assert len(result) <= LINKEDIN_MAX_CHARS

    def test_content_over_limit_truncated(self):
        # Build content longer than limit with real sentences
        sentences = "This is a sentence about professional growth. " * 100
        result = enforce_post_rules(sentences)
        assert len(result) <= LINKEDIN_MAX_CHARS

    def test_truncation_at_sentence_boundary(self):
        # Create content with clear sentence boundaries
        base = "First sentence here. Second sentence here. Third sentence here. "
        long_content = base * 100
        result = enforce_post_rules(long_content)
        assert len(result) <= LINKEDIN_MAX_CHARS
        # Should end at a sentence boundary, not mid-word
        assert not result.endswith(" ")


class TestBlankLineNormalisation:
    def test_triple_blank_lines_reduced(self):
        content = "Line one.\n\n\n\nLine two."
        result = enforce_post_rules(content)
        assert "\n\n\n" not in result

    def test_double_blank_lines_preserved(self):
        content = "Line one.\n\nLine two."
        result = enforce_post_rules(content)
        assert "Line one." in result
        assert "Line two." in result


class TestCheckPostLength:
    def test_short_post(self):
        info = check_post_length("Short post.")
        assert info["chars"] == len("Short post.")
        assert info["over_limit"] is False
        assert info["near_limit"] is False
        assert info["words"] > 0

    def test_over_limit_post(self):
        long_content = "word " * 1000
        info = check_post_length(long_content)
        assert info["over_limit"] is True
        assert info["remaining"] == 0

    def test_near_limit_post(self):
        near_limit = "a " * 1450  # ~2900 chars
        info = check_post_length(near_limit)
        assert info["near_limit"] is True
        assert info["over_limit"] is False

    def test_word_count_accurate(self):
        content = "one two three four five"
        info = check_post_length(content)
        assert info["words"] == 5


class TestEdgeCases:
    def test_only_newlines(self):
        result = enforce_post_rules("\n\n\n")
        assert result == ""

    def test_unicode_content(self):
        content = "Growing as a founder in India 🇮🇳\n\nThree lessons from 2024."
        result = enforce_post_rules(content)
        assert "India" in result
        assert "lessons" in result

    def test_hashtags_preserved(self):
        content = "Great post content here.\n\n#LinkedInGrowth #Startup #India"
        result = enforce_post_rules(content)
        assert "#LinkedInGrowth" in result

    def test_multiline_preamble(self):
        content = "Here's your post:\n\nFirst real line.\nSecond line."
        result = enforce_post_rules(content)
        assert result.startswith("First real line.")
