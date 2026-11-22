"""The workflows that run gh_hdc's playbooks all run one pinned Ansible.

Renovate bumps the ANSIBLE_VERSION pins (renovate.json, "Ansible version pin
in the workflows that run playbooks"). A workflow that gets Ansible any other
way runs whatever that gives it: `pipx run --spec ansible` took the newest
release on every run, out of Renovate's sight, and one workflow still
installed ansible-core 2.13.0 next to it.

Run with a Python that has PyYAML (ansible-workflows.yml uses the pinned
Ansible's):
    python3 -m unittest discover -s tests -v
"""

import json
import re
import unittest
from pathlib import Path

import yaml

ROOT =Path(__file__).resolve().parent.parent
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.y*ml"))
PINNED = '"ansible==${ANSIBLE_VERSION}"'
# A command line that installs or runs Ansible from PyPI.
INSTALL = re.compile(
    r"\b(?:pipx\s+(?:install|run)|pip3?\s+install|uvx|uv\s+(?:tool\s+)?(?:install|run))\b.*\bansible(?:-core)?\b"
)
# An Ansible command line tool being run.
COMMAND = re.compile(r"\bansible-(?:playbook|inventory|galaxy|config|vault|pull)\b")


def renovate_manager():
    """The regex manager that bumps ANSIBLE_VERSION, as Python regexes."""
    config = json.loads((ROOT / "renovate.json").read_text())
    for manager in config.get("customManagers", []):
        if manager.get("customType") == "regex" and manager.get("depNameTemplate") == "ansible":
            files = [re.compile(p.strip("/")) for p in manager["managerFilePatterns"]]
            # Renovate's (?<name>) groups are Python's (?P<name>).
            strings = [re.compile(s.replace("(?<", "(?P<")) for s in manager["matchStrings"]]
            return files, strings
    return None


def pins():
    """{workflow: [ANSIBLE_VERSION values]}, read the way Renovate reads them."""
    found = {}
    manager = renovate_manager()
    if manager is None:
        return found
    _, strings = manager
    for path in WORKFLOWS:
        text = path.read_text()
        values = [m.group("currentValue") for s in strings for m in s.finditer(text)]
        if values:
            found[path.name] = values
    return found


def steps():
    """(workflow, job, index, step, env) for every step, env merged from workflow, job and step."""
    for path in WORKFLOWS:
        workflow = yaml.safe_load(path.read_text())
        for job_name, job in (workflow.get("jobs") or {}).items():
            for index, step in enumerate(job.get("steps") or []):
                env = {**(workflow.get("env") or {}), **(job.get("env") or {}), **(step.get("env") or {})}
                yield path.name, job_name, index, step, env


class AnsibleWorkflows(unittest.TestCase):
    def test_renovate_tracks_the_pins(self):
        manager = renovate_manager()
        self.assertIsNotNone(manager, "renovate.json has no regex manager for ANSIBLE_VERSION (depName ansible)")
        files, strings = manager
        tracked = pins()
        self.assertTrue(tracked, "no workflow pins ANSIBLE_VERSION")
        for path in WORKFLOWS:
            text = path.read_text()
            mentions = len(re.findall(r"^\s*ANSIBLE_VERSION:", text, re.M))
            if not mentions:
                continue
            rel = path.relative_to(ROOT).as_posix()
            with self.subTest(workflow=path.name):
                self.assertTrue(any(f.search(rel) for f in files), "%s isn't in the manager's managerFilePatterns" % rel)
                self.assertEqual(len(tracked.get(path.name, [])), mentions,
                                 "Renovate's matchStrings don't read every ANSIBLE_VERSION in %s" % path.name)

    def test_one_version(self):
        versions = {name: sorted(set(values)) for name, values in pins().items()}
        self.assertEqual(len({v for values in versions.values() for v in values}), 1, versions)

    def test_installs_are_pinned(self):
        for workflow, job, index, step, env in steps():
            run = step.get("run") or ""
            for line in run.splitlines():
                if not INSTALL.search(line):
                    continue
                with self.subTest(workflow=workflow, job=job, step=step.get("name", index)):
                    self.assertIn(PINNED, line, "install or run Ansible as %s" % PINNED)
                    self.assertIn("ANSIBLE_VERSION", env, "the step's env needs ANSIBLE_VERSION")

    def test_ansible_commands_use_the_pinned_ansible(self):
        # Without its own install a job runs whatever Ansible the runner image
        # has, if any.
        installed = set()
        for workflow, job, index, step, _ in steps():
            run = step.get("run") or ""
            if PINNED in run:
                installed.add((workflow, job))
            if COMMAND.search(run):
                with self.subTest(workflow=workflow, job=job, step=step.get("name", index)):
                    self.assertIn((workflow, job), installed,
                                  "runs Ansible without installing %s first (or with pipx run in the same step)" % PINNED)


if __name__ == "__main__":
    unittest.main()
