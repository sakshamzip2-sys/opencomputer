# Hermes Agent Self-Evolution: Architecture Source Map

**Reference Codebase:** `/Users/saksham/Vscode/claude/sources/hermes-agent-self-evolution/`  
**Purpose:** Deep-scan analysis of the Hermes self-evolution system for OpenComputer's native evolution subpackage.

---

## Executive Overview

The Hermes Agent Self-Evolution system implements a GEPA-driven (Genetic-Pareto Evolutionary Prompting) optimization pipeline that mutates and evolves agent skills, tool descriptions, and prompts via DSPy. The architecture separates concerns into **data (eval datasets)**, **optimization (GEPA runner)**, **constraints (validation)**, and **evaluation (LLM-as-judge scoring)**. No GPU training is used—all work is text mutation + API evaluation. The system assumes tight coupling to hermes-agent's directory structure (skills at `skills/*/SKILL.md`) and uses the `batch_runner` for parallel execution. OpenComputer should build equivalent abstractions but remain framework-agnostic.

---

## Module-by-Module Inventory

### Core Infrastructure Layer

#### `evolution/core/config.py`
**Purpose:** Configuration and hermes-agent repo discovery.

**Public API:**
```python
@dataclass
class EvolutionConfig:
    hermes_agent_path: Path
    iterations: int = 10
    population_size: int = 5
    optimizer_model: str = "openai/gpt-4.1"
    eval_model: str = "openai/gpt-4.1-mini"
    judge_model: str = "openai/gpt-4.1"
    max_skill_size: int = 15_000  # chars
    max_tool_desc_size: int = 500
    max_param_desc_size: int = 200
    max_prompt_growth: float = 0.2
    eval_dataset_size: int = 20
    train_ratio: float = 0.5
    val_ratio: float = 0.25
    holdout_ratio: float = 0.25
    run_pytest: bool = True
    run_tblite: bool = False
    tblite_regression_threshold: float = 0.02
    output_dir: Path = Path("./output")
    create_pr: bool = True

def get_hermes_agent_path() -> Path
```

**Key Collaborators:** Reads env var `HERMES_AGENT_REPO`, falls back to `~/.hermes/hermes-agent` or sibling directory.

**Data Shape:** Flat config dataclass with sensible defaults for training (ratios), constraints (size limits), and optimization hyperparameters.

**Notable Patterns:**
- Three-tier model configuration: optimizer (reflection), eval (judging), judge (dataset generation)
- Extensible constraint config—constraints are data-driven, not hardcoded
- Path resolution with fallback chain encourages flexibility in deployment

**Hermes Coupling:** Direct dependency on external hermes-agent repo location via environment.

---

#### `evolution/core/constraints.py`
**Purpose:** Hard constraint validation—no evolved variant proceeds unless ALL constraints pass.

**Public API:**
```python
@dataclass
class ConstraintResult:
    passed: bool
    constraint_name: str
    message: str
    details: Optional[str] = None

class ConstraintValidator:
    def __init__(self, config: EvolutionConfig)
    def validate_all(
        artifact_text: str,
        artifact_type: str,  # "skill", "tool_description", "param_description"
        baseline_text: Optional[str] = None,
    ) -> list[ConstraintResult]
    def run_test_suite(self, hermes_repo: Path) -> ConstraintResult
```

**Key Collaborators:** Runs `subprocess.run(["python", "-m", "pytest", ...])` against hermes repo.

**Data Shape:**
- Size constraints per artifact type (config-driven)
- Growth constraint: % increase over baseline (20% max by default)
- Skill structure: checks for YAML frontmatter with `name:` and `description:` fields
- Test suite: runs full pytest, captures stdout for failure reporting

**Notable Patterns:**
- Constraint failures are **fatal**—GEPA never sees failed variants as successes
- Size checks are type-specific (skill ≤15KB, descriptions ≤500 chars)
- Growth limit prevents evolutionary drift toward verbose solutions
- Skill structure validation is lightweight (checks frontmatter presence, not schema validity)

**Hermes Coupling:** Subprocess call to `pytest` assumes tests exist in hermes-agent/tests/.

---

#### `evolution/core/fitness.py`
**Purpose:** Multi-dimensional fitness scoring via LLM-as-judge with rubrics.

