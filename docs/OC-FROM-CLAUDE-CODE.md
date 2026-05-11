 # OC Feature Gaps: What to Take from Claude Code (Anthropic)                                                           
                                                                                                                        
 Deep analysis of Claude Code's architecture, hooks system, skills model, and context management                        
 vs what OC ships today. Source: Anthropic official docs, DeepWiki analysis, Claude Code changelog.                     
                                                                                                                        
 ---                                                                                                                    
                                                                                                                        
 ## TIER 1 - Build These Now (Highest Impact)                                                                           
                                                                                                                        
 ---                                                                                                                    
                                                                                                                        
 ### 1. Prompt Caching - Best-of-Both (Anthropic Native + OpenClaw Config)                                              
                                                                                                                        
 **What Anthropic does (the right way):**                                                                               
 Native API-level caching. Two modes:                                                                                   
                                                                                                                        
 **Automatic caching** (simplest - add one field):                                                                      
 ```json                                                                                                                
 {                                                                                                                      
   "model": "claude-opus-4-7",                                                                                          
   "cache_control": { "type": "ephemeral" }                                                                             
 }                                                                                                                      
                                                                                                                        

The system automatically applies the cache breakpoint to the last cacheable block and moves it forward as the           
conversation grows. Each new request caches everything up to the last cacheable block; previous content is read from    
cache.                                                                                                                  

Explicit breakpoints (fine-grained): Place cache_control on individual content blocks - tools, system prompt, messages -
at different TTLs.                                                                                                      

Cache prefix order: tools -> system -> messages. Up to 4 breakpoints. 5-min TTL (default) or 1-hour TTL (2x cost). Cache
reads are 0.1x the base input token price.                                                                              

What OpenClaw adds on top: Per-model cache config in agent settings:                                                    

                                                                                                                        
 {                                                                                                                      
   "agents": {                                                                                                          
     "defaults": {                                                                                                      
       "models": {                                                                                                      
         "anthropic/claude-opus-4-5": {                                                                                 
           "params": {                                                                                                  
             "cacheControlTtl": "1h",                                                                                   
             "cacheRetention": "short"                                                                                  
           }                                                                                                            
         }                                                                                                              
       }                                                                                                                
     }                                                                                                                  
   }                                                                                                                    
 }                                                                                                                      
                                                                                                                        

Also: moving the date out of the system prompt to improve cache hit rates (OC already does this partially).             

What OC should do:                                                                                                      

 • Verify OC is passing cache_control on system prompt AND tools (not just messages)                                    
 • Add cacheControlTtl config per model: "5m" or "1h" depending on session length                                       
 • Move volatile content (date, cwd, git status) to the END of system prompt so the static prefix caches cleanly        
 • For heartbeat runs: use lightContext equivalent - minimal system prompt so cache hit rate is high                    
 • Expose cache hit stats in /usage output so Saksham can see actual savings                                            

------------------------------------------------------------------------------------------------------------------------

2. Hook System - Full Lifecycle Automation                                                                              

What Claude Code does: 26+ lifecycle hook events. Hooks are shell commands, HTTP endpoints, LLM prompts, or agent calls 
that fire automatically. No manual invocation.                                                                          

Full event list (as of 2026):                                                                                           

                                                                        
 Event                When                                              
 ────────────────────────────────────────────────────────────────────── 
 SessionStart         Session begins or resumes                         
 Setup                --init-only mode, one-time CI prep                
 UserPromptSubmit     Before Claude processes your message              
 UserPromptExpansion  When a slash command expands to a prompt          
 PreToolUse           Before a tool call - can block it                 
 PermissionRequest    When a permission dialog appears                  
 PermissionDenied     Tool denied - return {retry: true} to allow retry 
 PostToolUse          After a tool call succeeds                        
 PostToolUseFailure   After a tool call fails                           
 PostToolBatch        After a full parallel tool batch resolves         
 Notification         When Claude sends a notification                  
 SubagentStart        When a subagent is spawned                        
 SubagentStop         When a subagent finishes                          
 TaskCreated          When a task is being created                      
 TaskCompleted        When a task is marked complete                    
 Stop                 When Claude finishes a turn                       
 StopFailure          Turn ends due to API error                        
 TeammateIdle         Agent team teammate about to go idle              
 InstructionsLoaded   CLAUDE.md or rules file loaded                    
 ConfigChange         Config file changes during session                
 CwdChanged           Working directory changes                         
 FileChanged          Watched file changes on disk                      
 WorktreeCreate       Worktree being created                            
 WorktreeRemove       Worktree being removed                            
 PreCompact           Before context compaction                         
 PostCompact          After compaction completes                        
 Elicitation          MCP server requests user input                    
 ElicitationResult    User responds to MCP elicitation                  
 SessionEnd           Session terminates                                
                                                                        

Hook handler types:                                                                                                     

 • command: shell script, reads JSON from stdin, writes decision to stdout                                              
 • http: POST JSON to external endpoint with auth headers                                                               
 • mcp: call an MCP tool                                                                                                
 • prompt: let the LLM decide with context (flexible)                                                                   
 • agent: spawn a full agent for complex hook logic                                                                     

Async hooks: Add "async": true to run in the background without blocking the agent loop.                                

Example - block dangerous commands:                                                                                     

                                                                                                                        
 {                                                                                                                      
   "hooks": {                                                                                                           
     "PreToolUse": [{                                                                                                   
       "matcher": "Bash",                                                                                               
       "hooks": [{                                                                                                      
         "type": "command",                                                                                             
         "if": "Bash(rm *)",                                                                                            
         "command": "~/.claude/hooks/block-rm.sh"                                                                       
       }]                                                                                                               
     }]                                                                                                                 
   }                                                                                                                    
 }                                                                                                                      
                                                                                                                        

Example - auto-format after file edits:                                                                                 

                                                                                                                        
 {                                                                                                                      
   "hooks": {                                                                                                           
     "PostToolUse": [{                                                                                                  
       "matcher": "Edit",                                                                                               
       "hooks": [{                                                                                                      
         "type": "command",                                                                                             
         "command": "~/.claude/hooks/autoformat.sh",                                                                    
         "async": true                                                                                                  
       }]                                                                                                               
     }]                                                                                                                 
   }                                                                                                                    
 }                                                                                                                      
                                                                                                                        

What OC has today: OC has a hook system (PreToolUse, PostToolUse, SessionStart) but nowhere near the depth of Claude    
Code's 26+ events.                                                                                                      

What OC should build:                                                                                                   

 • CwdChanged: react when the agent changes directory (run direnv, update PATH)                                         
 • FileChanged: watch specific files for changes and trigger agent runs                                                 
 • PreCompact / PostCompact: inject context before compaction, restore state after                                      
 • PostToolBatch: fire after a batch of parallel tools resolves                                                         
 • TeammateIdle: coordinate agent teams                                                                                 
 • UserPromptExpansion: intercept and modify slash command expansions                                                   
 • InstructionsLoaded: react when CLAUDE.md files are loaded                                                            
 • HTTP hook type: POST events to external services (webhooks, logging, Slack)                                          
 • Async hook support: fire-and-forget hooks that don't block the agent loop                                            

------------------------------------------------------------------------------------------------------------------------

3. CLAUDE.md Hierarchy + Rules Directory                                                                                

What Claude Code does: Hierarchical context files, loaded bottom-up at session start and lazily during work:            

                                                                                                                        
 ~/.claude/CLAUDE.md          # Global: applies to every project                                                        
 ~/.claude/rules/             # Split global rules into files                                                           
   formatting.md                                                                                                        
   security.md                                                                                                          
   git-conventions.md                                                                                                   
 ~/project/CLAUDE.md          # Project-wide conventions                                                                
 ~/project/src/CLAUDE.md      # Directory-specific                                                                      
 ~/project/src/components/CLAUDE.md  # Component-specific                                                               
                                                                                                                        

When you work on src/components/Button.vue, Claude loads ALL of these in hierarchy.                                     

CLAUDE.local.md - sits alongside CLAUDE.md but is gitignored. Personal preferences without affecting teammates.         

Also: @file reference syntax inside CLAUDE.md to inline specific files.                                                 

What OC has today: Single SOUL.md and AGENTS.md at workspace root. No hierarchy. No directory-specific context.         

What OC should build:                                                                                                   

 • Hierarchical context loading: workspace root + current directory + parent directories                                
 • ~/.opencomputer/rules/ directory: split global rules into separate files                                             
 • OPENCOMPUTER.local.md: gitignored local overrides                                                                    
 • Lazy loading: load CLAUDE.md from a directory when the agent first touches a file in it                              
 • This makes OC project-aware without manual context injection                                                         

------------------------------------------------------------------------------------------------------------------------

4. Deferred Tool Loading (ToolSearch)                                                                                   

What Claude Code does (2026): Claude Code no longer loads full MCP tool schemas at startup. It loads tool names only,   
then fetches the full schema on demand via ToolSearch when needed. For 50+ tools this cuts context overhead by an order 
of magnitude.                                                                                                           

                                                                                                                        
 Startup: load [tool_name_1, tool_name_2, ...tool_name_50]  ~200 tokens                                                 
 On use:  fetch schema for tool_name_12                      ~800 tokens (only when needed)                             
                                                                                                                        

You can monitor with /context to see remaining context usage.                                                           

What OC has today: All tool descriptions are injected into system prompt at startup. With 80+ skills and 30+ tools, this
is significant token overhead every single turn.                                                                        

What OC should build:                                                                                                   

 • Deferred skill/tool loading: inject only names at startup, full description on first invocation                      
 • ToolSearch equivalent: agent can search for tools by name/description before loading full schema                     
 • Skill description optimization: shorten descriptions, they're capped in Claude Code                                  
 • /context command showing token breakdown by component                                                                

------------------------------------------------------------------------------------------------------------------------

5. Worktree Isolation for Subagents                                                                                     

What Claude Code does: Set isolation: worktree on a subagent to give it its own git worktree. Multiple subagents can    
edit files in parallel without conflicts. Worktree is cleaned up if no changes made; if changes produced, you get the   
branch back.                                                                                                            

                                                                                                                        
 ---                                                                                                                    
 name: deep-refactor                                                                                                    
 context: fork                                                                                                          
 isolation: worktree                                                                                                    
 agent: general-purpose                                                                                                 
 ---                                                                                                                    
 Refactor $ARGUMENTS. Work independently.                                                                               
                                                                                                                        

What OC has today: delegate with isolation: "worktree" - this already exists in OC. Good.                               

What OC should build:                                                                                                   

 • Verify OC's worktree isolation is working correctly end-to-end                                                       
 • Expose isolation option on skill frontmatter (not just delegate call)                                                
 • Auto-cleanup empty worktrees after subagent finishes                                                                 

------------------------------------------------------------------------------------------------------------------------

6. Extended Hook Handler Types                                                                                          

What Claude Code does: Beyond shell commands, hooks can be:                                                             

Prompt hooks - LLM decides what to do:                                                                                  

                                                                                                                        
 {                                                                                                                      
   "type": "prompt",                                                                                                    
   "prompt": "Review this tool call. If it looks dangerous, block it with a reason.",                                   
   "model": "haiku"                                                                                                     
 }                                                                                                                      
                                                                                                                        

Agent hooks - spawn a full agent:                                                                                       

                                                                                                                        
 {                                                                                                                      
   "type": "agent",                                                                                                     
   "agent": "security-auditor",                                                                                         
   "context": "fork"                                                                                                    
 }                                                                                                                      
                                                                                                                        

HTTP hooks - POST to external services:                                                                                 

                                                                                                                        
 {                                                                                                                      
   "type": "http",                                                                                                      
   "url": "https://hooks.slack.com/...",                                                                                
   "headers": { "Authorization": "Bearer ${SLACK_TOKEN}" }                                                              
 }                                                                                                                      
                                                                                                                        

What OC has today: OC hooks support command and prompt types. No HTTP hooks. No agent-as-hook-handler.                  

What OC should build:                                                                                                   

 • HTTP hook type: POST tool events to external services (n8n, Zapier, custom webhooks)                                 
 • Agent hook: spawn a delegate as a hook handler for complex decisions                                                 
 • Hook timeout config: prevent slow hooks from blocking the agent loop                                                 

------------------------------------------------------------------------------------------------------------------------

7. Skill Frontmatter: Full Feature Set                                                                                  

What Claude Code does: Rich frontmatter on skills/commands controlling invocation, isolation, model, paths, tools:      

                                                                                                                        
 ---                                                                                                                    
 name: security-audit                                                                                                   
 description: Analyzes code for security vulnerabilities                                                                
 disable-model-invocation: true    # only human can invoke, not auto                                                    
 user-invocable: false             # hide from / menu (background knowledge)                                            
 allowed-tools: Read, Grep, Bash(git *)                                                                                 
 context: fork                     # run in isolated subagent context                                                   
 agent: Explore                    # which built-in agent type                                                          
 model: sonnet                     # model override for this skill                                                      
 argument-hint: "<file_path>"      # shown in autocomplete                                                              
 paths: ["src/**/*.ts"]            # only auto-load when working in these paths                                         
 isolation: worktree               # git worktree isolation                                                             
 ---                                                                                                                    
                                                                                                                        

Key fields OC is missing:                                                                                               

 • disable-model-invocation: true - only the user can trigger this skill (not Claude auto-invoking it). Critical for    
   deploy/commit skills.                                                                                                
 • paths: - only auto-inject this skill when the agent is working in matching paths. Prevents irrelevant skills from    
   loading.                                                                                                             
 • argument-hint - shown in slash command autocomplete UI                                                               
 • user-invocable: false - inject as background knowledge but hide from slash command menu                              
 • model: override per skill                                                                                            

What OC should build:                                                                                                   

 • disable-model-invocation frontmatter key (already partially done via description wording, but make it explicit)      
 • paths: glob array: only auto-activate skill when working in matching directories                                     
 • model: per-skill model override (run expensive skills on Sonnet, cheap ones on Haiku)                                
 • argument-hint: for better CLI autocomplete                                                                           
 • allowed-tools: override per skill (restrict what the skill can use)                                                  

------------------------------------------------------------------------------------------------------------------------

8. Auto-Memory / Project Memory                                                                                         

What Claude Code does: Claude maintains ~/.claude/projects/<hash>/memory.md per project. As Claude works, it learns     
project-specific facts (preferred patterns, common commands, architecture notes) and writes them to this file. The file 
is injected into every future session on that project.                                                                  

Also: CLAUDE.local.md for personal preferences per project, gitignored.                                                 
I need a text sample to summarize. Please provide the reasoning or task you'd like me to summarize in one line  ›
oc ›
Tools keep getting cut. Here's the full Claude Code MD file as text - copy it manually into docs/OC-FROM-CLAUDE-CODE.md:

------------------------------------------------------------------------------------------------------------------------

                                                                                                                        
 # OC Feature Gaps: What to Take from Claude Code (Anthropic)                                                           
                                                                                                                        
 Deep analysis of Claude Code's architecture, hooks system, skills model, and context management                        
 vs what OC ships today. Source: Anthropic official docs, DeepWiki analysis, Claude Code changelog.                     
                                                                                                                        
 ---                                                                                                                    
                                                                                                                        
 ## TIER 1 - Build These Now (Highest Impact)                                                                           
                                                                                                                        
 ---                                                                                                                    
                                                                                                                        
 ### 1. Prompt Caching - Best-of-Both (Anthropic Native + OpenClaw Config)                                              
                                                                                                                        
 **What Anthropic does (the right way):**                                                                               
 Native API-level caching. Two modes:                                                                                   
                                                                                                                        
 **Automatic caching** (simplest - add one field):                                                                      
 ```json                                                                                                                
 {                                                                                                                      
   "model": "claude-opus-4-7",                                                                                          
   "cache_control": { "type": "ephemeral" }                                                                             
 }                                                                                                                      
                                                                                                                        

