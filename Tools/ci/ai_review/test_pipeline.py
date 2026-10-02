"""The system prompt must carry the repository rules that apply to the
changed paths, and the offline render path must produce a postable
artifact from saved findings without calling a model."""

import json
import tempfile
import unittest
from pathlib import Path

from ai_review import __main__ as cli
from ai_review import pipeline

ROOT = Path(__file__).resolve().parents[3]
DIFF = """\
diff --git a/src/modules/ekf2/a.cpp b/src/modules/ekf2/a.cpp
--- a/src/modules/ekf2/a.cpp
+++ b/src/modules/ekf2/a.cpp
@@ -1,2 +1,2 @@
 void f() {
-  x = 1;
+  x = 2;
"""


class TestSystemPrompt(unittest.TestCase):

    def test_matches_instructions_by_apply_to(self) -> None:
        names = [p.name for p in pipeline.matching_instructions(
            ROOT, ['src/modules/ekf2/EKF/ekf.cpp'])]
        self.assertIn('estimation.instructions.md', names)
        self.assertNotIn('docs.en.instructions.md', names)

    def test_sandbox_note_only_when_enabled(self) -> None:
        self.assertNotIn('px4-sandbox-python',
                         pipeline.system_prompt(ROOT, []))
        self.assertIn('px4-sandbox-python',
                      pipeline.system_prompt(ROOT, [], sandbox=True))
        self.assertNotIn('px4-sandbox-python', pipeline.validator_prompt())
        self.assertIn('px4-sandbox-python',
                      pipeline.validator_prompt(sandbox=True))

    def test_prompt_includes_criteria_and_agents(self) -> None:
        text = pipeline.system_prompt(ROOT, ['docs/en/index.md'])
        self.assertIn('PX4 review criteria', text)
        self.assertIn('docs.en.instructions.md', text)
        self.assertIn('Automated PX4 pull request review', text)


class TestRender(unittest.TestCase):

    def test_render_saved_findings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            checkout = t / 'pr'
            src = checkout / 'src/modules/ekf2'
            src.mkdir(parents=True)
            (src / 'a.cpp').write_text('void f() {\n  x = 2;\n')
            (t / 'pr.diff').write_text(DIFF)
            (t / 'findings.json').write_text(json.dumps({
                'summary': 'Changes x.',
                'pairs': [{
                    'finding': {
                        'path': 'src/modules/ekf2/a.cpp', 'line': 2,
                        'start_line': None, 'severity': 'blocker',
                        'title': 'Wrong value', 'body': 'b', 'trigger': 't',
                        'suggestion': 'Restore 1.', 'replacement': '  x = 1;',
                        'uncertainty': '', 'rule': None},
                    'verdict': {'keep': True, 'confidence': 'high',
                                'reason': 'traced'}}]}))
            rc = cli.main([
                'render', '--pr', '5', '--trusted-root', str(ROOT),
                '--checkout', str(checkout), '--work-dir', str(t / 'w'),
                '--out-dir', str(t / 'out'), '--findings',
                str(t / 'findings.json'), '--diff', str(t / 'pr.diff'),
                '--head-sha', 'c' * 40])
            self.assertEqual(rc, 0)
            comments = json.loads((t / 'out/comments.json').read_text())
            self.assertEqual(len(comments), 1)
            self.assertIn('```suggestion', comments[0]['body'])
            manifest = json.loads((t / 'out/manifest.json').read_text())
            self.assertIn('blocking issues', manifest['summary'])


if __name__ == '__main__':
    unittest.main()
