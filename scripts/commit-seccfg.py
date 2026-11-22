"""Commit only the reviewed 8192-byte I14 SECCFG prefix; no erase/reset/unlock command."""
import argparse
import re
import sys
from i14_seccfg_io import commit

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory",help="Frozen prepared directory reviewed by the parent")
    parser.add_argument("--sha256",required=True,help="Explicit full 524288-byte candidate SHA256")
    parser.add_argument("--serialport",default="/dev/cu.usbmodem21101")
    args=parser.parse_args(argv)
    if not re.fullmatch("[0-9a-f]{64}",args.sha256): parser.error("--sha256 must be 64 lowercase hex digits")
    result=commit(args.directory,args.sha256,args.serialport)
    print("Full SECCFG readback matches candidate SHA256:",result["readback_sha256"])

if __name__=="__main__":
    try: main()
    except Exception as exc:
        print(f"Commit stopped ({type(exc).__name__}). Check attempt/failure records; never retry automatically.",file=sys.stderr)
        sys.exit(1)