The system automatically applies the cache breakpoint to the last cacheable block and moves it forward as the           
conversation grows. Each new request caches everything up to the last cacheable block; previous content is read from    
cache.                                                                                                                  

Explicit breakpoints (fine-grained): Place cache_control on individual content blocks - tools, system prompt, messages -
at different TTLs.                                                                                                      

Cache prefix order: tools -> system -> messages. Up to 4 breakpoints. 5-min TTL (default) or 1-hour TTL (2x cost). Cache
reads are 0.1x the base input token price.                                                                              

What OpenClaw adds on top: Per-model cache config in agent settings:                                                    

                                                                                                                        
 {                                                                                                                      
   "agents": {                                                                                                          
     "defaults": {                                                                                                      
       "models": {                                                                                                      
         "anthropic/claude-opus-4-5": {                                                                                 
           "params": {                                                                                                  
             "cacheControlTtl": "1h",                                                                                   
             "cacheRetention": "short"                                                                                  
           }                                                                                                            
         }                                                                                                              
       }                                                                                                                
     }                                                                                                                  
   }                                                                                                                    
 }                                                                                                                      
                                                                                                                        

Also: moving the date out of the system prompt to improve cache hit rates (OC already does this partially).             

What OC should do:                                                                                                      

 • Verify OC is passing cache_control on system prompt AND tools (not just messages)                                    
 • Add cacheControlTtl config per model: "5m" or "1h" depending on session length                                       
 • Move volatile content (date, cwd, git status) to the END of system prompt so the static prefix caches cleanly        
 • For heartbeat runs: use lightContext equivalent - minimal system prompt so cache hit rate is high                    
 • Expose cache hit stats in /usage output so Saksham can see actual savings                                            

