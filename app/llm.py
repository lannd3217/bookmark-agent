"""Reading text out of Claude API responses."""


def response_text(msg) -> str:
    """The text of every text block in a Messages API response, joined in order.

    A response's content can hold other blocks too: with adaptive thinking (on by default
    on current models) a thinking block usually comes first, so content[0] isn't
    necessarily text. Raises ValueError if the response hit max_tokens or has no text.
    """
    if msg.stop_reason == "max_tokens":
        raise ValueError("The response was cut off before it finished; raise max_tokens.")
    texts = [block.text for block in msg.content if block.type == "text"]
    if not texts:
        types = ", ".join(block.type for block in msg.content) or "none"
        raise ValueError(f"The response has no text block (block types: {types}; stop_reason: {msg.stop_reason})")
    return "".join(texts)
