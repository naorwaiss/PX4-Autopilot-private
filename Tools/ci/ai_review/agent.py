"""Run Claude Code headless as the reviewer or the validator.

The agent works on the PR checkout, which is untrusted data:

- --bare: nothing in the checkout configures it (no CLAUDE.md, AGENTS.md,
  hooks or plugins).
- --restricted: settings files are ignored, the file tools are confined to
  the working directory, and code-running tools exist only if --tools
  names them.
- --tools Read,Bash with --allowedTools limited to Read, grep, ls and
  read-only git history (log, blame, show):
  Claude Code checks each Bash call, so chained commands, other programs
  and paths outside the checkout are refused.
- --strict-mcp-config: no MCP servers.
- dontAsk: anything not allowed is refused rather than prompted for.
"""

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

TOOLS = 'Read,Bash'
# git log/blame/show give the history check the review criteria ask for;
# px4-sandbox-python runs model-written code in the locked-down container
ALLOWED_TOOLS = ('Read', 'Bash(grep *)', 'Bash(ls *)', 'Bash(git log *)',
                 'Bash(git blame *)', 'Bash(git show *)')
SANDBOX_TOOL = 'Bash(px4-sandbox-python *)'
SANDBOX_BIN = Path(__file__).resolve().parent / 'cmd'

Runner = Callable[[List[str], str, Path, Mapping[str, str], float],
                  'subprocess.CompletedProcess[str]']

# The task is short and goes on the command line; the PR context can be
# far larger than one argv entry allows (128 KiB on Linux), so it is piped
# on stdin, which claude -p appends to the prompt.
REVIEW_TASK = ('Review the pull request described on stdin, following your '
               'system prompt. Return only the structured result.')
VALIDATE_TASK = ('Check the single review finding described on stdin, '
                 'following your system prompt. Return only the structured '
                 'result.')


class AgentError(RuntimeError):
    pass


@dataclass(frozen=True)
class AgentConfig:
    model: str
    effort: str
    max_turns: int
    timeout_s: float
    # sandbox settings (image, checkout, runtime) as environment variables;
    # empty means the agent gets no code execution at all
    sandbox_env: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentResult:
    output: Any
    cost_usd: float
    usage: Dict[str, Any]


def command(cfg: AgentConfig, system_prompt: Path, schema: Dict[str, Any],
            task: str, claude: str = 'claude') -> List[str]:
    return [
        claude, '--bare', '--restricted', '--strict-mcp-config',
        '-p', task,
        '--output-format', 'json',
        '--json-schema', json.dumps(schema),
        '--append-system-prompt-file', str(system_prompt),
        '--permission-mode', 'dontAsk',
        '--tools', TOOLS,
        '--allowedTools', *ALLOWED_TOOLS,
        *([SANDBOX_TOOL] if cfg.sandbox_env else []),
        '--effort', cfg.effort,
        '--max-turns', str(cfg.max_turns),
    ]


def environment(cfg: AgentConfig,
                base: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    env = dict(os.environ if base is None else base)
    env.update({
        'CLAUDE_CODE_USE_BEDROCK': '1',
        'ANTHROPIC_MODEL': cfg.model,
        # the background model must also be one the IAM role allows
        'ANTHROPIC_DEFAULT_HAIKU_MODEL': cfg.model,
        'DISABLE_AUTOUPDATER': '1',
    })
    # the GitHub token is never needed by the agent
    env.pop('GITHUB_TOKEN', None)
    env.pop('GH_TOKEN', None)
    if cfg.sandbox_env:
        env.update(cfg.sandbox_env)
        env['PATH'] = f'{SANDBOX_BIN}{os.pathsep}{env.get("PATH", "")}'
    return env


def _subprocess_runner(cmd: List[str], stdin: str, cwd: Path,
                       env: Mapping[str, str],
                       timeout: float) -> 'subprocess.CompletedProcess[str]':
    return subprocess.run(cmd, input=stdin, cwd=cwd, env=dict(env),
                          capture_output=True, text=True, timeout=timeout,
                          check=False)


def run(cfg: AgentConfig, system_prompt: Path, schema: Dict[str, Any],
        task: str, context: str, cwd: Path,
        runner: Runner = _subprocess_runner) -> AgentResult:
    cmd = command(cfg, system_prompt, schema, task)
    try:
        proc = runner(cmd, context, cwd, environment(cfg), cfg.timeout_s)
    except subprocess.TimeoutExpired as e:
        raise AgentError(f'agent timed out after {cfg.timeout_s:.0f}s') from e
    if proc.returncode != 0:
        raise AgentError(f'agent exited {proc.returncode}: '
                         f'{proc.stderr.strip()[-2000:]}')
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise AgentError('agent output is not JSON') from e
    if not isinstance(data, dict) or data.get('is_error'):
        raise AgentError(f'agent reported an error: {str(data)[:2000]}')
    if 'structured_output' not in data:
        raise AgentError('agent output has no structured_output')
    return AgentResult(output=data['structured_output'],
                       cost_usd=float(data.get('total_cost_usd') or 0.0),
                       usage=dict(data.get('usage') or {}))
