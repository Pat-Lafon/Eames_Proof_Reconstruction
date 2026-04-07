# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "anthropic>=0.89.0",
#     "python-dotenv>=1.0.0",
# ]
# ///
"""
Phase 0: LLM Proof Reconstruction Feasibility Test

For each test case directory containing a goal.lean file, asks Claude to produce
a Lean 4 tactic proof under three conditions:
  1. Unguided      — only the Lean goal
  2. Alethe-guided — goal + cvc5 Alethe proof
  3. Z3-guided     — goal + Z3 native proof

Then checks each generated proof with the Lean kernel.

Test case layout:
  <name>/
    goal.lean          — Lean theorem with sorry (required)
    query.smt2         — SMT-LIB encoding (optional, for reference)
    proof_alethe.txt   — cvc5 Alethe proof output (optional)
    proof_z3.txt       — Z3 native proof output (optional)

Usage: uv run experiments/phase0/run_experiment.py
"""

import anthropic
import json
import os
import re
import subprocess
import sys
from pathlib import Path

PHASE0_DIR = Path(__file__).parent
MODEL = "claude-sonnet-4-20250514"

# Prompt design draws from existing LLM proof generation work:
# - COPRA (Thakur et al. 2024): structured [GOAL]/[HYPOTHESES]/[THEOREMS] tags for in-context learning
# - DSP (Jiang et al. 2023): external proof as a "sketch" guiding formal generation
# - Baldur (First et al. 2023): whole-proof generation, error-message repair loop
# - ReProver (Yang et al. 2023): retrieved premises prepended to tactic state
# - GPT-f (Polu & Sutskever 2020): goal-only input (our unguided baseline)
#
# Our novelty: providing a *formal* solver proof artifact as guidance via [SMT PROOF] tag.
# No existing system does this — DSP uses informal proofs, ReProver/COPRA use premises.

SYSTEM_PROMPT = """\
<role>
You are a Lean 4 proof assistant. You produce tactic proofs that are checked by Lean's kernel.
</role>

<context>
Proofs are checked with standalone `lean` (no project, no Mathlib), so only core Lean 4 tactics \
are available: omega, simp, exact, intro, apply, congr, subst, rw, cases, induction, constructor, \
have, show, calc, etc. Do not use sorry or native_decide on Prop.

When an [SMT PROOF] section is provided, it contains a machine-checked proof from an SMT solver \
(cvc5 or Z3) that the negation of the goal is unsatisfiable. Use it as a roadmap for your proof — \
follow the reasoning structure but translate it into idiomatic Lean 4 tactics. You do not need to \
replicate every step.
</context>

<output_format>
Respond with ONLY a ```lean code block containing the tactic body (what goes after `by`). \
No explanation or commentary.
</output_format>"""

# TODO: Add few-shot examples to the system prompt. Per Anthropic's guidance and the
# DSP/COPRA literature, 3-5 concrete input/output examples are the strongest lever for
# steering output quality. Candidates:
#   - Simple propositional: goal → `exact hpq hp`
#   - Arithmetic: goal → `omega`
#   - Congruence: goal → `subst h; rfl`
#   - SMT-guided: goal + Alethe proof → tactic proof (once we have successful reconstructions)


def discover_test_cases() -> list[dict]:
    """Find all test case directories containing a goal.lean file."""
    cases = []
    for d in sorted(PHASE0_DIR.iterdir()):
        if d.is_dir() and (d / "goal.lean").exists():
            goal_text = (d / "goal.lean").read_text().strip()
            goal_sig = goal_text.split(":= by")[0].strip()
            cases.append({"name": d.name, "goal": goal_sig, "dir": d})
    return cases


def make_unguided_prompt(goal: str) -> str:
    return f"""\
[GOAL]
{goal} := by
  sorry"""


def make_guided_prompt(goal: str, proof: str, format_name: str) -> str:
    return f"""\
[GOAL]
{goal} := by
  sorry

[SMT PROOF ({format_name})]
The negation of this goal is unsatisfiable. Below is the solver's proof.
Use it as a roadmap — you do not need to replicate every step, but follow its reasoning structure.

{proof}"""


# Pricing per million tokens (USD) — update if model changes
PRICING = {
    "claude-sonnet-4-20250514": {"input": 3.00, "output": 15.00},
}


def call_claude(prompt: str) -> dict:
    """Send a prompt to Claude and return a dict with response, stop_reason, and usage."""
    client = anthropic.Anthropic()
    message = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
    )
    input_tokens = message.usage.input_tokens
    output_tokens = message.usage.output_tokens
    prices = PRICING.get(MODEL, {"input": 0, "output": 0})
    cost = (input_tokens * prices["input"] + output_tokens * prices["output"]) / 1_000_000

    block = message.content[0]
    text = block.text if isinstance(block, anthropic.types.TextBlock) else ""

    return {
        "text": text,
        "stop_reason": message.stop_reason,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost": cost,
    }


class ExtractionError(ValueError):
    """Raised when we cannot extract a valid proof from the LLM response."""


