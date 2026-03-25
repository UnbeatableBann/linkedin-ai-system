"""
tests/test_style_memory.py
───────────────────────────
Tests for style signal extraction and preference merging.
Pure unit tests — no DB, no network.
"""

import pytest
from app.content.style_memory import (
    extract_style_signals,
    merge_style_prefs,
    build_style_context,
)


class TestExtractStyleSignals:
    def test_word_count(self):
        content = "one two three four five"
        signals = extract_style_signals(content)
        assert signals["word_count"] == 5

    def test_hashtag_extraction(self):
        content = "Great post about #LinkedIn and #Growth in #India"
        signals = extract_style_signals(content)
        assert signals["hashtag_count"] == 3
        assert "#LinkedIn" in signals["hashtags_sample"]

    def test_no_hashtags(self):
        content = "A post with no hashtags at all."
        signals = extract_style_signals(content)
        assert signals["hashtag_count"] == 0
        assert signals["hashtags_sample"] == []

    def test_ends_with_question(self):
        content = "Great thoughts here.\n\nWhat do you think?"
        signals = extract_style_signals(content)
        assert signals["ends_with_question"] is True

    def test_no_question(self):
        content = "Here are my three lessons from this year."
        signals = extract_style_signals(content)
        assert signals["ends_with_question"] is False

    def test_numbered_list_detected(self):
        content = "Three things I learned:\n\n1. Be consistent\n2. Add value\n3. Engage daily"
        signals = extract_style_signals(content)
        assert signals["uses_numbered_list"] is True

    def test_no_numbered_list(self):
        content = "I want to share something I learned this year about growth."
        signals = extract_style_signals(content)
        assert signals["uses_numbered_list"] is False

    def test_bullet_points_detected(self):
        content = "Key points:\n\n• First point\n• Second point\n• Third point"
        signals = extract_style_signals(content)
        assert signals["uses_bullets"] is True

    def test_emoji_detection(self):
        content = "Growing in India 🇮🇳\n\nThree lessons 🚀"
        signals = extract_style_signals(content)
        assert signals["uses_emojis"] is True
        assert signals["emoji_count"] >= 2

    def test_no_emoji(self):
        content = "A plain post without any emojis whatsoever."
        signals = extract_style_signals(content)
        assert signals["uses_emojis"] is False
        assert signals["emoji_count"] == 0

    def test_short_hook(self):
        content = "Most founders get this wrong.\n\nHere is why I think so."
        signals = extract_style_signals(content)
        assert signals["short_hook"] is True
        assert signals["hook_length"] <= 12

    def test_long_hook(self):
        content = "I have been thinking a lot about the way startup founders approach growth in the Indian market.\n\nHere is what I found."
        signals = extract_style_signals(content)
        assert signals["short_hook"] is False

    def test_paragraph_count(self):
        content = "Para 1.\n\nPara 2.\n\nPara 3."
        signals = extract_style_signals(content)
        assert signals["paragraph_count"] == 3


class TestMergeStylePrefs:
    def test_first_post_creates_prefs(self):
        prefs = merge_style_prefs({}, {"word_count": 300, "hashtag_count": 3})
        assert prefs["posts_learned"] == 1
        assert prefs["avg_word_count"] == 300.0

    def test_rolling_average(self):
        prefs = {}
        prefs = merge_style_prefs(prefs, {"word_count": 200, "hashtag_count": 2})
        prefs = merge_style_prefs(prefs, {"word_count": 400, "hashtag_count": 4})
        assert prefs["posts_learned"] == 2
        assert prefs["avg_word_count"] == 300.0  # (200 + 400) / 2
        assert prefs["avg_hashtag_count"] == 3.0  # (2 + 4) / 2

    def test_boolean_rate_tracking(self):
        prefs = {}
        prefs = merge_style_prefs(prefs, {"ends_with_question": True})
        prefs = merge_style_prefs(prefs, {"ends_with_question": True})
        prefs = merge_style_prefs(prefs, {"ends_with_question": False})
        # 2/3 posts end with question
        assert abs(prefs["rate_ends_with_question"] - 2/3) < 0.01

    def test_example_posts_stored(self):
        prefs = {}
        prefs = merge_style_prefs(prefs, {}, example_post="First post content here.")
        assert len(prefs["example_posts"]) == 1
        assert "First post content here." in prefs["example_posts"][0]

    def test_example_posts_capped_at_3(self):
        prefs = {}
        for i in range(5):
            prefs = merge_style_prefs(prefs, {}, example_post=f"Post number {i}")
        assert len(prefs["example_posts"]) == 3

    def test_example_posts_are_recent(self):
        """Should keep the 3 most recent posts, not the oldest."""
        prefs = {}
        for i in range(5):
            prefs = merge_style_prefs(prefs, {}, example_post=f"Post {i}")
        # Should have posts 2, 3, 4 (last 3)
        assert "Post 2" in prefs["example_posts"]
        assert "Post 3" in prefs["example_posts"]
        assert "Post 4" in prefs["example_posts"]
        assert "Post 0" not in prefs["example_posts"]

    def test_length_preference_short(self):
        prefs = merge_style_prefs({}, {"word_count": 100})
        assert "short" in prefs["preferred_length"]

    def test_length_preference_medium(self):
        prefs = merge_style_prefs({}, {"word_count": 250})
        assert "medium" in prefs["preferred_length"]

    def test_length_preference_long(self):
        prefs = merge_style_prefs({}, {"word_count": 500})
        assert "long" in prefs["preferred_length"]

    def test_empty_signals(self):
        """Should not crash on empty signals dict."""
        prefs = merge_style_prefs({}, {})
        assert prefs["posts_learned"] == 1

    def test_existing_prefs_preserved(self):
        """User-set prefs like tone should not be overwritten."""
        existing = {"tone": "storytelling", "audience": "founders"}
        prefs = merge_style_prefs(existing, {"word_count": 300})
        assert prefs["tone"] == "storytelling"
        assert prefs["audience"] == "founders"


class TestBuildStyleContext:
    def test_empty_prefs_returns_empty(self):
        result = build_style_context({})
        assert result == ""

    def test_zero_posts_returns_empty(self):
        result = build_style_context({"posts_learned": 0})
        assert result == ""

    def test_single_post_returns_empty(self):
        """Need at least 2 posts before showing style context."""
        result = build_style_context({"posts_learned": 1})
        assert result == ""

    def test_two_posts_returns_context(self):
        prefs = {
            "posts_learned": 2,
            "preferred_length": "medium (around 300 words)",
            "avg_hashtag_count": 3.0,
            "rate_ends_with_question": 0.8,
            "rate_short_hook": 0.9,
            "rate_uses_emojis": 0.1,
        }
        result = build_style_context(prefs)
        assert "2 previous posts" in result
        assert "medium" in result
        assert "3 hashtags" in result
        assert "question" in result

    def test_lists_mentioned_when_common(self):
        prefs = {
            "posts_learned": 3,
            "rate_uses_numbered_list": 0.8,
            "rate_uses_emojis": 0.1,
        }
        result = build_style_context(prefs)
        assert "numbered" in result.lower() or "list" in result.lower()

    def test_emoji_rarely_when_low_rate(self):
        prefs = {
            "posts_learned": 4,
            "rate_uses_emojis": 0.2,
        }
        result = build_style_context(prefs)
        assert "rarely" in result.lower() or "text-only" in result.lower()
