# Open Interpreter Architecture & Capability Source Map

**Document Purpose**: Complete mapping of OI capabilities, dependencies, and sandbox gaps for OpenComputer subprocess integration.  
**OI Version Scanned**: 0.4.3  
**Scan Date**: 2024-04-24  
**License**: GNU AGPL v3 (confirmed)

---

## Executive Summary

Open Interpreter is a **GNU Affero General Public License v3** (AGPL v3) codebase that provides language models direct computer control through Python-based capabilities. The architecture is organized into 15 major capability modules under `/interpreter/core/computer/`, each exposing methods like `computer.mouse.click()`, `computer.keyboard.press()`, etc. A parallel modern Anthropic-aligned tool collection exists in `/interpreter/computer_use/` for tool-based integration.

**Key findings:**

1. **AGPL Boundary**: OI must be isolated to a subprocess venv; no direct Python imports into OpenComputer main code.
2. **Telemetry**: PostHog hardcoded at `/interpreter/core/utils/telemetry.py` line 52 (API key exposed). Kill-switch: patch to no-op before ANY OI import.
3. **Capability Count**: 14 major modules + 40+ discrete methods; we curate 23 for the master plan across 5 tiers.
4. **Unsafe Patterns**: AppleScript execution (all macOS methods), shell subprocess (terminal module), file I/O without restrictions, pyautogui direct hardware access, Selenium browser automation without sandboxing.
5. **Subprocess Concerns**: Expects stdin/stdout control, tolerates 120s timeouts, Python import hooks for `computer` API injection.

---

## Capability Families & Detailed Mapping

### 1. **File System (4 methods)**
- **Module**: `/interpreter/core/computer/files/files.py`
- **Class**: `Files`
- **Platform**: macOS / Linux / Windows
- **Methods**:
  - `search(query)` → Filesystem search (wraps `aifs` library)
  - `edit(path, original_text, replacement_text)` → String replacement in files
- **Dependencies**: `aifs` (lazy import)
- **Side Effects**: Direct file reads/writes; no sandboxing
- **Risks**: CRITICAL—unbounded file access; can read/modify any file in filesystem
- **Risk Level**: **HIGH**

### 2. **Display & Screenshots (8 methods)**
- **Module**: `/interpreter/core/computer/display/display.py`
- **Class**: `Display`
- **Platform**: macOS / Linux / Windows (with xdisplay for Linux)
- **Methods**:
  - `view(show, quadrant, screen, combine_screens, active_app_only)` → Screenshot to base64
  - `screenshot(...)` → Capture screen in various formats
  - `size()` → Get screen resolution
  - `center()` → Calculate screen center
  - `info()` → List connected monitors
  - `find(text, screenshot)` → OCR text location
  - `ocr()` → Extract text from image
  - `find_icon(icon_description)` → Vision-based icon detection
- **Dependencies**: PyAutoGUI, PIL, OpenCV (optional), pytesseract, sentence-transformers, Tesseract (OS-level)
- **Side Effects**: Screenshot captures to temp files; OCR via Tesseract subprocess
- **Risks**: Captures full screen including sensitive data (passwords, PII)
- **Risk Level**: **HIGH**

### 3. **Keyboard & Input Simulation (6 methods)**
- **Module**: `/interpreter/core/computer/keyboard/keyboard.py`
- **Class**: `Keyboard`
- **Platform**: macOS / Linux / Windows
- **Methods**:
  - `write(text, interval, delay)` → Type text with clipboard fallback
  - `press(*keys, presses, interval)` → Press keyboard keys
  - `hotkey(*keys)` → Keyboard shortcuts (Cmd+C, Ctrl+V, etc.)
- **Dependencies**: PyAutoGUI
- **Side Effects**: Modifies system clipboard via `computer.clipboard`; direct hardware input injection
- **Risks**: Can inject keystrokes into any app; clipboard hijacking
- **Risk Level**: **CRITICAL**

### 4. **Mouse Control (6 methods)**
- **Module**: `/interpreter/core/computer/mouse/mouse.py`
- **Class**: `Mouse`
- **Platform**: macOS / Linux / Windows
- **Methods**:
  - `scroll(clicks)` → Scroll wheel
  - `position()` → Get cursor coordinates
  - `move(x, y, text, icon, screenshot)` → Move cursor (text-based or XY)
  - `click(x, y, text, icon, double, right)` → Click at position or text
  - `drag(x, y, ...)` → Drag & drop
