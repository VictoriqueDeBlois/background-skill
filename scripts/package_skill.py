#!/usr/bin/env python3
"""Build a reproducible skill ZIP from maintained files, with its MIT license."""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import re
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / 'background-job-continuation'
FILES = ('SKILL.md', 'agents/openai.yaml', 'references/protocol.md', 'scripts/bgjob.py')


def version():
    tree = ast.parse((SKILL / 'scripts/bgjob.py').read_text(encoding='utf-8'))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'VERSION' for t in node.targets):
            value = ast.literal_eval(node.value)
            if isinstance(value, str) and re.fullmatch(r'\d+\.\d+\.\d+', value):
                return value
    raise ValueError('Helper does not define a semantic VERSION')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'dist')
    args = parser.parse_args()
    entries = [(SKILL / name, 'background-job-continuation/' + name) for name in FILES]
    entries.append((ROOT / 'LICENSE', 'background-job-continuation/LICENSE'))
    for source, _ in entries:
        if not source.is_file() or source.is_symlink():
            parser.error('Missing maintained regular file: ' + str(source))
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    target = output / f'background-job-continuation-{version()}.zip'
    temporary = target.with_suffix('.zip.tmp')
    try:
        with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for source, name in sorted(entries, key=lambda pair: pair[1]):
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (0o100755 if source.name == 'bgjob.py' else 0o100644) << 16
                archive.writestr(info, source.read_bytes(), compresslevel=9)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    print(json.dumps({'archive': str(target), 'version': version(), 'files': len(entries),
                      'sha256': hashlib.sha256(target.read_bytes()).hexdigest()}, indent=2))


if __name__ == '__main__':
    main()