**Public API:**
```python
@dataclass
class FitnessScore:
    correctness: float = 0.0  # 0-1
    procedure_following: float = 0.0  # 0-1
    conciseness: float = 0.0  # 0-1
    length_penalty: float = 0.0  # 0-1, ramps 0.9→1.0 size ratio
    feedback: str = ""
    
    @property
    def composite(self) -> float:
        """Weighted: 0.5*correctness + 0.3*procedure + 0.2*conciseness - length_penalty"""

class LLMJudge:
    class JudgeSignature(dspy.Signature):
        task_input: str = dspy.InputField(...)
        expected_behavior: str = dspy.InputField(...)
        agent_output: str = dspy.InputField(...)
        skill_text: str = dspy.InputField(...)
        correctness: float = dspy.OutputField(...)
        procedure_following: float = dspy.OutputField(...)
        conciseness: float = dspy.OutputField(...)
        feedback: str = dspy.OutputField(...)
    
    def __init__(self, config: EvolutionConfig)
    def score(
        task_input: str,
        expected_behavior: str,
        agent_output: str,
        skill_text: str,
        artifact_size: Optional[int] = None,
        max_size: Optional[int] = None,
    ) -> FitnessScore

def skill_fitness_metric(example: dspy.Example, prediction: dspy.Prediction, trace=None) -> float:
    """DSPy-compatible metric (0-1) for GEPA optimization loop."""
```

**Key Collaborators:** `dspy.LM()`, `dspy.ChainOfThought()`, uses `eval_model` for scoring.

**Data Shape:**
- Four-dimensional score: three quality dimensions + one penalty
- Composite formula: weighted average minus length penalty
- Length penalty ramps from 0 at 90% size ratio to 0.3 at 100%+
- Feedback is free-form text for GEPA's reflective analysis

**Notable Patterns:**
- Multi-dimensional scoring decouples concerns (correctness ≠ conciseness)
- Length penalty discourages GEPA from "solving" problems via verbosity
- Fast heuristic metric (`skill_fitness_metric`) uses keyword overlap as proxy
- LLM judge uses `ChainOfThought` for reasoning

**Hermes Coupling:** Assumes `dspy` is installed and configured; LLM calls use external models.

---

#### `evolution/core/dataset_builder.py`
**Purpose:** Generate or load evaluation datasets for skill/tool/prompt optimization.

**Public API:**
```python
@dataclass
class EvalExample:
    task_input: str
    expected_behavior: str  # Rubric, not exact output
    difficulty: str = "medium"  # easy, medium, hard
    category: str = "general"
    source: str = "synthetic"
    
    def to_dict(self) -> dict
    @classmethod
    def from_dict(cls, d: dict) -> "EvalExample"

@dataclass
class EvalDataset:
    train: list[EvalExample]
    val: list[EvalExample]
    holdout: list[EvalExample]
    
    @property
    def all_examples(self) -> list[EvalExample]
    def save(self, path: Path)
    @classmethod
    def load(cls, path: Path) -> "EvalDataset"
    def to_dspy_examples(self, split: str = "train") -> list[dspy.Example]

class SyntheticDatasetBuilder:
    class GenerateTestCases(dspy.Signature):
        artifact_text: str = dspy.InputField(...)
        artifact_type: str = dspy.InputField(...)
        num_cases: int = dspy.InputField(...)
        test_cases: str = dspy.OutputField(...)  # JSON array
    
    def __init__(self, config: EvolutionConfig)
    def generate(
        artifact_text: str,
        artifact_type: str = "skill",
        num_cases: Optional[int] = None,
    ) -> EvalDataset

class GoldenDatasetLoader:
    @staticmethod
    def load(path: Path) -> EvalDataset
```

**Key Collaborators:** `dspy.LM()`, `dspy.ChainOfThought()`, uses `judge_model` for generation.

**Data Shape:**
- EvalExample: task + rubric (not exact output)
- Supports three sources: synthetic (LLM-generated), golden (hand-curated), sessiondb (mined)
- Splits: 50% train, 25% val, 25% holdout
- Persisted as JSONL (one example per line)

**Notable Patterns:**
- Rubrics are *procedures*, not exact outputs (e.g., "identify SQL injection on line 42" not exact string)
- Three-split approach prevents overfitting (holdout is never touched by GEPA)
- Synthetic generation uses strong model (judge_model, typically GPT-4) for quality
- Examples include metadata: difficulty, category, source—useful for analysis

**Hermes Coupling:** Relies on `dspy` for LLM calls; expects reasonably sized eval sets (10-30 examples).

---

#### `evolution/core/external_importers.py`
**Purpose:** Mine real session data from Claude Code, Copilot, and Hermes to bootstrap eval datasets.

