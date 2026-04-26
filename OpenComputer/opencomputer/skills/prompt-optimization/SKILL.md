---
name: prompt-optimization
description: Use when crafting or refining LLM prompts, system instructions, or chat templates for better quality and lower cost
---

# Prompt Optimization

## When to use

- Designing a new system prompt for an agent / chatbot
- Cutting tokens on a hot prompt that's expensive
- Debugging "why does the model keep doing X?" failures

## Steps

1. **State the goal in one sentence.** If you can't, the prompt is wrong before you write it.
2. **Show, don't tell.** Two examples > one paragraph of instruction. Few-shot beats verbose instructions for format/style/tone.
3. **Order matters.** Put critical instructions at the start AND end (recency bias is real). Middle gets ignored on long prompts.
4. **Negative space.** "Do not include X" rarely works on LLMs. Phrase as "respond using only Y" instead.
5. **Token cost audit.** Count tokens with `tiktoken` (OpenAI) or the SDK's tokenizer (Anthropic). Cut anything that doesn't move quality.
6. **Eval before ship.** Pick 5-10 representative inputs. Diff outputs old vs new prompt. Quality changes you didn't intend → roll back.

## Notes

- "Think step by step" is overrated; specify *what* to think about (e.g. "first identify the user's intent, then…").
- Caching beats re-prompting. If a prompt is reused, cache the prefix.
- If the model ignores rules, the rule probably conflicts with another rule. Surface the contradiction.