- **Dependencies**: PyAutoGUI, OpenCV (for text/icon finding), PIL
- **Side Effects**: Direct hardware control; screen scanning for targets
- **Risks**: Unguarded pointer automation; can click ANY button including system-critical UI
- **Risk Level**: **CRITICAL**

### 5. **Clipboard (3 methods)**
- **Module**: `/interpreter/core/computer/clipboard/clipboard.py`
- **Class**: `Clipboard`
- **Platform**: macOS / Linux / Windows
- **Methods**:
  - `view()` → Read clipboard contents
  - `copy(text)` → Write to clipboard
  - `paste()` → Issue Cmd+V / Ctrl+V
- **Dependencies**: PyPerclip (+ system clipboard tools: xclip on Linux, native on macOS/Windows)
- **Side Effects**: Reads/modifies system clipboard; can expose previously copied data
- **Risks**: Leaks data from clipboard to LLM responses; can be used as covert data exfiltration channel
- **Risk Level**: **HIGH**

### 6. **Email & Communication (2 modules)**

#### 6a. **Mail**
- **Module**: `/interpreter/core/computer/mail/mail.py`
- **Class**: `Mail`
- **Platform**: macOS ONLY (uses AppleScript to Mail.app)
- **Methods**:
  - `get(number, unread)` → Fetch last N emails from inbox
  - `send(to, subject, body, attachments)` → Send email via Mail.app
- **Dependencies**: AppleScript (macOS-only), subprocess
- **Side Effects**: Invokes Mail.app; reads emails from system database; sends real emails
- **Risks**: CRITICAL—can read all inbox emails (including passwords in messages), send unauthorized emails, attach files
- **Risk Level**: **CRITICAL** (macOS), **NONE** (other OS)

#### 6b. **SMS**
- **Module**: `/interpreter/core/computer/sms/sms.py`
- **Class**: `SMS`
- **Platform**: macOS ONLY (sqlite3 on chat.db + AppleScript Messages)
- **Methods**:
  - `send(to, message)` → Send iMessage via Messages.app
  - `get(contact, limit, substring)` → Query message database
- **Dependencies**: sqlite3, AppleScript, subprocess
- **Side Effects**: Direct database access to `~/Library/Messages/chat.db`; sends real messages
- **Risks**: CRITICAL—full SMS/iMessage history accessible; unsandboxed message sending
- **Risk Level**: **CRITICAL** (macOS), **NONE** (other OS)

### 7. **Email Metadata**
- **Module**: `/interpreter/core/computer/mail/mail.py` (extended)
- **Capability**: `mail.get()` returns sender, subject, content
- **Risks**: Exposes all email metadata without filtering
- **Risk Level**: **HIGH**

### 8. **Calendar (4 methods)**
- **Module**: `/interpreter/core/computer/calendar/calendar.py`
- **Class**: `Calendar`
- **Platform**: macOS ONLY (AppleScript Calendar.app)
- **Methods**:
  - `get_events(start_date, end_date)` → Fetch calendar events
  - `create_event(title, start_date, end_date, location, notes, calendar)` → Create event
  - `delete_event(title, date, calendar)` → Delete event
  - `update_event(title, new_title, new_date, calendar)` → Modify event
- **Dependencies**: AppleScript, subprocess
- **Side Effects**: Reads/writes Calendar.app database via AppleScript
- **Risks**: Full calendar access including sensitive meeting titles/attendees
- **Risk Level**: **HIGH** (macOS), **NONE** (other OS)

### 9. **Contacts (4 methods)**
- **Module**: `/interpreter/core/computer/contacts/contacts.py`
- **Class**: `Contacts`
- **Platform**: macOS ONLY (AppleScript Contacts.app)
- **Methods**:
  - `get_phone_number(contact_name)` → Retrieve phone number
  - `get_email_address(contact_name)` → Retrieve email
  - `get_full_names_from_first_name(first_name)` → Fuzzy contact search
  - `get_contact_info(contact_name)` → Full contact card
- **Dependencies**: AppleScript, subprocess
- **Side Effects**: Reads Contacts.app database
- **Risks**: Full PII exposure; can enumerate all contacts with phone/email
- **Risk Level**: **HIGH** (macOS), **NONE** (other OS)