------------------------------------------------------------------------------------------------------------------------

2. Hook System - Full Lifecycle Automation                                                                              

What Claude Code does: 26+ lifecycle hook events. Hooks are shell commands, HTTP endpoints, LLM prompts, or agent calls 
that fire automatically. No manual invocation.                                                                          

Full event list (as of 2026):                                                                                           

                                                                        
 Event                When                                              
 ────────────────────────────────────────────────────────────────────── 
 SessionStart         Session begins or resumes                         
 Setup                --init-only mode, one-time CI prep                
 UserPromptSubmit     Before Claude processes your message              
 UserPromptExpansion  When a slash command expands to a prompt          
 PreToolUse           Before a tool call - can block it                 
 PermissionRequest    When a permission dialog appears                  
 PermissionDenied     Tool denied - return {retry: true} to allow retry 
 PostToolUse          After a tool call succeeds                        
 PostToolUseFailure   After a tool call fails                           
 PostToolBatch        After a full parallel tool batch resolves         
 Notification         When Claude sends a notification                  
 SubagentStart        When a subagent is spawned                        
 SubagentStop         When a subagent finishes                          
 TaskCreated          When a task is being created                      
 TaskCompleted        When a task is marked complete                    
 Stop                 When Claude finishes a turn                       
 StopFailure          Turn ends due to API error                        
 TeammateIdle         Agent team teammate about to go idle              
 InstructionsLoaded   CLAUDE.md or rules file loaded                    
 ConfigChange         Config file changes during session                
 CwdChanged           Working directory changes                         
 FileChanged          Watched file changes on disk                      
 WorktreeCreate       Worktree being created                            
 WorktreeRemove       Worktree being removed                            
 PreCompact           Before context compaction                         
 PostCompact          After compaction completes                        
 Elicitation          MCP server requests user input                    
 ElicitationResult    User responds to MCP elicitation                  
 SessionEnd           Session terminates                                
                                                                        