**Public API:**
```python
class ClaudeCodeImporter:
    HISTORY_PATH = Path.home() / ".claude" / "history.jsonl"
    @staticmethod
    def extract_messages(limit: int = 0) -> list[dict]

class CopilotImporter:
    SESSION_DIR = Path.home() / ".copilot" / "session-state"
    @staticmethod
    def extract_messages(limit: int = 0) -> list[dict]

class HermesSessionImporter:
    SESSION_DIR = Path.home() / ".hermes" / "sessions"
    @staticmethod
    def extract_messages(limit: int = 0) -> list[dict]

class RelevanceFilter:
    class ScoreRelevance(dspy.Signature):
        skill_name: str = dspy.InputField(...)
        skill_description: str = dspy.InputField(...)
        user_message: str = dspy.InputField(...)
        assistant_response: str = dspy.InputField(...)
        scoring: str = dspy.OutputField(...)  # JSON: {relevant, expected_behavior, difficulty, category}
    
    def __init__(self, model: str)
    def filter_and_score(
        messages: list[dict],
        skill_name: str,
        skill_text: str,
        max_examples: int = 50,
    ) -> list[EvalExample]

def build_dataset_from_external(
    skill_name: str,
    skill_text: str,
    sources: list[str],  # ["claude-code", "copilot", "hermes"]
    output_path: Path,
    model: str,
    max_examples: int = 50,
) -> EvalDataset
```

**Key Collaborators:**
- Reads from `~/.claude/history.jsonl`, `~/.copilot/session-state/*/events.jsonl`, `~/.hermes/sessions/*.json`
- Uses regex-based secret detection to filter sensitive data
- `dspy.ChainOfThought()` for LLM-based relevance scoring

**Data Shape:**
- ClaudeCodeImporter: user messages only (no assistant responses)
- CopilotImporter: user↔assistant pairs from event streams
- HermesSessionImporter: OpenAI-format message lists with tool context
- RelevanceFilter: two-stage pipeline (cheap heuristic + LLM scoring)

**Notable Patterns:**
- **Secret detection:** Hardcoded regex patterns for API keys, tokens, passwords
- **Cheap pre-filter:** Keyword overlap check before LLM scoring
- **Two-stage relevance:** Heuristics first to reduce LLM calls, LLM second for final scoring
- **Error rate reporting:** Logs LLM scoring failures

**Hermes Coupling:** Reads from standard Hermes session storage locations.

---

### Skill Evolution Layer

#### `evolution/skills/skill_module.py`
**Purpose:** Wraps a SKILL.md file as a DSPy module for optimization.

**Public API:**
```python
def load_skill(skill_path: Path) -> dict:
    """Returns: {path, raw, frontmatter, body, name, description}"""

def find_skill(skill_name: str, hermes_agent_path: Path) -> Optional[Path]:
    """Searches hermes_agent_path/skills for SKILL.md by dir name or frontmatter."""

class SkillModule(dspy.Module):
    class TaskWithSkill(dspy.Signature):
        skill_instructions: str = dspy.InputField(...)
        task_input: str = dspy.InputField(...)
        output: str = dspy.OutputField(...)
    
    def __init__(self, skill_text: str)
    def forward(self, task_input: str) -> dspy.Prediction

def reassemble_skill(frontmatter: str, evolved_body: str) -> str:
    """Reconstructs SKILL.md from YAML frontmatter + evolved markdown body."""
```

**Key Collaborators:** `dspy.ChainOfThought()`, reads/writes SKILL.md files.

**Data Shape:**
- Skill file structure: YAML frontmatter (name, description, metadata) + markdown body
- DSPy module: accepts task_input, returns prediction.output
- Skill discovery: searches for SKILL.md recursively or matches frontmatter name field

**Notable Patterns:**
- **Dual parsing:** YAML frontmatter preserved separately from body
- **Two-level skill discovery:** direct directory name match + fuzzy frontmatter match
- **Modular execution:** skill text is injected as "instructions" in the signature
- **Reassembly:** preserves original frontmatter while only body changes

**Hermes Coupling:** Tight coupling to SKILL.md file structure.

---

#### `evolution/skills/evolve_skill.py`
**Purpose:** Main orchestrator—evolves a single skill from end to end.

**Execution Flow:**
1. Load skill from hermes-agent/skills/
2. Build eval dataset (synthetic, golden, or sessiondb)
3. Validate baseline constraints
4. Configure DSPy + GEPA optimizer
5. Run GEPA optimization loop (N iterations)
6. Validate evolved skill constraints
7. Evaluate on holdout set
8. Save outputs (evolved skill, baseline, metrics JSON)