### 10. **Browser Control (8 methods)**
- **Module**: `/interpreter/core/computer/browser/browser.py`
- **Class**: `Browser`
- **Platform**: macOS / Linux / Windows (Selenium ChromeDriver)
- **Methods**:
  - `search(query)` → Web search via OpenInterpreter API
  - `fast_search(query)` → Parallel API + local Google search
  - `go_to_url(url)` → Navigate Selenium Chrome instance
  - `find(text, screenshot)` → Locate text on page
  - `click(text, x, y, icon)` → Click page elements
  - `execute_script(script)` → Run JavaScript on page
  - `get_page_content()` → Retrieve HTML/text
  - `close()` → Close Selenium session
- **Dependencies**: Selenium, webdriver-manager, ChromeDriver (OS-specific), html2text, requests
- **Side Effects**: Launches real Chrome browser; can interact with live websites; downloads ChromeDriver
- **Risks**: CRITICAL—unfiltered web access; can login to accounts, submit forms, download files; Selenium runs unsigned code
- **Risk Level**: **CRITICAL**

### 11. **Browser History & Bookmarks**
- **Capability**: Not directly exposed in current codebase
- **Alternative**: Achievable via terminal shell + sqlite3 on browser databases
- **Risk Level**: **HIGH** (if implemented)

### 12. **Browser DOM & Inspection**
- **Capability**: Covered by `browser.execute_script()` and `browser.get_page_content()`
- **Risk Level**: **CRITICAL**

### 13. **System OS Control (2 methods)**
- **Module**: `/interpreter/core/computer/os/os.py`
- **Class**: `Os`
- **Platform**: macOS (AppleScript) / Linux (plyer) / Windows (plyer)
- **Methods**:
  - `get_selected_text()` → Retrieve currently selected text (clipboard trick)
  - `notify(text)` → Display system notification
- **Dependencies**: AppleScript (macOS) / plyer (Linux/Windows) / subprocess
- **Side Effects**: Modifies system clipboard; triggers native notifications
- **Risks**: Clipboard leakage; notification spam
- **Risk Level**: **MEDIUM**

### 14. **Process & System Introspection**
- **Module**: Terminal module via `computer.terminal.run("shell", "ps aux")`
- **Methods**: Shell subprocess execution
- **Risks**: Full process list exposure; can enumerate system users, services, network connections
- **Risk Level**: **HIGH**

### 15. **AppleScript Execution (macOS-specific)**
- **Module**: `/interpreter/core/computer/utils/run_applescript.py`
- **Functions**: `run_applescript(script)`, `run_applescript_capture(script)`
- **Platform**: macOS ONLY
- **Risk**: AppleScript is a full automation language; can control ANY macOS app, read files, execute arbitrary code
- **Risk Level**: **CRITICAL**

### 16. **Terminal & Code Execution (5 language runtimes)**
- **Module**: `/interpreter/core/computer/terminal/terminal.py`
- **Class**: `Terminal`
- **Languages**: Python, Shell, JavaScript (Node), Ruby, R, PowerShell, Java, AppleScript, HTML, React
- **Methods**:
  - `run(language, code, stream, display)` → Execute code in specified runtime
  - `sudo_install(package)` → Install OS packages (with password prompt)
- **Dependencies**: Interpreter instances for each language (Python, Node, Ruby, etc.), APT (Linux)
- **Side Effects**: Creates language-specific subprocesses; can request sudo password; modifies system packages
- **Risks**: CRITICAL—unrestricted code execution in multiple runtimes; APT install can modify system; Python import hook injects `computer` API
- **Risk Level**: **CRITICAL**

### 17. **Vision & OCR**
- **Module**: `/interpreter/core/computer/vision/vision.py`
- **Class**: `Vision`
- **Methods**:
  - `load(load_moondream, load_easyocr)` → Load vision models (~2GB, first run only)
  - `ocr(base_64, path, pil_image, ...)` → Extract text from image
  - `describe(...)` → Describe image in natural language (Moondream)
- **Dependencies**: EasyOCR, Moondream (transformers), torch, torchvision
- **Side Effects**: Downloads large ML models on first use; creates temp files for image processing
- **Risks**: High memory/disk usage; OCR can accidentally read sensitive data in background windows
- **Risk Level**: **MEDIUM**