def extract_lean_proof(response: str) -> str:
    """Extract the tactic proof from a ```lean code block.

    Raises ExtractionError if the response is empty or contains no recognizable proof.
    """
    if not response or not response.strip():
        raise ExtractionError("LLM returned an empty response")

    # Try ```lean block first
    match = re.search(r"```lean\s*\n(.*?)```", response, re.DOTALL)
    if match:
        proof = match.group(1).strip()
        if not proof:
            raise ExtractionError("Found ```lean block but it was empty")
        return proof

    # Try any code block
    match = re.search(r"```\s*\n(.*?)```", response, re.DOTALL)
    if match:
        proof = match.group(1).strip()
        if not proof:
            raise ExtractionError("Found code block but it was empty")
        print("[WARN: no ```lean block, fell back to generic code block] ", end="")
        return proof

    raise ExtractionError(
        f"No code block found in response. First 200 chars: {response[:200]!r}"
    )


def check_lean_proof(goal: str, proof_body: str) -> tuple[bool, str]:
    """Write a Lean file with the proof and check it with lean."""
    if not proof_body or not proof_body.strip():
        return False, "Empty proof body"

    if "sorry" in proof_body.lower():
        return False, "Proof contains sorry"

    lean_content = f"{goal} := by\n"
    for line in proof_body.split("\n"):
        lean_content += f"  {line}\n"

    check_file = PHASE0_DIR / "_check_tmp.lean"
    check_file.write_text(lean_content)

    try:
        result = subprocess.run(
            ["lean", str(check_file)],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(PHASE0_DIR),
        )
        output = (result.stdout + result.stderr).strip()
        success = result.returncode == 0 and "sorry" not in output.lower()
        return success, output
    except subprocess.TimeoutExpired:
        return False, "TIMEOUT (60s)"
    except FileNotFoundError:
        return False, "ERROR: `lean` not found on PATH"
    finally:
        check_file.unlink(missing_ok=True)


def run_condition(case: dict, condition: str, prompt: str) -> dict:
    """Run one condition for one test case."""
    print(f"    {condition}... ", end="", flush=True)

    # Call LLM
    try:
        result = call_claude(prompt)
    except anthropic.APIError as e:
        print(f"API_ERROR ({e})")
        return {
            "condition": condition,
            "status": "API_ERROR",
            "error": str(e),
            "proof_body": "",
            "lean_output": "",
            "input_tokens": 0,
            "output_tokens": 0,
            "cost": 0.0,
        }

    cost_str = f"${result['cost']:.4f} ({result['input_tokens']}in/{result['output_tokens']}out)"

    # Save raw response regardless of extraction success
    case_dir = case["dir"]
    (case_dir / f"llm_response_{condition}.txt").write_text(result["text"])

    # Check for truncation
    if result["stop_reason"] == "max_tokens":
        (case_dir / f"llm_proof_{condition}.lean").write_text("")
        print(f"TRUNCATED [{cost_str}]")
        return {
            "condition": condition,
            "status": "TRUNCATED",
            "proof_body": "",
            "lean_output": "Response was truncated (hit max_tokens)",
            "input_tokens": result["input_tokens"],
            "output_tokens": result["output_tokens"],
            "cost": result["cost"],
        }

    # Extract proof
    try:
        proof_body = extract_lean_proof(result["text"])
    except ExtractionError as e:
        (case_dir / f"llm_proof_{condition}.lean").write_text("")
        print(f"EXTRACT_FAIL [{cost_str}] — {e}")
        return {
            "condition": condition,
            "status": "EXTRACT_FAIL",
            "error": str(e),
            "proof_body": "",
            "lean_output": "",
            "input_tokens": result["input_tokens"],
            "output_tokens": result["output_tokens"],
            "cost": result["cost"],
        }

    (case_dir / f"llm_proof_{condition}.lean").write_text(proof_body)

    # Check with Lean
    success, lean_output = check_lean_proof(case["goal"], proof_body)
    status = "PASS" if success else "FAIL"
    print(f"{status} [{cost_str}]")

    return {
        "condition": condition,
        "status": status,
        "proof_body": proof_body,
        "lean_output": lean_output[:500],
        "input_tokens": result["input_tokens"],
        "output_tokens": result["output_tokens"],
        "cost": result["cost"],
    }


def run_test_case(case: dict) -> list[dict]:
    """Run all three conditions for one test case."""
    case_dir = case["dir"]
    goal = case["goal"]

    print(f"\n  === {case['name']} ===")

    results = []

    # Unguided
    results.append(run_condition(case, "unguided", make_unguided_prompt(goal)))

    # Alethe-guided
    alethe_path = case_dir / "proof_alethe.txt"
    if alethe_path.exists():
        proof = alethe_path.read_text()
        results.append(
            run_condition(
                case, "alethe", make_guided_prompt(goal, proof, "Alethe (cvc5)")
            )
        )
    else:
        print("    alethe... SKIP (no proof file)")

    # Z3-guided
    z3_path = case_dir / "proof_z3.txt"
    if z3_path.exists():
        proof = z3_path.read_text()
        results.append(
            run_condition(case, "z3", make_guided_prompt(goal, proof, "Z3 native"))
        )
    else:
        print("    z3... SKIP (no proof file)")

    return results


