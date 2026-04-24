# Writing a skill description that triggers well

The `description` field is the retrieval query the agent matches against
the user's intent. A bad description means the skill never fires (or
fires on the wrong turn). A good description fires exactly when you
want it to and never when you don't.

## The rules

1. **Verbs first.** Start with the action — "Use when", "Triggers on",
   "Fires when". Nouns are weak retrieval anchors.
2. **Include synonyms.** List all the ways a user might phrase the same
   intent. "error", "crash", "failure", "broken", "not working".
3. **Include exact error strings.** If the skill handles a specific
   traceback, paste the exact message into the description.
4. **Cap at ~400 chars.** Longer descriptions get truncated by some
   retrieval layers and the tail is wasted.
5. **Don't describe what the skill DOES.** Describe when the agent
   should CALL it. "When the user X" not "Teaches you to X".

## Five good descriptions

**1. Debug Python import error** (shipped):

> "Use when the user hits a ModuleNotFoundError, ImportError, ImportError
> when running a Python script, circular import, 'no module named X', or
> asks about fixing a broken Python import."

Why it works: lists concrete error strings, includes user phrasing
("fixing a broken import"), covers multiple synonymous conditions.

**2. A VCP screener trigger**:

> "Screen S&P 500 stocks for Mark Minervini's Volatility Contraction
> Pattern. Use when the user asks about VCP setups, volatility
> contraction, base formations, breakout candidates, or tight
> consolidation patterns."

Why it works: opens with the canonical name, lists aliases and
adjacent terms ("base formations", "breakout candidates"), makes the
use case unambiguous.

**3. Frontmatter-style**:

> "This skill should be used when the user asks to 'create a plugin',
> 'scaffold a plugin', 'understand plugin structure', 'organize plugin
> components', or needs guidance on plugin directory layout, manifest
> configuration, or plugin architecture."

Why it works: uses the canonical "This skill should be used when..."
prefix that some retrieval layers expect; quotes the literal phrases
users type.

**4. Conventional commits**:

> "Use when the user is about to commit, asks to 'write a commit
> message', mentions 'conventional commits', or needs help picking a
> commit type (feat, fix, chore, refactor, docs, test)."

Why it works: captures the decision moment ("about to commit"), lists
the vocabulary, enumerates the tag set the user might search for.

**5. Security review**:

> "Use this skill when adding authentication, handling user input,
> parsing JSON from untrusted sources, writing SQL, exposing a new
> endpoint, or the user says 'security review', 'auth flow', or 'threat
> model'."

Why it works: triggers on behavior patterns the agent can infer from
what it's editing, plus explicit user phrases.

## Five bad descriptions (and why)

**1. "A skill for handling imports."**

Too abstract. Doesn't tell the retriever what "handling" means or when
it's wanted. Fails to fire on a user who says "fix this import".

**2. "Teaches you Python import best practices."**

Describes the skill, not the trigger. "Teach me about imports" is a
user query; "fix this import" is a different user query. This
description matches the former but fails the latter.

**3. "Imports, modules, packages, Python, dependencies."**

Keyword soup. No verbs, no context. Matches too widely and fires on
unrelated messages mentioning imports in passing.

**4. "The import error debugger skill for Python 3.12+ on darwin and
linux, supporting editable installs, circular detection, and sys.path
manipulation with useful diagnostic output via the python -m
inspection flag."**

Too long. The tail is probably truncated by the retriever. Information-
dense but hard to match because the keywords are diluted.

**5. "Use when needed."**

Zero signal. The retriever can't tell when it's "needed" — that's its
whole job.

## Test your description by reading it in isolation

Before you ship, read ONLY the description (not the skill body) and
ask yourself:

- If I were the agent, would I know when to fire this?
- What user phrases would match it?
- Are there synonyms I forgot?

If you can think of three user queries that should fire this skill and
they don't appear (even as synonyms) in the description, add them.

## Do not duplicate content in the description

The description is for retrieval. The body is for execution. If you
find yourself writing instructions ("first do X, then do Y") in the
description, you're doing it wrong. Move that to the body and leave the
description as pure trigger text.
