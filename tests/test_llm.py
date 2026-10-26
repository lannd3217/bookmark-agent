from types import SimpleNamespace

import pytest
from anthropic.types import TextBlock, ThinkingBlock, ToolUseBlock

from app.llm import response_text


def message(*blocks, stop_reason="end_turn"):
    """A stand-in for a Messages API response, with real SDK content blocks."""
    return SimpleNamespace(content=list(blocks), stop_reason=stop_reason)


def text(t):
    return TextBlock(type="text", text=t)


def thinking():
    # Adaptive thinking's default display: the thinking text comes back empty.
    return ThinkingBlock(type="thinking", thinking="", signature="sig")


def test_thinking_block_then_text_returns_the_text():
    assert response_text(message(thinking(), text('{"ok": true}'))) == '{"ok": true}'


def test_two_text_blocks_are_joined_in_order():
    assert response_text(message(text('{"a": '), thinking(), text("1}"))) == '{"a": 1}'


def test_no_text_block_raises_naming_types_and_stop_reason():
    msg = message(thinking(), ToolUseBlock(type="tool_use", id="t1", name="search", input={}), stop_reason="tool_use")
    with pytest.raises(ValueError) as exc:
        response_text(msg)
    assert "thinking, tool_use" in str(exc.value) and "tool_use" in str(exc.value).split("stop_reason:")[1]


def test_empty_content_raises():
    with pytest.raises(ValueError, match="block types: none"):
        response_text(message(stop_reason="end_turn"))


def test_max_tokens_raises_even_with_partial_text():
    with pytest.raises(ValueError) as exc:
        response_text(message(thinking(), text('{"models": [{"name": "Feat'), stop_reason="max_tokens"))
    assert str(exc.value) == "The response was cut off before it finished; raise max_tokens."
