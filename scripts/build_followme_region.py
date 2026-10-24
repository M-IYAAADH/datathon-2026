#!/usr/bin/env python3
"""Build the required complete-segment regional subset from validated national outputs."""
import argparse
from pathlib import Path
import sys

from followme_pipeline.region import build_region


def main():
    root=Path(__file__).resolve().parents[1]
    parser=argparse.ArgumentParser()
    parser.add_argument('--config',default=str(root/'config/followme_region.json'))
    parser.add_argument('--national-output')
    parser.add_argument('--verify-repeat',action='store_true')
    args=parser.parse_args()
    try:
        build_region(args.config,root,args.national_output,args.verify_repeat)
    except Exception as error:
        print(f'Regional cleaning failed ({type(error).__name__}); no source record contents logged.',file=sys.stderr)
        return 1
    return 0


if __name__=='__main__':
    raise SystemExit(main())