### 18. **Skills & Plugin System**
- **Module**: `/interpreter/core/computer/skills/skills.py`
- **Class**: `Skills`
- **Methods**: Dynamic Python function registration
- **Side Effects**: Loads arbitrary Python files from disk; executes as Python code
- **Risks**: CRITICAL—arbitrary code execution if skills directory is writable
- **Risk Level**: **CRITICAL**

### 19. **Docs & Knowledge Retrieval**
- **Module**: `/interpreter/core/computer/docs/docs.py`
- **Class**: `Docs`
- **Methods**: Local documentation search
- **Risk Level**: **LOW**

### 20. **AI Local Inference (Not yet implemented)**
- **Module**: `/interpreter/core/computer/ai/ai.py`
- **Placeholder for local LLM inference
- **Risk Level**: **MEDIUM** (would require sandboxing local model)

---

## Tier Organization (Master Plan: 23 Curated Tools)

### **Tier 1: Introspection (Read-Only, Passive)**
1. **read_file_region** → `files.edit()` (read-only extraction)
2. **list_app_usage** → Terminal: `ps aux | grep`
3. **read_clipboard_once** → `clipboard.view()` (one-shot)
4. **screenshot** → `display.view()` + base64 encoding
5. **extract_screen_text** → `display.ocr()` + Tesseract
6. **list_recent_files** → Terminal: `find ~/.recent` or `ls -lt`
7. **search_files** → `files.search(query)`

**Sandbox Requirements**: Read-only file access only; no clipboard modification; screenshot only when explicit consent given.

---

### **Tier 2: Communication (User-Approved Actions)**
8. **read_git_log** → Terminal: `git log` (shell execution, read-only)
9. **read_email_metadata** → `mail.get()` (metadata only, macOS)
10. **read_email_bodies** → `mail.get()` (full content, macOS)
11. **list_calendar_events** → `calendar.get_events()` (macOS)
12. **read_contacts** → `contacts.get_*()` (macOS)
13. **send_email** → `mail.send()` (macOS, REQUIRES USER APPROVAL)
14. **read_browser_history** → Terminal: `sqlite3 ~/.config/google-chrome/History` (macOS/Linux)
15. **read_browser_bookmarks** → Terminal or browser.py metadata

**Sandbox Requirements**: macOS features require platform check; email/SMS send requires explicit confirmation dialog; read operations should be logged.

---

### **Tier 3: Browser & Web (Controlled, Logged)**
16. **read_browser_dom** → `browser.get_page_content()` (HTML extraction)
17. **web_search** → `browser.search(query)` (read-only web queries)

**Sandbox Requirements**: HTTPS-only; no credential submission without user confirmation; all URLs logged; response size capped.

---

### **Tier 4: System Mutation (Gated, Dangerous)**
18. **edit_file** → `files.edit()` (with path whitelisting)
19. **run_shell** → `terminal.run("shell", ...)` (shell execution)
20. **run_applescript** → `computer.utils.run_applescript()` (macOS ONLY, requires dialog)
21. **inject_keyboard** → `keyboard.write()` + `keyboard.press()` (requires screen confirm)
22. **extract_selected_text** → `os.get_selected_text()` (via clipboard)

**Sandbox Requirements**: Command whitelisting (no `rm`, `sudo`, `curl` to external servers); AppleScript must be explicitly approved; keyboard inject only after 2s confirm delay.

---

### **Tier 5: Advanced (Infrastructure, Rarely Used)**
23. **list_running_processes** → Terminal: `ps aux`

**Sandbox Requirements**: Process list only; no kill/signal permissions.

---

## Telemetry Module: PostHog Hardcoded

**Location**: `/interpreter/core/utils/telemetry.py`

**What It Does**:
- Sends event data to PostHog SaaS at `https://app.posthog.com/capture`
- Hardcoded API key: `phc_6cmXy4MEbLfNGezqGjuUTY8abLu0sAwtGzZFpQW97lc` (exposed in source)
- Tracks: event name, OI version, distinct_id (UUID stored in `~/.cache/open-interpreter/telemetry_user_id`)
- Called via `send_telemetry(event_name, properties=None)` throughout codebase

