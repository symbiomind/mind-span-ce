"""
Bridge context XML assembly for mind-span-ce v0.2.

Assembles the <bridge_context> block from plugin contributions and injects it
into the working message list before forwarding to the backend.

These are pure functions — no state, no side effects, easily testable.
"""


def assemble(bridge_context: dict) -> str:
    """
    Assemble <bridge_context> XML from the bridge_context dict.

    Keys → <key>value</key>
    Keys starting with "_raw_" → value injected verbatim (no wrapping tag)

    Insertion order is preserved (Python 3.7+ dict). Plugins that run later
    in the pipeline can overwrite earlier entries by using the same key.

    Returns an empty string if bridge_context is empty (no injection needed).

    Examples:
        {"current_time": "Wednesday 1st April 2026"}
        → <bridge_context>\\n<current_time>Wednesday 1st April 2026</current_time>\\n</bridge_context>

        {"_raw_caller": '<caller trust="trusted">Martin</caller>'}
        → <bridge_context>\\n<caller trust="trusted">Martin</caller>\\n</bridge_context>
    """
    if not bridge_context:
        return ""

    parts = []
    for key, value in bridge_context.items():
        if key.startswith("_raw_"):
            parts.append(str(value))
        else:
            parts.append(f"<{key}>{value}</{key}>")

    return "<bridge_context>\n" + "\n".join(parts) + "\n</bridge_context>"


def inject_into_messages(messages: list[dict], bridge_xml: str) -> list[dict]:
    """
    Return a new message list with bridge_xml prepended to the first user message.

    Does NOT mutate the input list — returns a new list. The pipeline assigns
    the result back to ctx.request.messages, preserving ctx.request.original_messages.

    If bridge_xml is empty, returns the original list unchanged (no copy).

    If no user message exists, inserts a system message containing bridge_xml
    at index 0 so the context is always visible to the backend.
    """
    if not bridge_xml:
        return messages

    new_messages = list(messages)

    for i, msg in enumerate(new_messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            new_messages[i] = {**msg, "content": f"{bridge_xml}\n\n{content}"}
            return new_messages

    # No user message found — insert as a system message at the start
    new_messages.insert(0, {"role": "system", "content": bridge_xml})
    return new_messages