Hook handler types:                                                                                                     

 • command: shell script, reads JSON from stdin, writes decision to stdout                                              
 • http: POST JSON to external endpoint with auth headers                                                               
 • mcp: call an MCP tool                                                                                                
 • prompt: let the LLM decide with context (flexible)                                                                   
 • agent: spawn a full agent for complex hook logic                                                                     

Async hooks: Add "async": true to run in the background without blocking the agent loop.                                

Example - block dangerous commands:                                                                                     

                                                                                                                        
 {                                                                                                                      
   "hooks": {                                                                                                           
     "PreToolUse": [{                                                                                                   
       "matcher": "Bash",                                                                                               
       "hooks": [{                                                                                                      
         "type": "command",                                                                                             
         "if": "Bash(rm *)",                                                                                            
         "command": "~/.claude/hooks/block-rm.sh"                                                                       
       }]                                                                                                               
     }]                                                                                                                 
   }                                                                                                                    
 }                                                                                                                      
                                                                                                                        

Example - auto-format after file edits:                                                                                 

                                                                                                                        
 {                                                                                                                      
   "hooks": {                                                                                                           
     "PostToolUse": [{                                                                                                  
       "matcher": "Edit",                                                                                               
       "hooks": [{                                                                                                      
         "type": "command",                                                                                             
         "command": "~/.claude/hooks/autoformat.sh",                                                                    
         "async": true                                                                                                  
       }]                                                                                                               
     }]                                                                                                                 
   }                                                                                                                    
 }                                                                                                                      
                                                                                                                        

