"""Command line entry point: python3 -m ai_review {run,render} ...

Run from Tools/ci (the workflow sets working-directory) so the package is
importable.
"""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional

from . import agent, findings, harden, pipeline, render, sandbox

# Bedrock global inference profiles the px4-ai-review IAM role may invoke
MODELS = {
    'opus': ('global.anthropic.claude-opus-5-5', 'Claude Opus 5.5'),
    'sonnet': ('global.anthropic.claude-sonnet-5-5', 'Claude Sonnet 5.5'),
}
EFFORTS = ('low', 'medium', 'high', 'xhigh', 'max')
SECRET_ENV = ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY',
              'AWS_SESSION_TOKEN', 'GITHUB_TOKEN', 'GH_TOKEN')


def _sandbox(args: argparse.Namespace) -> Optional[sandbox.SandboxConfig]:
    if not args.sandbox_image:
        return None
    return sandbox.SandboxConfig(image=args.sandbox_image,
                                 checkout=str(Path(args.checkout).resolve()),
                                 runtime=args.sandbox_runtime or None)


def _sandbox_env(args: argparse.Namespace) -> Dict[str, str]:
    cfg = _sandbox(args)
    if cfg is None:
        return {}
    env = {sandbox.ENV_IMAGE: cfg.image, sandbox.ENV_CHECKOUT: cfg.checkout}
    if cfg.runtime:
        env[sandbox.ENV_RUNTIME] = cfg.runtime
    return env


def _settings(args: argparse.Namespace) -> pipeline.Settings:
    model_id, label = MODELS[args.model]
    validator_id, _ = MODELS[args.validator_model]
    sandbox_env = _sandbox_env(args)
    return pipeline.Settings(
        repo=args.repo,
        pr_number=args.pr,
        trusted_root=Path(args.trusted_root).resolve(),
        checkout=Path(args.checkout).resolve(),
        work_dir=Path(args.work_dir).resolve(),
        out_dir=Path(args.out_dir).resolve(),
        reviewer=agent.AgentConfig(model=model_id, effort=args.effort,
                                   max_turns=args.max_turns,
                                   timeout_s=args.timeout,
                                   sandbox_env=sandbox_env),
        validator=agent.AgentConfig(model=validator_id,
                                    effort=args.validator_effort,
                                    max_turns=20, timeout_s=600,
                                    sandbox_env=sandbox_env),
        model_label=label,
        max_changed_lines=args.max_changed_lines,
    )


def _guard_leaks(out_dir: Path, work_dir: Path) -> None:
    """Delete the outputs and fail if any credential appears in them."""
    files = [out_dir / 'manifest.json', out_dir / 'comments.json',
             work_dir / 'report.md']
    texts = [p.read_text(encoding='utf-8') for p in files if p.exists()]
    secrets = [os.environ.get(k, '') for k in SECRET_ENV]
    leaks = render.find_leaks(texts, secrets)
    if leaks:
        for p in files:
            p.unlink(missing_ok=True)
        sys.exit('error: review output contains credentials '
                 f'({", ".join(leaks)}); nothing was written')


def _write_step_summary(report: str) -> None:
    path = os.environ.get('GITHUB_STEP_SUMMARY')
    if path:
        with open(path, 'a', encoding='utf-8') as fh:
            fh.write(report)


def cmd_run(args: argparse.Namespace) -> int:
    s = _settings(args)
    data = pipeline.run(s)
    _guard_leaks(s.out_dir, s.work_dir)
    _write_step_summary(data['report'])
    print(data['report'])
    return 0


def cmd_harden(args: argparse.Namespace) -> int:
    cfg = _sandbox(args)
    if cfg is None:
        sys.exit('error: --sandbox-image is required')
    try:
        harden.harden(cfg)
    except harden.HardenError as e:
        sys.exit(f'error: {e}')
    print(f'runner hardened: sandbox {cfg.image} '
          f'(runtime {cfg.runtime or "default"}), metadata service blocked')
    return 0


def cmd_render(args: argparse.Namespace) -> int:
    """Re-render saved findings without calling any model."""
    s = _settings(args)
    saved = json.loads(Path(args.findings).read_text(encoding='utf-8'))
    pairs = [(findings.parse_finding(p['finding']),
              findings.parse_verdict(p['verdict'])) for p in saved['pairs']]
    diff_text = Path(args.diff).read_text(encoding='utf-8')
    checklist = (findings.parse_checklist(saved['checklist'])
                 if saved.get('checklist') else None)
    data = pipeline.finish(s, args.pr, args.head_sha, diff_text,
                           saved.get('summary', ''), pairs, {}, checklist)
    _guard_leaks(s.out_dir, s.work_dir)
    print(data['report'])
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog='ai_review', description=__doc__)
    sub = p.add_subparsers(dest='command', required=True)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument('--repo', default=os.environ.get(
            'GITHUB_REPOSITORY', 'PX4/PX4-Autopilot'))
        sp.add_argument('--pr', type=int, required=True)
        sp.add_argument('--trusted-root', required=True,
                        help='checkout of the base branch (prompts, rules)')
        sp.add_argument('--checkout', required=True,
                        help='checkout of the PR head (data only)')
        sp.add_argument('--work-dir', default='ai-review-work')
        sp.add_argument('--out-dir', default='pr-review')
        sp.add_argument('--model', choices=MODELS, default='opus')
        sp.add_argument('--effort', choices=EFFORTS, default='xhigh')
        sp.add_argument('--validator-model', choices=MODELS,
                        default='opus')
        sp.add_argument('--validator-effort', choices=EFFORTS,
                        default='high')
        sp.add_argument('--max-turns', type=int, default=80)
        sp.add_argument('--timeout', type=float, default=1500)
        sp.add_argument('--max-changed-lines', type=int, default=3000)
        sandbox_args(sp)

    def sandbox_args(sp: argparse.ArgumentParser) -> None:
        sp.add_argument('--sandbox-image', default='',
                        help='container image for model-written code; '
                        'without it the agent cannot run code')
        sp.add_argument('--sandbox-runtime', default='',
                        help='container runtime, e.g. runsc (gVisor)')

    run = sub.add_parser('run', help='review a PR end to end')
    common(run)
    run.set_defaults(func=cmd_run)

    hard = sub.add_parser('harden', help='prepare a CI runner: install '
                          'gVisor, block the metadata service, self-test')
    hard.add_argument('--checkout', required=True)
    sandbox_args(hard)
    hard.set_defaults(func=cmd_harden)

    rend = sub.add_parser('render', help='re-render saved findings offline')
    common(rend)
    rend.add_argument('--findings', required=True)
    rend.add_argument('--diff', required=True)
    rend.add_argument('--head-sha', required=True)
    rend.set_defaults(func=cmd_render)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = parser().parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == '__main__':
    sys.exit(main())
