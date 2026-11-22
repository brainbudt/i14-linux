"""Prepare a reviewed I14 SECCFG candidate using RAM/MMIO only; no storage write."""
import argparse
import sys
from i14_seccfg_io import prepare

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory",help="New local private output directory")
    parser.add_argument("--serialport",default="/dev/cu.usbmodem21101")
    args=parser.parse_args(argv)
    result=prepare(args.directory,args.serialport)
    print("Prepared without storage writes. Candidate SHA256:",result["candidate_sha256"])

if __name__=="__main__":
    try: main()
    except Exception as exc:
        print(f"Preparation stopped ({type(exc).__name__}); no storage write performed.",file=sys.stderr)
        sys.exit(1)
