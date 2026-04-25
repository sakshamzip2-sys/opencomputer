-- 003: T2.4 — flag prompt-edit proposals that would invalidate Anthropic
-- prompt cache (system prompt or tool spec mid-session edits).
ALTER TABLE prompt_proposals ADD COLUMN cache_invalidation_warning INTEGER NOT NULL DEFAULT 0;