What OC has today: OC has a hook system (PreToolUse, PostToolUse, SessionStart) but nowhere near the depth of Claude    
Code's 26+ events.                                                                                                      

What OC should build:                                                                                                   

 • CwdChanged: react when the agent changes directory (run direnv, update PATH)                                         
 • FileChanged: watch specific files for changes and trigger agent runs                                                 
 • PreCompact / PostCompact: inject context before compaction, restore state after                                      
 • PostToolBatch: fire after a batch of parallel tools resolves                                                         
 • TeammateIdle: coordinate agent teams                                                                                 
 • UserPromptExpansion: intercept and modify slash command expansions                                                   
 • InstructionsLoaded: react when CLAUDE.md files are loaded                                                            
 • HTTP hook type: POST events to external services (webhooks, logging, Slack)                                          
 • Async hook support: fire-and-forget hooks that don't block the agent loop                                            

------------------------------------------------------------------------------------------------------------------------

3. CLAUDE.md Hierarchy + Rules Directory                                                                                

What Claude Code does: Hierarchical context files, loaded bottom-up at session start and lazily during work:            

                                                                                                                        
 ~/.claude/CLAUDE.md          # Global: applies to every project                                                        
 ~/.claude/rules/             # Split global rules into files                                                           
   formatting.md                                                                                                        
   security.md                                                                                                          
   git-conventions.md                                                                                                   
 ~/project/CLAUDE.md          # Project-wide conventions                                                                
 ~/project/src/CLAUDE.md      # Directory-specific                                                                      
 ~/project/src/components/CLAUDE.md  # Component-specific                                                               
                                                                                                                        