**Key Collaborators:**
- `SkillModule`, `load_skill`, `find_skill`, `reassemble_skill`
- `EvolutionConfig`, `ConstraintValidator`
- `SyntheticDatasetBuilder`, `GoldenDatasetLoader`, `build_dataset_from_external`
- `skill_fitness_metric`, `LLMJudge`
- `dspy.GEPA()` or `dspy.MIPROv2()` as fallback

**Data Shape:**
- Output directory: `output/<skill_name>/<timestamp>/`
  - `evolved_skill.md`: optimized version
  - `baseline_skill.md`: original for comparison
  - `metrics.json`: scores, timings, dataset sizes

**Notable Patterns:**
- **Graceful fallback:** GEPA→MIPROv2 if GEPA unavailable
- **Dual validation:** baseline + evolved constraints
- **Holdout-based evaluation:** final scores on data never seen by optimizer
- **Detailed metrics logging:** includes dataset sources, timing, improvement percentage

---

## Architecture Overview: Data Flow

```
Skill file (SKILL.md)
        │
        ▼
Eval Dataset Generation (synthetic/golden/sessiondb)
        │
        ▼
Constraint Validation (Baseline)
        │
        ▼
DSPy Module Wrapping
        │
        ▼
GEPA Optimization Loop (N iterations)
        │
        ▼
Constraint Validation (Evolved)
        │
        ▼
Holdout Evaluation
        │
        ▼
Output & Reporting
```

---

## Key Design Patterns

### 1. Constraint-First Evolution
Constraints are **hard gates**, not soft objectives:
- Variants that violate any constraint are immediately rejected
- GEPA never learns from failed variants
- Prevents degenerate solutions (e.g., solving problems via verbosity)

### 2. Multi-Dimensional Fitness
Skill quality balances:
- **Correctness** (50%): Did agent solve the task?
- **Procedure Following** (30%): Did it follow the skill's instructions?
- **Conciseness** (20%): Was response appropriately brief?
- **Length Penalty** (applied): Extra cost if approaching size limit

### 3. Rubric-Based Evaluation
Expected behavior is a *rubric* (procedure description), not exact output:
- Rubrics are inherently more flexible and easier for LLMs to evaluate
- Supports natural language evaluation at skill-level granularity

### 4. Two-Stage Relevance Filtering
Importing external session data uses cheap→expensive pipeline:
1. **Heuristic pre-filter:** Keyword overlap (fast)
2. **LLM scoring:** Relevance + metadata generation (thorough)

### 5. Train/Val/Holdout Separation
Three-way split prevents overfitting:
- **Train (50%)**: Used by GEPA for optimization
- **Val (25%)**: Reserved for statistical testing
- **Holdout (25%)**: Final evaluation, never seen by optimizer

---

## Borrow vs. Rebuild Decision Table

| Concept | Current in Hermes | Recommendation for OpenComputer |
|---------|------------------|--------------------------------|
| **Reward/Fitness Function** | LLM-as-judge with rubrics | Borrow pattern, rebuild code |
| **Constraint Validation** | Hard gates (size, growth, structure) | Borrow pattern, rebuild code |
| **Eval Dataset Format** | EvalExample dataclass + JSONL | Borrow format, generalize sources |
| **Skill/Module Abstraction** | SkillModule wraps SKILL.md text | Rebuild as generic artifact abstraction |
| **GEPA Integration** | dspy.GEPA() wrapper | Borrow pattern, build adapter layer |
| **SessionDB Mining** | Hardcoded importers | Rebuild with plugin architecture |
| **Continuous Loop** | Phase 5 (not yet implemented) | Rebuild with monitoring interface |

---

## Cost & Performance Notes

### Typical Optimization Run
- **Eval dataset generation:** 1-2 min synthetic, 10-30 min sessiondb mining
- **GEPA optimization:** 5-20 min for 10 iterations
- **Holdout evaluation:** 1-2 min
- **Total:** ~30-60 minutes per skill, $2-10 cost

---

## License Analysis

**Explicit License:** MIT © 2026 Nous Research

**Dependencies:**
- DSPy: MIT (direct import)
- GEPA: MIT (integrated into DSPy)
- Darwinian Evolver: AGPL v3 (Phase 4, external CLI only)
- click, rich: BSD/MIT

**AGPL Caveat (Phase 4):** Strategy is to invoke Darwinian Evolver as subprocess, not import it. Reduces derivative-work obligation, but keep Phase 4 optional and external if building OpenComputer variant.

---

**Document Generated:** 2026-04-24  
**Source Repo:** `/Users/saksham/Vscode/claude/sources/hermes-agent-self-evolution/`  
**Reference Licensing:** Hermes Agent Self-Evolution is MIT © 2026 Nous Research
