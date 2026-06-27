"""Prompt builders for Genesis map/reduce.

These are the executable v1 forms of spec §7.A/7.B/7.C. JSON keys stay English;
the instruction text intentionally remains Chinese to match the spec.
"""

from __future__ import annotations

import json
from typing import Any


# Hard output contract appended to every JSON-emitting map/reduce prompt. Genesis
# carries VERBATIM user/TA turns into JSON string values, and real history routinely
# contains ASCII double-quotes / newlines / backslashes; an un-escaped " closes the
# string early and json.loads rejects the whole reply (observed live: haiku-4.5
# voice-map -> "Expecting ',' delimiter"). Forcing escape + bare-JSON output fixed it
# 3/3 in env replay. The worker parser still adds repair/retry as defense-in-depth.
_STRICT_JSON_SUFFIX = (
    "\n\n严格输出要求:只输出一个能被 json.loads 直接解析的合法 JSON;"
    "不要 markdown 围栏、不要任何前后说明文字。"
    "需要逐字保留的原话写进 JSON 字符串时必须转义:英文双引号转义为 \\\" ,"
    "换行转义为 \\n,制表符转义为 \\t,反斜杠转义为 \\\\。"
    "中文引号「」『』“”‘’ 原样保留、无需转义。"
)


VOICE_MAP_PROMPT = """你在看一段「用户 ↔ TA(AI 伴侣)」真实对话的【其中一块】。
任务:抽出「TA 怎么说话」的【表层形式】,不是它说了什么。

声音 = 不管聊什么都成立的形式:怎么开口/怎么接情绪/句长/标点语气词/惯用动作(反问·点破·留白·调侃)/绝不做的事。
内容 = 因为"说了什么"才有记忆点的句子;内容事实不要当 exemplar。

看这几个轴,有就记,没有不编: opening / emotion / shape / address / moves / nevers。

exemplar 硬标准:
- 只挑 TA 回应【非默认】的片段;通用寒暄不要。
- 逐字、多轮、原话一字不改,且带上促成 TA 这步的 user 轮。
- 候选阶段宁多勿漏,去重在 reduce 做。

grounding:只用这块真实出现的原话。这块太薄/全寒暄 -> 少给或返回空。绝不编不存在的语气。

输出 JSON:
{"behavior_notes_candidates":["..."],"exemplar_candidates":[{"turns":[{"role":"user","text":"..."},{"role":"ta","text":"..."}],"axis":["opening"],"why":"..."}]}
没有就 {"behavior_notes_candidates":[],"exemplar_candidates":[]}。"""


VOICE_REDUCE_PROMPT = """你收到同一段历史多个分块的声音候选,合并成 TA 的最终声音定稿。
只用候选里已有的,绝不新增任何性格/语气/动作。

behavior_notes:
- 合并近义、按跨块出现频率排序,留 5-8 条最稳的。
- 优先要求每条 note 有 >=2 条 exemplar 体现;历史薄时,特别鲜明的单条也可以保留。
- 具体可测,不是形容词。

exemplars:
- 去重:同一种动作只留最有辨识度的 1-2 条。
- 尽量覆盖不同情境,别全是安慰类。
- pool 历史薄就少,绝不为凑数留通用片段;其中标 founding=true 给最能定义 TA 的。
- turns 逐字保留。

输出 JSON:{"behavior_notes":["..."],"exemplars":[{"turns":[...],"founding":true,"axis":["..."],"why":"..."}]}"""