When you work on src/components/Button.vue, Claude loads ALL of these in hierarchy.                                     

CLAUDE.local.md - sits alongside CLAUDE.md but is gitignored. Personal preferences without affecting teammates.         

Also: @file reference syntax inside CLAUDE.md to inline specific files.                                                 

What OC has today: Single SOUL.md and AGENTS.md at workspace root. No hierarchy. No directory-specific context.         

What OC should build:                                                                                                   

 • Hierarchical context loading: workspace root + current directory + parent directories                                
 • ~/.opencomputer/rules/ directory: split global rules into separate files                                             
 • OPENCOMPUTER.local.md: gitignored local overrides                                                                    
 • Lazy loading: load CLAUDE.md from a directory when the agent first touches a file in it                              
 • This makes OC project-aware without manual context injection                                                         

------------------------------------------------------------------------------------------------------------------------

4. Deferred Tool Loading (ToolSearch)                                                                                   

What Claude Code does (2026): Claude Code no longer loads full MCP tool schemas at startup. It loads tool names only,   
then fetches the full schema on demand via ToolSearch when needed. For 50+ tools this cuts context overhead by an order 
of magnitude.                                                                                                           

                                                                                                                        
 Startup: load [tool_name_1, tool_name_2, ...tool_name_50]  ~200 tokens                                                 
 On use:  fetch schema for tool_name_12                      ~800 tokens (only when needed)                             
                                                                                                                        

You can monitor with /context to see remaining context usage.                                                           

