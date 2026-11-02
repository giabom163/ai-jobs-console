"""Preview or import a bounded source-backed discovery run; no RSS/network calls."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from tools.deepseek_discovery import save_candidates


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',type=Path,required=True,help='Existing evidence run containing accepted[]')
    parser.add_argument('--db',type=Path,default=ROOT/'ai_jobs.db')
    parser.add_argument('--limit',type=int,default=3)
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--dry-run',action='store_true')
    args=parser.parse_args()
    if not 1<=args.limit<=15:parser.error('limit must be 1..15')
    if args.apply and args.dry_run:parser.error('choose preview or apply')
    raw=args.input.read_bytes();items=json.loads(raw)['accepted']
    if not isinstance(items,list) or len(items)>args.limit:parser.error('input exceeds bounded candidate limit; split explicitly')
    report=dict(input_sha256=hashlib.sha256(raw).hexdigest(),records=len(items),mode='preview',requests=0,approved=0)
    if args.apply:
        report.update(mode='applied',batch=save_candidates(args.db,items))
    print(json.dumps(report,ensure_ascii=True))


if __name__=='__main__':main()
