#!/usr/bin/env python3
"""Required end-to-end workflow: national cleaning, then complete-segment regional selection."""
import argparse
from pathlib import Path
import sys

from followme_pipeline.run import run
from followme_pipeline.region import build_region


def main():
    root=Path(__file__).resolve().parents[1]
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',default=str(root/'config/followme.json'))
    parser.add_argument('--region-config',default=str(root/'config/followme_region.json'))
    parser.add_argument('--input')
    parser.add_argument('--output')
    parser.add_argument('--init-secret',action='store_true')
    parser.add_argument('--sample-rows',type=int)
    parser.add_argument('--reuse-staging',action='store_true')
    parser.add_argument('--verify-repeat',action='store_true')
    args=parser.parse_args()
    if args.sample_rows is not None and args.sample_rows<=0:
        parser.error('--sample-rows must be positive')
    try:
        manifest=run(args.config,root,output_override=args.output,input_override=args.input,
            initialise=args.init_secret,sample_rows=args.sample_rows,reuse_staging=args.reuse_staging,
            verify_repeat=args.verify_repeat)
        output=root/(args.output or manifest['configuration']['output'])
        build_region(args.region_config,root,output,args.verify_repeat)
    except Exception as error:
        print(f'FollowMe pipeline failed ({type(error).__name__}); no source record contents logged.',file=sys.stderr)
        return 1
    return 0


if __name__=='__main__':
    raise SystemExit(main())