def update_results_md(test_cases: list[dict], all_results: dict, all_details: dict, total_cost: float):
    """Write results.md with experiment outcomes."""
    lines = [
        "# Phase 0 Results: LLM Proof Reconstruction Feasibility\n",
        "\n## Setup\n",
        f"- **Solvers:** cvc5 1.2.1 (Alethe), Z3 4.15.2 (native)\n",
        f"- **LLM:** Claude ({MODEL})\n",
        "- **Lean:** 4.29.0\n",
        "- **Temperature:** 0\n",
        f"- **Total API cost:** ${total_cost:.4f}\n",
        "\n## Proof sizes (lines)\n",
        "\n| Test case | cvc5 Alethe | Z3 native |\n",
        "|-----------|-------------|----------|\n",
    ]

    for tc in test_cases:
        case_dir = tc["dir"]
        alethe_lines = (
            len((case_dir / "proof_alethe.txt").read_text().splitlines())
            if (case_dir / "proof_alethe.txt").exists()
            else "—"
        )
        z3_lines = (
            len((case_dir / "proof_z3.txt").read_text().splitlines())
            if (case_dir / "proof_z3.txt").exists()
            else "—"
        )
        lines.append(f"| {tc['name']} | {alethe_lines} | {z3_lines} |\n")

    lines.append("\n## Reconstruction results\n")
    lines.append("\n| Test case | Unguided | Alethe-guided | Z3-guided |\n")
    lines.append("|-----------|----------|---------------|----------|\n")

    for tc in test_cases:
        r = all_results.get(tc["name"], {})
        d = all_details.get(tc["name"], {})
        cells = []
        for cond in ["unguided", "alethe", "z3"]:
            status = r.get(cond, "—")
            detail = d.get(cond, {})
            cost = detail.get("cost")
            if cost is not None:
                cells.append(f"{status} (${cost:.4f})")
            else:
                cells.append(status)
        lines.append(f"| {tc['name']} | {cells[0]} | {cells[1]} | {cells[2]} |\n")

    lines.append("\n## Generated proofs\n")
    for tc in test_cases:
        case_dir = tc["dir"]
        lines.append(f"\n### {tc['name']}\n")
        lines.append(f"\n**Goal:** `{tc['goal']}`\n")

        for condition in ["unguided", "alethe", "z3"]:
            proof_file = case_dir / f"llm_proof_{condition}.lean"
            if proof_file.exists():
                proof = proof_file.read_text()
                status = all_results.get(tc["name"], {}).get(condition, "—")
                lines.append(f"\n**{condition}** ({status}):\n```lean\n{proof}\n```\n")

    (PHASE0_DIR / "results.md").write_text("".join(lines))


def main():
    # Load .env from the phase0 directory or project root
    from dotenv import load_dotenv

    load_dotenv(PHASE0_DIR / ".env")
    load_dotenv(PHASE0_DIR.parent.parent / ".env")

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("Error: ANTHROPIC_API_KEY not set.")
        print("  Option 1: Create experiments/phase0/.env with ANTHROPIC_API_KEY=sk-ant-...")
        print("  Option 2: export ANTHROPIC_API_KEY=sk-ant-...")
        sys.exit(1)

    test_cases = discover_test_cases()
    if not test_cases:
        print(
            "No test cases found. Each test case needs a directory with a goal.lean file."
        )
        sys.exit(1)

    print("Phase 0: LLM Proof Reconstruction Feasibility Test")
    print(f"Model: {MODEL}")
    print(
        f"Test cases: {len(test_cases)} ({', '.join(tc['name'] for tc in test_cases)})"
    )
    print(f"Conditions: unguided, alethe-guided, z3-guided")

    all_results = {}
    all_details = {}  # name -> condition -> full result dict (for JSON export)
    total_cost = 0.0

    for tc in test_cases:
        case_results = run_test_case(tc)
        all_results[tc["name"]] = {r["condition"]: r["status"] for r in case_results}
        all_details[tc["name"]] = {r["condition"]: r for r in case_results}
        total_cost += sum(r.get("cost", 0) for r in case_results)

    # Summary
    print("\n\n=== SUMMARY ===\n")
    print(f"{'Test case':<25} {'Unguided':<12} {'Alethe':<12} {'Z3':<12}")
    print("-" * 61)
    for tc in test_cases:
        r = all_results.get(tc["name"], {})
        print(
            f"{tc['name']:<25} {r.get('unguided', '—'):<12} {r.get('alethe', '—'):<12} {r.get('z3', '—'):<12}"
        )
    print(f"\nTotal cost: ${total_cost:.4f}")

    update_results_md(test_cases, all_results, all_details, total_cost)
    print(f"Detailed results written to {PHASE0_DIR / 'results.md'}")

    (PHASE0_DIR / "results.json").write_text(json.dumps(all_details, indent=2, default=str))


if __name__ == "__main__":
    main()