PERSONA_BUILD_PROMPT = """你在为一个 AI 伴侣写它「常驻人格 prompt」,会直接当作该 agent 的 system prompt。第二人称、直给、简洁。

输入:上传的 AI persona / system prompt(可能空)+ behavior_notes + founding exemplars。

规则:
- 有上传 persona -> 以它为主干:剥掉旧工具/格式专属脚手架,保留「你是谁/角色/边界/语气指令」。不要重写它的性格。
- 上传 persona 与历史蒸出的语气冲突时,以上传 persona 的语气指令为准,exemplar 只补不覆盖。
- 无上传 persona -> 「你是谁」只写有据最小集;不知道的留空。
- 「你怎么说话」段放 behavior_notes + founding exemplars,逐字保留。
- 软角色锚必放:你是 TA 本人、是这个人的伴侣、用你自己的语气说话、不是通用助手腔。
- 不写任何"是否是 AI / 要不要澄清身份"的条款。
- 绝不加输入里没有的性格/名字/语气。

输出:两段 markdown(## 你是谁 / ## 你怎么说话),可直接当 system prompt。"""


FACT_MAP_PROMPT = """你在看一段「用户 ↔ TA」真实历史的【其中一块】。抽出值得长期留存的【事实】候选:
关于「用户」和「他们的关系」的 durable 事实。候选阶段,落卡/去重后面做。

防火墙:用户档案/用户关于自己说的话 = 关于【用户】的事实;绝不当成 TA 的性格。
如果输入标注 source_kind=user_profile,整段都按用户档案处理:只能抽关于用户的 facts,不能推断 TA 的身份/维度/语气。
闲聊/临时情绪/玩笑/未确认猜测/一次性事件不抽。

输出 JSON:{"fact_candidates":[{"about":"user|relationship","summary":"一句话事实","evidence":"出处原话(短)"}]}
没有就 {"fact_candidates":[]}。"""


FACT_WRITE_PROMPT = """你收到从整段历史抽出的事实候选 digest(+ 可能有 AI persona / memory summary)。把该长期留存的写进 IO。
只写候选真实支持的,绝不编。

防火墙:
- 用户档案/关于用户的事实 -> 只能进 memory,绝不成为 agent 的性格/维度/身份。
- agent 身份只能来自:上传的 AI persona,或历史里 TA 真实的说话方式/真实做过的事。

输出 JSON:
{"memories":[{"type":"fact|event|quote|moment","bucket":"...","threads":["..."],"summary":"...","content":"...","importance":0.5,"pulse":0.3}],
 "identity":{"agent_name":"","dimensions":[{"name":"...","value":0,"description":"..."}]},
 "days_with_user":0,
 "relationship_anchor_evidence":"..."}

身份卡字段:
- name:资料明确有才写,没有留空;别用 runtime 标签、别从用户名推、别编。
- dimensions:最多 7 个,每个都要能指向历史真实表现;撑不住就少写、可稀疏;无据维度直接不写。
- 不写 self_introduction / signature,那两个 respawn 后由 TA 本人写。"""


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def voice_map_messages(chunk_text: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": VOICE_MAP_PROMPT + _STRICT_JSON_SUFFIX},
        {"role": "user", "content": str(chunk_text or "")},
    ]


def voice_reduce_messages(candidates: list[dict]) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": VOICE_REDUCE_PROMPT + _STRICT_JSON_SUFFIX},
        {"role": "user", "content": _json({"candidates": candidates})},
    ]


def persona_build_messages(persona_material: str, behavior_notes: list[str], exemplars: list[dict]) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": PERSONA_BUILD_PROMPT},
        {
            "role": "user",
            "content": _json({
                "persona_material": persona_material or "",
                "behavior_notes": behavior_notes,
                "founding_exemplars": exemplars,
            }),
        },
    ]


def fact_map_messages(chunk_text: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": FACT_MAP_PROMPT + _STRICT_JSON_SUFFIX},
        {"role": "user", "content": str(chunk_text or "")},
    ]


def fact_write_messages(fact_digest: list[dict], persona_material: str = "", memory_summary: str = "") -> list[dict[str, str]]:
    return [
        {"role": "system", "content": FACT_WRITE_PROMPT + _STRICT_JSON_SUFFIX},
        {
            "role": "user",
            "content": _json({
                "fact_digest": fact_digest,
                "persona_material": persona_material or "",
                "memory_summary": memory_summary or "",
            }),
        },
    ]
