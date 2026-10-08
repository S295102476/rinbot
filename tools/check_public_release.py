"""Scan publishable files/history without printing sensitive values.

This targeted project check supplements GitHub secret scanning; it does not
claim to identify every possible credential format.
"""
from __future__ import annotations

import argparse
import ast
import ipaddress
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEXT = {'.py', '.md', '.yaml', '.yml', '.json', '.toml', '.ini', '.cfg', '.env', '.txt', '.sh', '.ts', '.tsx'}
KEY = re.compile(r'^(?:.*_)?(?:api_key|password|passwd|secret_key|access_key|token|secret)$', re.I)
PLACEHOLDER = re.compile(r'^(?:|test.*|fake.*|demo.*|example.*|change.*|replace.*|your.*|placeholder.*|unused|dummy.*|ci-only-.*|rinbot-ci-only-.*|xxx|\*+|<.*>|\$\{.*\})$', re.I)
PRIVATE_PATH = re.compile(r'(?:^|/)(?:sftp\.json|config\.ya?ml|\.env(?:\.(?!example$).*)?|[^/]+\.db(?:-wal|-shm)?|[^/]+\.pem|[^/]+\.key|special_users\.md)$')
SECRET_PATTERNS = [
    ('private key', re.compile(r'-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----')),
    ('provider token', re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|sk-[A-Za-z0-9_-]{24,})')),
    ('credential in URL', re.compile(r'https?://[^\s/:]+:[^\s/@]+@')),
]

def literal_secret(value: ast.AST) -> bool:
    return isinstance(value, ast.Constant) and isinstance(value.value, str) and not PLACEHOLDER.fullmatch(value.value) and len(value.value) >= 4

def inspect_text(name: str, data: bytes) -> list[str]:
    findings = []
    name = name.replace('\\', '/')
    if PRIVATE_PATH.search(name): findings.append('private file path')
    if len(data) > 50 * 1024 * 1024: findings.append('file exceeds public source size limit')
    try: text = data.decode('utf-8-sig')
    except UnicodeDecodeError: return findings
    for label, pattern in SECRET_PATTERNS:
        if label == 'credential in URL' and '/tests/' in '/' + name:
            continue  # tests deliberately verify rejection of credential-bearing URLs
        if pattern.search(text): findings.append(label)
    for match in re.finditer(r'https?://((?:\d{1,3}\.){3}\d{1,3})(?::\d+)?', text):
        try: address = ipaddress.ip_address(match.group(1))
        except ValueError: continue
        if address.is_global: findings.append('hardcoded public IP URL')
    if name.endswith('.py') and '/tests/' not in '/' + name and not name.startswith('tests/'):
        try: tree = ast.parse(text)
        except SyntaxError: return findings
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and literal_secret(node.value):
                if any(isinstance(target, ast.Name) and KEY.fullmatch(target.id) for target in node.targets):
                    findings.append(f'line {node.lineno}: literal credential assignment')
            elif isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg and KEY.fullmatch(kw.arg) and literal_secret(kw.value):
                        findings.append(f'line {node.lineno}: literal credential argument')
                if isinstance(node.func, ast.Attribute) and node.func.attr == 'get' and len(node.args) >= 2:
                    key, value = node.args[:2]
                    if isinstance(key, ast.Constant) and isinstance(key.value, str) and KEY.fullmatch(key.value) and literal_secret(value):
                        findings.append(f'line {node.lineno}: literal credential default')
            elif isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if isinstance(key, ast.Constant) and isinstance(key.value, str) and KEY.fullmatch(key.value) and literal_secret(value):
                        findings.append(f'line {node.lineno}: literal credential mapping')
    return sorted(set(findings))

def git(*args: str) -> bytes:
    return subprocess.check_output(['git', '-C', str(ROOT), *args])

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--history', action='store_true', help='also inspect every historical blob reachable from the checked out revision')
    args = parser.parse_args()
    findings, count = [], 0
    names = git('ls-files', '-z', '--cached', '--others', '--exclude-standard').decode('utf-8').split('\0')
    for name in sorted(set(names) - {''}):
        path = ROOT / name
        if not path.is_file(): continue
        count += 1
        for reason in inspect_text(name, path.read_bytes()): findings.append(f'{name}: {reason}')
    if args.history:
        objects = {}
        for row in git('rev-list', '--objects', 'HEAD').decode('utf-8').splitlines():
            oid, _, name = row.partition(' ')
            if name: objects[oid] = name
        proc = subprocess.Popen(['git', '-C', str(ROOT), 'cat-file', '--batch'], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        assert proc.stdin and proc.stdout
        try:
            for oid, name in objects.items():
                proc.stdin.write((oid + '\n').encode()); proc.stdin.flush()
                header = proc.stdout.readline().decode().strip().split()
                if len(header) != 3: raise RuntimeError('Cannot inspect Git object')
                payload = proc.stdout.read(int(header[2])); proc.stdout.read(1)
                if header[1] != 'blob': continue
                count += 1
                for reason in inspect_text(name, payload): findings.append(f'history {oid[:12]} {name}: {reason}')
        finally:
            proc.stdin.close(); proc.wait()
    for finding in sorted(set(findings)): print(finding)
    print(f'Scanned {count} file versions; {len(set(findings))} findings.')
    return 1 if findings else 0

if __name__ == '__main__':
    raise SystemExit(main())
