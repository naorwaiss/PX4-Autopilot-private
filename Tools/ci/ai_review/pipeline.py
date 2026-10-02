"""Review pipeline: gather, review, validate each finding, route, render.

Trusted inputs (prompts, review criteria, repository instructions) are
read from the base checkout. The PR checkout is only ever data the agent
reads.
"""

import fnmatch
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import agent, context, diff, findings, render, route

PROMPTS = Path(__file__).resolve().parent / 'prompts'
CRITERIA = Path('.agents/skills/review-pr/review-criteria.md')
INSTRUCTIONS = Path('.github/instructions')
APPLY_TO_RE = re.compile(r'^applyTo:\s*"([^"]*)"', re.MULTILINE)


@dataclass(frozen=True)
class Settings:
    repo: str
    pr_number: int
    trusted_root: Path
    checkout: Path
    work_dir: Path
    out_dir: Path
    reviewer: agent.AgentConfig
    validator: agent.AgentConfig
    model_label: str
    max_changed_lines: int


def matching_instructions(trusted_root: Path, files: List[str]) -> List[Path]:
    """Instruction files whose applyTo globs match any changed file."""
    matched = []
    for path in sorted((trusted_root / INSTRUCTIONS).glob(
            '*.instructions.md')):
        m = APPLY_TO_RE.search(path.read_text(encoding='utf-8'))
        if not m:
            continue
        globs = [g.strip() for g in m.group(1).split(',') if g.strip()]
        if any(fnmatch.fnmatch(f, g) for f in files for g in globs):
            matched.append(path)
    return matched


def _sandbox_note(sandbox: bool) -> List[str]:
    # only mention code execution when the agent actually has it
    if not sandbox:
        return []
    return [(PROMPTS / 'sandbox.md').read_text(encoding='utf-8')]


def system_prompt(trusted_root: Path, files: List[str],
                  sandbox: bool = False) -> str:
    parts = [(PROMPTS / 'reviewer.md').read_text(encoding='utf-8'),
             *_sandbox_note(sandbox),
             (trusted_root / CRITERIA).read_text(encoding='utf-8'),
             (trusted_root / 'AGENTS.md').read_text(encoding='utf-8')]
    for path in matching_instructions(trusted_root, files):
        rel = path.relative_to(trusted_root)
        parts.append(f'## Repository instructions: {rel}\n\n'
                     + path.read_text(encoding='utf-8'))
    return '\n\n---\n\n'.join(parts)


def validator_prompt(sandbox: bool = False) -> str:
    parts = [(PROMPTS / 'validator.md').read_text(encoding='utf-8'),
             *_sandbox_note(sandbox)]
    return '\n\n---\n\n'.join(parts)


def _validate_all(s: Settings, pr: context.PrContext,
                  review: findings.Review,
                  usage: Dict[str, Any]) -> List[Tuple[findings.Finding,
                                                       findings.Verdict]]:
    prompt_file = s.work_dir / 'validator-prompt.md'
    prompt_file.write_text(validator_prompt(bool(s.validator.sandbox_env)),
                           encoding='utf-8')
    pairs = []
    for f in review.findings:
        try:
            result = agent.run(
                s.validator, prompt_file, findings.VERDICT_SCHEMA,
                agent.VALIDATE_TASK,
                context.validator_input(f.to_json(), pr),
                s.checkout)
            verdict = findings.parse_verdict(result.output)
            _add_usage(usage, result)
        except (agent.AgentError, findings.ContractError) as e:
            # an unchecked finding never reaches the PR inline
            verdict = findings.Verdict(keep=True, confidence='low',
                                       reason=f'validation failed: {e}')
        pairs.append((f, verdict))
    return pairs


def _add_usage(usage: Dict[str, Any], result: agent.AgentResult) -> None:
    usage['calls'] = usage.get('calls', 0) + 1
    usage['cost_usd'] = usage.get('cost_usd', 0.0) + result.cost_usd
    for key, value in result.usage.items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            usage[key] = usage.get(key, 0) + value


def run(s: Settings) -> Dict[str, Any]:
    """Run the whole pipeline. Returns the report data it also writes."""
    s.work_dir.mkdir(parents=True, exist_ok=True)
    pr = context.gather(s.repo, s.pr_number)
    usage: Dict[str, Any] = {}
    diff_map = diff.parse(pr.diff)

    skip = None
    if pr.diff_too_large or diff_map.changed_lines > s.max_changed_lines:
        skip = (f'the diff is too large for automated review (more than '
                f'{s.max_changed_lines} changed lines)')
    elif not pr.diff.strip():
        skip = 'the diff is empty'

    if skip:
        review = findings.Review(summary=f'Not reviewed: {skip}.',
                                 findings=[])
        pairs: List[Tuple[findings.Finding, findings.Verdict]] = []
    else:
        prompt_file = s.work_dir / 'system-prompt.md'
        prompt_file.write_text(
            system_prompt(s.trusted_root, pr.files,
                          sandbox=bool(s.reviewer.sandbox_env)),
            encoding='utf-8')
        result = agent.run(s.reviewer, prompt_file, findings.REVIEW_SCHEMA,
                           agent.REVIEW_TASK, context.reviewer_input(pr),
                           s.checkout)
        _add_usage(usage, result)
        # keep the raw output so a parse failure never loses a paid review
        (s.work_dir / 'reviewer-output.json').write_text(
            json.dumps(result.output, indent=2))
        review = findings.parse_review(result.output)
        pairs = _validate_all(s, pr, review, usage)

    (s.work_dir / 'findings.json').write_text(json.dumps({
        'summary': review.summary,
        'checklist': {k: e.__dict__ for k, e in review.checklist.items()},
        'pairs': [{'finding': f.to_json(), 'verdict': v.to_json()}
                  for f, v in pairs],
        'rejected': [{'item': item, 'reason': why}
                     for item, why in review.rejected],
    }, indent=2))
    usage['rejected_findings'] = len(review.rejected)
    data = finish(s, pr.number, pr.head_sha, pr.diff, review.summary, pairs,
                  usage, review.checklist)
    return data


def finish(s: Settings, pr_number: int, head_sha: str, diff_text: str,
           model_summary: str,
           pairs: List[Tuple[findings.Finding, findings.Verdict]],
           usage: Dict[str, Any],
           checklist: Optional[Dict[str, findings.ChecklistEntry]] = None
           ) -> Dict[str, Any]:
    """Deterministic tail of the pipeline, also used by `render`."""
    s.work_dir.mkdir(parents=True, exist_ok=True)
    diff_map = diff.parse(diff_text)
    head_files = route.load_head_files(
        s.checkout, sorted({f.path for f, _ in pairs if f.path}))
    routed = route.route(pairs, diff_map, head_files)
    artifact = render.write_artifact(s.out_dir, pr_number, head_sha, routed,
                                     model_summary, s.model_label, checklist)
    report = render.report_markdown(routed, artifact, usage, checklist)
    (s.work_dir / 'report.md').write_text(report, encoding='utf-8')
    (s.work_dir / 'usage.json').write_text(json.dumps(usage, indent=2))
    return {'routed': routed, 'artifact': artifact, 'report': report,
            'usage': usage}
