"""Central v1 memory prompt snippets.

Seven can replace this file's text blocks without changing the memory
execution code. Keep route A skills and route B hosted prompts semantically
aligned with these rules.
"""

# Canonical common buckets (A9) — ONE shared bilingual vocabulary so onboarding +
# capture + migration converge instead of each card minting a fresh near-synonym
# bucket (工作/职业/事业) or, with a weak model, all landing in 未分类. Tuned for a
# COMPANION app (not a notes tool): emotion / relationship / preferences / pets /
# values matter more than tidy productivity folders. NOT a hard taxonomy — the model
# still creates a specific bucket (妈妈 / 某个朋友) when none of these fit. Each pair
# is the SAME bucket in the two garden languages; never let 工作 and Work coexist —
# use the side matching the user's language. Seven can edit this list (and the
# guidance below) without touching execution code; everything downstream derives
# from it, so there's a single source of truth.
COMMON_BUCKETS_V1 = [
    ("工作", "Work"),
    ("目标与成长", "Goals & growth"),
    ("家庭", "Family"),
    ("朋友", "Friends"),
    ("宠物", "Pets"),
    ("我们的关系", "Our relationship"),
    ("情绪与安抚", "Feelings & comfort"),
    ("偏好与边界", "Preferences & boundaries"),
    ("个性与价值观", "Personality & values"),
    ("健康", "Health"),
    ("爱好", "Interests"),
    ("金钱", "Money"),
    ("饮食", "Food"),
    ("地点与旅行", "Places & travel"),
]

# Ready-to-inject bilingual line: 工作/Work、目标与成长/Goals & growth、… — short enough to fit every prompt.
COMMON_BUCKETS_LINE_V1 = "、".join(f"{zh}/{en}" for zh, en in COMMON_BUCKETS_V1)
# English-only list for the route-B guidance block below (kept in sync automatically).
_COMMON_BUCKETS_EN = " / ".join(en for _zh, en in COMMON_BUCKETS_V1)


MEMORY_WRITE_GUIDANCE_V1 = ("""
Memory write guidance:
- Write only durable user/relationship facts, preferences, boundaries, repeated patterns, or meaningful events.
- Do not write greetings, jokes, one-off task instructions, unconfirmed guesses, roleplay hypotheticals, or the assistant's own inference.
- Use memory.add for new durable events/facts.
- Use memory.supersede when the user corrects or replaces an older memory; do not patch old cards in place.
- Pick one bucket and 1-4 reusable threads. Prefer existing bucket/thread names when provided; converge on the common buckets ("""
+ _COMMON_BUCKETS_EN + """) and only mint a specific new bucket (Mom / the house) when none fit. Keep buckets in the user's language — never let 工作 and Work coexist.
- importance means future usefulness for understanding the user. pulse means emotional activation when remembered.
- content must use three Markdown sections: 记忆 / 上下文 / 使用提示.
- Do not claim "saved" or "remembered" before the backend write actually succeeds.
""").strip()


MEMORY_CONTEXT_FRAMING_V1 = """
Memory context framing:
- Ambient memories are background color. Use them to maintain continuity; do not force them into the reply as a topic.
- Fetched memories are evidence. Weave them naturally into the answer instead of reciting card text.
- Follow each card's 使用提示 when deciding tone, timing, and whether to mention the memory explicitly.
- If memory conflicts with the user's current message, trust the current message unless safety/privacy says otherwise.
- If memory is only weakly related, do not assert it as fact.
""".strip()


# Full bucket-convergence guidance injected into every card-creating prompt
# (capture / migrate / genesis) so onboarding and capture steer toward the same set.
COMMON_BUCKETS_GUIDANCE_V1 = (
    "桶名要收敛、可复用,别每张卡都新起一个近义桶。优先从这组通用桶里选并复用——\n"
    "  " + COMMON_BUCKETS_LINE_V1 + "\n"
    "中文记忆用左侧中文桶、英文记忆用右侧英文桶,同一个桶绝不中英并存(别让「工作」和「Work」同时出现)。"
    "这些都不贴合,再起一个简短的具体桶(如 妈妈、房子);别造「工作/职业/事业」这种近义重复桶。"
).strip()