What OC has today: All tool descriptions are injected into system prompt at startup. With 80+ skills and 30+ tools, this
is significant token overhead every single turn.                                                                        

What OC should build:                                                                                                   

 • Deferred skill/tool loading: inject only names at startup, full description on first invocation                      
 • ToolSearch equivalent: agent can search for tools by name/description before loading full schema                     
 • Skill description optimization: shorten descriptions, they're capped in Claude Code                                  
 • /context command showing token breakdown by component                                                                

------------------------------------------------------------------------------------------------------------------------

5. Worktree Isolation for Subagents                                                                                     

What Claude Code does: Set isolation: worktree on a subagent to give it its own git worktree. Multiple subagents can    
edit files in parallel without conflicts. Worktree is cleaned up if no changes made; if changes produced, you get the   
branch back.                                                                                                            

                                                                                                                        
 ---                                                                                                                    
 name: deep-refactor                                                                                                    
 context: fork                                                                                                          
 isolation: worktree                                                                                                    
 agent: general-purpose                                                                                                 
 ---                                                                                                                    
 Refactor $ARGUMENTS. Work independently.                                                                               
                                                                                                                        

What OC has today: delegate with isolation: "worktree" - this already exists in OC. Good.                               

What OC should build:                                                                                                   

 • Verify OC's worktree isolation is working correctly end-to-end                                                       
 • Expose isolation option on skill frontmatter (not just delegate call)                                                
 • Auto-cleanup empty worktrees after subagent finishes                                                                 

------------------------------------------------------------------------------------------------------------------------

6. Extended Hook Handler Types                                                                                          

What Claude Code does: Beyond shell commands, hooks can be:                                                             

Prompt hooks - LLM decides what to do:                                                                                  

                                                                                                                        
 {                                                                                                                      
   "type": "prompt",                                                                                                    
   "prompt": "Review this tool call. If it looks dangerous, block it with a reason.",                                   
   "model": "haiku"                                                                                                     
 }                                                                                                                      
                                                                                                                        

Agent hooks - spawn a full agent:                                                                                       

                                                                                                                        
 {                                                                                                                      
   "type": "agent",                                                                                                     
   "agent": "security-auditor",                                                                                         
   "context": "fork"                                                                                                    
 }                                                                                                                      
                                                                                                                        

HTTP hooks - POST to external services:                                                                                 

                                                                                                                        
 {                                                                                                                      
   "type": "http",                                                                                                      
   "url": "https://hooks.slack.com/...",                                                                                
   "headers": { "Authorization": "Bearer ${SLACK_TOKEN}" }                                                              
 }                                                                                                                      
                                                                                                                        

What OC has today: OC hooks support command and prompt types. No HTTP hooks. No agent-as-hook-handler.                  

What OC should build:                                                                                                   

 • HTTP hook type: POST tool events to external services (n8n, Zapier, custom webhooks)                                 
 • Agent hook: spawn a delegate as a hook handler for complex decisions                                                 
 • Hook timeout config: prevent slow hooks from blocking the agent loop                                                 

------------------------------------------------------------------------------------------------------------------------

7. Skill Frontmatter: Full Feature Set                                                                                  

What Claude Code does: Rich frontmatter on skills/commands controlling invocation, isolation, model, paths, tools:      

                                                                                                                        
 ---                                                                                                                    
 name: security-audit                                                                                                   
 description: Analyzes code for security vulnerabilities                                                                
 disable-model-invocation: true    # only human can invoke, not auto                                                    
 user-invocable: false             # hide from / menu (background knowledge)                                            
 allowed-tools: Read, Grep, Bash(git *)                                                                                 
 context: fork                     # run in isolated subagent context                                                   
 agent: Explore                    # which built-in agent type                                                          
 model: sonnet                     # model override for this skill                                                      
 argument-hint: "<file_path>"      # shown in autocomplete                                                              
 paths: ["src/**/*.ts"]            # only auto-load when working in these paths                                         
 isolation: worktree               # git worktree isolation                                                             
 ---                                                                                                                    
                                                                                                                        

