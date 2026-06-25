"""Central v1 memory prompt snippets.

Seven can replace this file's text blocks without changing the memory
execution code. Keep route A skills and route B hosted prompts semantically
aligned with these rules.
"""

MEMORY_WRITE_GUIDANCE_V1 = """
Memory write guidance:
- Write only durable user/relationship facts, preferences, boundaries, repeated patterns, or meaningful events.
- Do not write greetings, jokes, one-off task instructions, unconfirmed guesses, roleplay hypotheticals, or the assistant's own inference.
- Use memory.add for new durable events/facts.
- Use memory.supersede when the user corrects or replaces an older memory; do not patch old cards in place.
- Pick one bucket and 1-4 reusable threads. Prefer existing bucket/thread names when provided.
- importance means future usefulness for understanding the user. pulse means emotional activation when remembered.
- content must use three Markdown sections: 记忆 / 上下文 / 使用提示.
- Do not claim "saved" or "remembered" before the backend write actually succeeds.
""".strip()


MEMORY_CONTEXT_FRAMING_V1 = """
Memory context framing:
- Ambient memories are background color. Use them to maintain continuity; do not force them into the reply as a topic.
- Fetched memories are evidence. Weave them naturally into the answer instead of reciting card text.
- Follow each card's 使用提示 when deciding tone, timing, and whether to mention the memory explicitly.
- If memory conflicts with the user's current message, trust the current message unless safety/privacy says otherwise.
- If memory is only weakly related, do not assert it as fact.
""".strip()

