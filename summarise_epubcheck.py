#!/usr/bin/env python3
"""
Aggregate the per-file epubcheck JSON reports in epubcheck-out/.

Prints a per-severity tally, then groups every non-INFO message by its
epubcheck ID so recurring problems collapse into one line with a count.
Exits non-zero if any file reported a FATAL or ERROR.
"""

import argparse
import collections
import glob
import json
import os
import sys

from epub_paths import REPORTS as OUT
SKIP = {'INFO', 'USAGE'}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--reports', metavar='DIR', default=OUT,
                    help='where the epubcheck JSON reports live '
                         '(default: %(default)s)')
    args = ap.parse_args()

    files = sorted(glob.glob(os.path.join(args.reports, '*.json')))
    if not files:
        print('no epubcheck reports found')
        return 0

    severity = collections.Counter()
    by_id = collections.defaultdict(list)
    bad_files = set()
    clean = 0

    for path in files:
        name = os.path.basename(path)[:-5]
        try:
            with open(path, encoding='utf-8') as fh:
                report = json.load(fh)
        except (json.JSONDecodeError, OSError) as exc:
            print(f'could not read report for {name}: {exc}')
            bad_files.add(name)
            continue

        msgs = report.get('messages') or []
        counted = False
        for m in msgs:
            sev = m.get('severity', 'ERROR')
            severity[sev] += 1
            if sev in SKIP:
                continue
            counted = True
            mid = m.get('ID') or '?'
            loc = (m.get('locations') or [{}])[0].get('path') or ''
            by_id[(sev, mid)].append((name, m.get('message', ''), loc))
            if sev in ('FATAL', 'ERROR'):
                bad_files.add(name)
        if not counted:
            clean += 1

    print(f'{len(files)} reports')
    for sev in ('FATAL', 'ERROR', 'WARNING', 'INFO', 'USAGE'):
        if severity[sev]:
            print(f'  {sev:<8}: {severity[sev]}')
    print(f'  clean files (no message at all): {clean}')
    print(f'  files with errors: {len(bad_files)}')

    if by_id:
        print('\nmessages grouped by epubcheck id:')
        for (sev, mid), hits in sorted(
                by_id.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            print(f'\n  [{sev}] {mid}  x{len(hits)}')
            shown = collections.Counter()
            for name, msg, loc in hits:
                shown[(msg, loc)] += 1
            for (msg, loc), n in shown.most_common(4):
                tail = f'   ({loc})' if loc else ''
                print(f'      x{n}  {msg[:110]}{tail}')
            if len(shown) > 4:
                print(f'      ... and {len(shown) - 4} more distinct variants')

    for name in sorted(bad_files):
        print(f'\nFAIL {name}')

    return 1 if bad_files else 0


if __name__ == '__main__':
    sys.exit(main())
