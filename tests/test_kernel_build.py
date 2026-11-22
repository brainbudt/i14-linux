import hashlib
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class KernelBuildPreflightTests(unittest.TestCase):
    """Exercise the real shell preflight against a real disposable Git tree."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.src = self.root / 'linux'
        self.src.mkdir()
        self.board = 'arch/arm64/boot/dts/mediatek/mt6735-i14-bringup.dts'
        (self.src / self.board).parent.mkdir(parents=True)
        (self.src / 'Makefile').write_text('fixture kernel\n')
        self.git('init', '-q')
        self.git('add', 'Makefile')
        self.git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                 '-c', 'commit.gpgsign=false', '-c', 'core.hooksPath=/dev/null',
                 'commit', '-qm', 'fixture')
        revision = self.git('rev-parse', 'HEAD').strip()
        project = self.root / 'project'
        (project / 'scripts').mkdir(parents=True)
        (project / 'kernel').mkdir()
        self.board_source = project / 'kernel/mt6735-i14-bringup.dts'
        self.board_source.write_text('/dts-v1/; / { model = "fixture I14"; };\n')
        self.config = self.root / 'nura.config'
        self.config.write_text('CONFIG_FIXTURE=y\n')
        script = (ROOT / 'scripts/build-kernel.sh').read_text()
        script = re.sub(r'^revision=.*$', 'revision=' + revision, script, flags=re.M)
        script = re.sub(r'^base_hash=.*$', 'base_hash=' + hashlib.sha512(self.config.read_bytes()).hexdigest(),
                        script, flags=re.M)
        # Stop at the build boundary: this test checks source custody/staging,
        # while the real LLVM build separately verifies compilation.
        script = script.split('mkdir -p "$out"', 1)[0] + '\nprintf "DTB=%s\\n" "$dtb"\n'
        self.script = project / 'scripts/build-kernel.sh'
        self.script.write_text(script)
        tools = self.root / 'tools'
        tools.mkdir()
        # The production recipe targets Linux; emulate only these GNU utilities
        # on macOS, leaving Git, cmp and the shell logic real.
        (tools / 'realpath').write_text('#!/usr/bin/env python3\nimport pathlib,sys\nprint(pathlib.Path(sys.argv[-1]).resolve())\n')
        (tools / 'sha512sum').write_text('#!/usr/bin/env python3\nimport hashlib,pathlib,sys\nexpected,path=sys.stdin.read().strip().split(None,1)\nsys.exit(0 if hashlib.sha512(pathlib.Path(path.strip()).read_bytes()).hexdigest()==expected else 1)\n')
        for tool in tools.iterdir():
            tool.chmod(0o755)
        self.env = {**os.environ, 'PATH': str(tools) + os.pathsep + os.environ['PATH']}

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.src), *args], text=True)

    def run_preflight(self):
        return subprocess.run(['bash', str(self.script), str(self.src), str(self.root / 'out'),
                               str(self.config)], env=self.env, capture_output=True, text=True)

    def test_clean_source_stages_i14_and_defaults_to_its_dtb(self):
        result = self.run_preflight()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('DTB=mediatek/mt6735-i14-bringup.dtb', result.stdout)
        self.assertEqual((self.src / self.board).read_bytes(), self.board_source.read_bytes())
        self.assertEqual(self.run_preflight().returncode, 0)

    def test_modified_tracked_source_is_rejected_before_build(self):
        (self.src / 'Makefile').write_text('different source at same HEAD\n')
        self.assertNotEqual(self.run_preflight().returncode, 0)
        self.assertFalse((self.root / 'out').exists())

    def test_unexpected_or_mismatching_untracked_input_is_rejected(self):
        other = self.src / 'unexpected.h'
        other.write_text('unexpected build input\n')
        self.assertNotEqual(self.run_preflight().returncode, 0)
        other.unlink()
        (self.src / self.board).write_text('different board\n')
        self.assertNotEqual(self.run_preflight().returncode, 0)


if __name__ == '__main__':
    unittest.main()