**Kill-Switch Strategy**:
1. **Pre-import patch**: Before importing ANY OI module, patch `/interpreter/core/utils/telemetry.py:send_telemetry` to no-op:
   ```python
   interpreter.core.utils.telemetry.send_telemetry = lambda *a, **k: None
   ```
2. **Env var override**: Check `DISABLE_TELEMETRY` env var (lines 4-6 document this, but it's not enforced in send_telemetry itself)
3. **Monkey-patch in subprocess**: When OI runs in subprocess venv, patch telemetry.py before execution:
   ```python
   with open('/path/to/telemetry.py', 'r') as f:
       content = f.read()
   content = content.replace(
       'def send_telemetry(',
       'def send_telemetry_DISABLED('
   )
   ```

**Risk**: User data (including code snippets, file paths, errors) may be sent to external SaaS without user knowledge. API key exposure allows external parties to send events under your telemetry ID.

---

## Sandbox Gaps: What OI Does Unsafely

| Feature | Current Behavior | Gap | Mitigation |
|---------|-----------------|-----|-----------|
| **File I/O** | No path restrictions | Can read/write anywhere | Whitelist allowed paths; block home/system dirs |
| **Keyboard/Mouse** | Direct hardware access | Can interact with any UI | Require 2s confirmation screen; log all actions |
| **Clipboard** | No isolation | Can read/write system clipboard | Redirect to isolated clipboard; log all access |
| **AppleScript** | Full automation language | Can control any macOS app | macOS-only; require per-command approval |
| **Terminal** | Unrestricted shell | Can run any command | Whitelist shell commands; block dangerous patterns (sudo, curl, rm) |
| **Browser** | Live Selenium Chrome | Can access any website | HTTPS-only; URL whitelist; no credential submission |
| **Email/SMS** | Real mail/message sending | Sends real messages | Require explicit user approval; show preview |
| **Screenshot** | Full screen capture | Captures sensitive data | Blur/redact on-screen passwords; log capture |
| **Mail/Calendar/Contacts** | Full database access | All PII exposed | Require per-operation approval; audit log |
| **Process List** | Full ps output | Enumerate all processes | Filter to non-system processes; read-only |

---

## AGPL Boundary Plan

**Legal Status**: GNU AGPL v3 confirmed in `/LICENSE`

**Implications**:
- Cannot import `interpreter` module directly into OpenComputer main codebase (proprietary/closed-source)
- Must communicate with OI via JSON-RPC over subprocess IPC
- Any modifications to OI must be released under AGPL v3
- If OI is used as a service (network-accessible), source code must be offered to users

**Subprocess Strategy**:
```
┌─────────────────────────────────┐
│  OpenComputer Main (Proprietary) │
│  ├─ Extension API                 │
│  ├─ Consent Manager              │
│  └─ Sandbox Enforcement          │
└────────┬────────────────────────┘
         │ JSON-RPC over stdin/stdout
         ▼
┌─────────────────────────────────┐
│  OI Subprocess (AGPL v3)        │
│  ├─ interpreter.core            │
│  ├─ computer.* modules          │
│  └─ Telemetry (disabled)        │
└─────────────────────────────────┘
```

**No Direct Python Imports**: Never execute `from interpreter import interpreter` or `import interpreter.core.computer` in OpenComputer process space.

**Sub-dependencies**:
- PyAutoGUI: BSD license (compatible with AGPL)
- Selenium: Apache 2.0 (compatible)
- PyPerclip: BSD (compatible)
- Anthropic SDK: MIT (compatible)
- PIL/Pillow: HPND (compatible)
- All others: GPL-compatible or permissive

---

## Subprocess Concerns

**Environment Variables Expected**:
- `INTERPRETER_COMPUTER_API=False` (prevent recursive imports; set by OI on Python code exec)
- `DISABLE_TELEMETRY` (user hint; not enforced—must patch)
- `TOKENIZERS_PARALLELISM=false` (Vision module; prevents torch warning)

**IPC Protocol**:
- OI subprocess receives JSON-RPC requests on stdin
- Responses returned on stdout (structured as `{"type": "...", "content": "..."}`
- stderr for logging/errors
- Supports streaming for long-running commands (120s timeout in `/interpreter/computer_use/tools/run.py`)

**Signal Handling**:
- SIGTERM from parent → gracefully close open resources (Selenium driver, mail connection, file handles)
- No SIGKILL handling (would corrupt file I/O)
- Timeout: 120s default for shell commands; configurable per task

**stdin/stdout Buffering**:
- OI expects line-buffered output for real-time streaming
- Each tool response should emit `\n` for proper framing
- Screenshot base64 data may exceed 16KB; truncation at 16KB boundary with notice

**Python Import Hooks**:
- Terminal module injects `computer` API on first Python code execution via `import_computer_api_code` (lines 21-30 in terminal.py)
- This requires `INTERPRETER_COMPUTER_API != "False"` env var
- OpenComputer wrapper should set this to `"False"` to prevent in-sandbox LLM code from accessing computer API

---

## Borrow vs. Rebuild Recommendations

| Tool | OI Module | Recommendation | Rationale |
|------|-----------|-----------------|-----------|
| screenshot | display.py | **REBUILD** | OI captures full screen; needs region selection, password blur, consent |
| keyboard_inject | keyboard.py | **BORROW** | PyAutoGUI is battle-tested; wrap with delay + confirm |
| mouse_click | mouse.py | **BORROW** | Text/icon detection via CV is sophisticated; but wrap with logging |
| clipboard_read | clipboard.py | **REBUILD** | Too risky; isolated clipboard API better |
| file_edit | files.py | **REBUILD** | OI has no path validation; needs whitelisting |
| shell_execute | terminal.py | **BORROW+RESTRICT** | Borrow execution; add command whitelisting |
| applescript | utils/run_applescript.py | **BORROW** | Only option on macOS; wrap with approval dialog |
| email_send | mail.py | **BORROW+GATE** | Borrow Mail.app integration; require per-email approval |
| calendar_read | calendar.py | **BORROW+GATE** | Borrow AppleScript; require per-operation approval |
| browser_control | browser.py | **BORROW+RESTRICT** | Borrow Selenium; add URL whitelist + HTTPS-only |
| ocr | vision.py | **BORROW** | Moondream + EasyOCR are best-in-class |
| web_search | browser.py search() | **REBUILD** | OI relies on external API; implement local web search |
| process_list | terminal.py + ps | **REBUILD** | Use psutil library for controlled introspection |
| sms_read | sms.py | **BORROW+GATE** | sqlite3 access is clean; require per-message approval |

---

## Tool Risk Matrix

```
          │ High Impact    │ Medium Impact   │ Low Impact
──────────┼────────────────┼─────────────────┼──────────────
High Freq │ CRITICAL       │ HIGH            │ MEDIUM
(Daily)   │ • shell exec   │ • file_edit     │ • screenshot
          │ • keyboard_inj │ • applescript   │
──────────┼────────────────┼─────────────────┼──────────────
Med Freq  │ CRITICAL       │ MEDIUM          │ LOW
(Weekly)  │ • email_send   │ • calendar      │ • process_list
          │ • sms_send     │ • contacts_read │
──────────┼────────────────┼─────────────────┼──────────────
Low Freq  │ HIGH           │ LOW             │ MINIMAL
(Monthly) │ • browser_ctrl │ • ocr           │ • clipboard_r
          │ • file_search  │ • notifications │
```

---

## Architecture Diagram: Data Flow

```
User Request
    │
    ▼
OpenComputer Consent Manager
    │ (Check: Tier level, Frequency, Path/URL whitelist)
    ▼
OpenComputer Sandbox Enforcer
    │ (Set env vars, patch telemetry, cap timeouts)
    ▼
JSON-RPC Marshaller
    │ (Serialize request to subprocess)
    ▼
┌─────────────────────────────────────────────────────┐
│ OI Subprocess (Isolated venv, no network except...)  │
│                                                     │
│ Tool Collection (computer_use/) or                 │
│ Computer API (core/computer/)                      │
│   ├─ Files Module (files.py)                       │
│   ├─ Display Module (display.py)                   │
│   ├─ Keyboard Module (keyboard.py)                 │
│   ├─ Mouse Module (mouse.py)                       │
│   ├─ Terminal Module (terminal.py)                 │
│   ├─ Mail Module (mail.py) [macOS]                 │
│   ├─ SMS Module (sms.py) [macOS]                   │
│   ├─ Browser Module (browser.py)                   │
│   ├─ Calendar Module (calendar.py) [macOS]         │
│   ├─ Contacts Module (contacts.py) [macOS]         │
│   └─ Vision Module (vision.py)                     │
│                                                     │
│ [Telemetry Patched to No-Op]                       │
└─────────────────────────────────────────────────────┘
    │
    ▼
Response JSON-RPC
    │
    ▼
OpenComputer Audit Logger
    │ (Log: tool_name, args, stdout, duration)
    ▼
User Response
```

---

## Key Files & Lines of Interest

| File | Lines | Purpose |
|------|-------|---------|
| `/LICENSE` | 1-661 | AGPL v3 confirmation |
| `/pyproject.toml` | 1-106 | Dependencies; pyautogui, anthropic, selenium, etc. |
| `/interpreter/core/core.py` | 1-150+ | OpenInterpreter main class initialization |
| `/interpreter/core/computer/computer.py` | 1-200+ | Computer API registry; lists all 15 modules |
| `/interpreter/core/computer/terminal/terminal.py` | 1-150+ | Code execution; language routing; Python API injection |
| `/interpreter/core/utils/telemetry.py` | 1-63 | PostHog integration; hardcoded API key; kill-switch |
| `/interpreter/computer_use/tools/computer.py` | 1-283 | Modern Anthropic tool interface (keyboard, mouse, screenshot) |
| `/interpreter/computer_use/tools/bash.py` | 1-158 | Shell command execution with user confirmation |
| `/interpreter/computer_use/tools/edit.py` | 1-313 | File editor tool; str_replace, view, create commands |
| `/interpreter/core/computer/mail/mail.py` | 1-100+ | AppleScript Mail.app integration (macOS-only) |
| `/interpreter/core/computer/sms/sms.py` | 1-100+ | sqlite3 + AppleScript Messages (macOS-only) |
| `/interpreter/core/computer/browser/browser.py` | 1-100+ | Selenium Chrome automation |
| `/interpreter/core/computer/vision/vision.py` | 1-100+ | Moondream + EasyOCR models |

---

## Summary: The 23 Curated Tools for OpenComputer

1. **read_file_region** (Tier 1)
2. **list_app_usage** (Tier 1)
3. **read_clipboard_once** (Tier 1)
4. **screenshot** (Tier 1)
5. **extract_screen_text** (Tier 1)
6. **list_recent_files** (Tier 1)
7. **search_files** (Tier 1)
8. **read_git_log** (Tier 2)
9. **read_email_metadata** (Tier 2, macOS)
10. **read_email_bodies** (Tier 2, macOS)
11. **list_calendar_events** (Tier 2, macOS)
12. **read_contacts** (Tier 2, macOS)
13. **send_email** (Tier 2, macOS, APPROVAL-GATED)
14. **read_browser_history** (Tier 2)
15. **read_browser_bookmarks** (Tier 2)
16. **read_browser_dom** (Tier 3)
17. **web_search** (Tier 3)
18. **edit_file** (Tier 4, PATH-WHITELISTED)
19. **run_shell** (Tier 4, COMMAND-WHITELISTED)
20. **run_applescript** (Tier 4, macOS, APPROVAL-GATED)
21. **inject_keyboard** (Tier 4, CONFIRM-DELAYED)
22. **extract_selected_text** (Tier 4)
23. **list_running_processes** (Tier 5)

---

## Conclusion

Open Interpreter is a powerful but **inherently unsafe** capability framework. Its design prioritizes ease-of-use over security, with unrestricted file I/O, direct hardware access, and full subprocess spawning. The hardcoded PostHog telemetry and exposed API key are additional concerns.

For OpenComputer integration:

1. **Isolate in subprocess**: Never import OI code into main process
2. **Patch telemetry immediately**: Replace `send_telemetry()` before any OI import
3. **Implement consent gates**: Require user approval for Tier 3+ actions
4. **Whitelist paths & commands**: Restrict file access and shell patterns
5. **Log all actions**: Audit every capability invocation
6. **Platform-check**: macOS-only features (Mail, SMS, Calendar, Contacts, AppleScript) must degrade gracefully on Linux/Windows
7. **Cap timeouts**: 60-120s per action to prevent hanging
8. **Redirect I/O**: Isolate clipboard, browser, and file access to sandbox

**Estimated effort**: 3-4 weeks to build secure wrapper for all 23 tools + tier system + audit logging + consent dialogs.