Key fields OC is missing:                                                                                               

 • disable-model-invocation: true - only the user can trigger this skill (not Claude auto-invoking it). Critical for    
   deploy/commit skills.                                                                                                
 • paths: - only auto-inject this skill when the agent is working in matching paths. Prevents irrelevant skills from    
   loading.                                                                                                             
 • argument-hint - shown in slash command autocomplete UI                                                               
 • user-invocable: false - inject as background knowledge but hide from slash command menu                              
 • model: override per skill                                                                                            

What OC should build:                                                                                                   

 • disable-model-invocation frontmatter key (already partially done via description wording, but make it explicit)      
 • paths: glob array: only auto-activate skill when working in matching directories                                     
 • model: per-skill model override (run expensive skills on Sonnet, cheap ones on Haiku)                                
 • argument-hint: for better CLI autocomplete                                                                           
 • allowed-tools: override per skill (restrict what the skill can use)                                                  

------------------------------------------------------------------------------------------------------------------------


 8 Auto-Memory / Project Memory                                                                                         

What Claude Code does: Claude maintains ~/.claude/projects/<hash>/memory.md per project. As Claude works, it learns     
project-specific facts (preferred patterns, common commands, architecture notes) and writes them to this file. The file 
is injected into every future session on that project.                                                                  

Also: CLAUDE.local.md for personal preferences per project, gitignored.                                                 

What OC has today: Global MEMORY.md. No per-project memory. No automatic learning from work done.                       

What OC should build:                                                                                                   

 • Per-project memory file: ~/.opencomputer/projects/<project-hash>/memory.md                                           
 • Auto-learn: after completing tasks, agent writes key facts to project memory                                         
 • Inject project memory at session start when cwd matches                                                              
 • OPENCOMPUTER.local.md per project: gitignored personal preferences                                                   

------------------------------------------------------------------------------------------------------------------------


  9 Three Built-in Subagent Types                                                                                       

What Claude Code does: Three pre-configured subagent personalities available without writing config:                    

 • Explore - fast, read-only, uses Haiku. For codebase exploration, file finding.                                       
 • Plan - research and architecture, read-only. For designing approaches before implementation.                         
 • General-purpose - full tool access. For implementation work.                                                         

Usage in skill frontmatter: agent: Explore                                                                              

What OC has today: Subagents are generic. No pre-configured specializations.                                            

What OC should build:                                                                                                   

 • explore agent type: Haiku, read-only tools (Read, Grep, Glob), fast/cheap                                            
 • plan agent type: Sonnet, read-only, optimized for architecture thinking                                              
 • implement agent type: Opus/Sonnet, full tools, for execution                                                         
 • These map to OC's existing delegate tool - just pre-configure the system prompt + tool allowlist + model             

------------------------------------------------------------------------------------------------------------------------


 10 /usage Command with Full Cost Breakdown                                                                             

What Claude Code does: /usage (merged from /cost and /stats) shows:                                                     

 • Token usage breakdown by component (system prompt, tools, messages, thinking)                                        
 • Prompt cache hit/miss stats and cost savings                                                                         
 • Effort metrics (when supported by model)                                                                             
 • Context window remaining percentage                                                                                  
 • Compaction count                                                                                                     

What OC has today: No in-session cost visibility. You find out at the end of the month.                                 

What OC should build:                                                                                                   

 • /usage command: tokens used, context % remaining, cache hit rate, estimated cost                                     
 • Surface cache_read_input_tokens vs cache_creation_input_tokens from API response                                     
 • Show per-component breakdown: system prompt tokens, tool description tokens, conversation tokens                     
 • Running cost estimate per session                                                                                    

------------------------------------------------------------------------------------------------------------------------


 11 Checkpointing (Session Snapshots)                                                                                   

What Claude Code does: Creates restorable checkpoints at key points during a session. If Claude goes down a wrong path  
for 10 minutes, you can restore to the last checkpoint rather than starting over. Separate from compaction.             

What OC has today: Sessions are stored sequentially. No restore-to-checkpoint capability.                               

What OC should build:                                                                                                   

 • Checkpoint creation: /checkpoint command saves current session state                                                 
 • Checkpoint restore: `/restore             
 