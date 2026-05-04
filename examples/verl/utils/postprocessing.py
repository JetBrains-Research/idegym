import re
from typing import Any

from idegym.api.tools.bash import BashCommandResponse


def apply_reasoning_filter(content: str, max_turns: int) -> str:
    """Remove a leading <think>...</think> reasoning trace from content string."""
    filtered, num_substitutions = re.subn(r"^.*?</think>", "", content, flags=re.DOTALL)
    if num_substitutions > 0:
        return filtered.strip("\n")

    stripped = content.strip()
    if stripped.startswith("<think>"):
        return content if max_turns == 1 else "The thinking was too long. I should be more concise next time."

    return content


def extract_bash_output(cmd_output: BashCommandResponse | dict) -> str:
    """Combine stdout and stderr into a single output string."""
    if isinstance(cmd_output, dict):
        stdout = cmd_output.get("stdout")
        stderr = cmd_output.get("stderr")
    elif isinstance(cmd_output, BashCommandResponse):
        stdout = cmd_output.stdout or ""
        stderr = cmd_output.stderr or ""
    else:
        raise ValueError(f"Invalid command output type: {type(cmd_output)}")
    return f"{stdout}\n{stderr}".strip()


def parse_idegym_tests_output(output: str) -> dict[str, Any]:
    test_pattern = re.compile(
        r"^(={70})\n"
        r"([A-Z]+):\s"  # ===== header  # Status, eg: ERROR:
        r"([^\s]+)\s+\(([^)]+)\)\n"  # Test method and module path in parentheses
        r"(?:.*?)"
        r"[-]{70}\n"  # ------
        r"(.*?)(?=\n[=-]{70}|\Z)",  # Stacktrace (non-greedy, up to next ===== or end of file
        re.DOTALL | re.MULTILINE,
    )
    # Pattern for summary statistics at end of file
    summary_pattern = re.compile(
        r"Ran (\d+) tests? in [\d.]+s\s*\n\s*"  # Ran X tests in Y.ZZZs
        r"([A-Z]+)(?: \(([^)]+)\))?",  # FAILED or OK, optionally with details in parentheses
        re.MULTILINE,
    )
    results = []
    for match in test_pattern.finditer(output):
        status = match.group(2)
        test_method = match.group(3)  # Just the method name (test_i18n_app_dirs)
        # Module path (i18n.tests.WatchForTranslationChangesTests)
        module_path = match.group(4)
        full_test_path = f"{module_path}.{test_method}"  # Complete test path
        stacktrace = match.group(5)
        results.append(
            {
                "test_full_path": full_test_path,
                "status": status,
                "test_method": test_method,
                "module_path": module_path,
                "stacktrace": stacktrace.strip(),
            }
        )

    # Extract summary information
    summary_match = summary_pattern.search(output)
    if summary_match:
        total_tests = int(summary_match.group(1))
        overall_status = summary_match.group(2)  # "FAILED" or "OK"
        details_str = summary_match.group(3)
        # e.g. "failures=1, errors=14, skipped=998, expected failures=4"

        # Parse the details into a dictionary
        details = {}
        if details_str:
            for item in details_str.split(", "):
                key, value = item.split("=")
                details[key] = int(value)

        summary_info = {
            "total_tests": total_tests,
            "status": overall_status,
            "details": details,
        }
    else:
        summary_info = None

    return {"tests": results, "summary": summary_info}


def get_percentage_passed(test_results: dict) -> float:
    """Extract test pass percentage from a processed test result dict."""
    summary = test_results.get("summary", {})
    if summary is None:
        return 0.0
    total_tests = summary.get("total_tests", 0)
    details = summary.get("details", {})
    if (details is None) or (total_tests is None) or (total_tests == 0):
        return 0.0

    # Guard against under-reported total_tests
    total_tests = max((details.get("failures", 0) + details.get("errors", 0)), total_tests)
    passed_tests = total_tests - (details.get("failures", 0) + details.get("errors", 0))
    return (passed_tests / total_tests) if total_tests > 0 else 0.0
