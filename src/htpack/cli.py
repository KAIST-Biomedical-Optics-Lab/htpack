"""Minimal commands; machine-readable stdout and progress/errors on stderr."""
import argparse
from decimal import Decimal, DecimalException
import json
import math
from pathlib import Path
import sys
from . import __version__
from . import archive
from .platform_support import configure_console


def _ri_command(args):
    import numpy as np
    from . import ri
    if args.command=="ri-encode":
        original=np.load(args.input,allow_pickle=False)
        reference=np.load(args.reference,allow_pickle=False) if args.reference else None
        blob,header=ri.encode(original,args.max_ri_error,"fast" if args.fast else "fourier",reference=reference)
        with archive.atomic_output(args.output) as temporary:temporary.write_bytes(blob)
        return dict(version=__version__,contract="v17-native-RI-to-float32",packet_bytes=len(blob),
                    raw_f32_bytes=original.size*4,cr=original.size*4/len(blob),**header["quality"])
    blob=args.input.read_bytes()
    if args.command=="ri-decode":
        restored=ri.decode(blob)
        with archive.atomic_output(args.output) as temporary:
            with temporary.open("wb") as stream:np.save(stream,restored,allow_pickle=False)
        return dict(shape=list(restored.shape),dtype=restored.dtype.str,output=str(args.output))
    reference=np.load(args.reference,allow_pickle=False) if args.reference else None
    return ri.verify(blob,reference)


def _ri_error(value,scale):
    try:
        epsilon=Decimal(value)*scale
        if not epsilon.is_finite() or not 0<epsilon<=Decimal("0.1"):
            raise ValueError
        result=float(epsilon)
        if result<=0:raise ValueError
        return result
    except (DecimalException,ValueError,OverflowError):
        raise argparse.ArgumentTypeError("RI error must be finite and in (0, 0.1]; --ri-error-scale must be in (0, 1000]") from None


def _scaled_ri_error(value):
    return _ri_error(value,Decimal("0.0001"))


def _absolute_ri_error(value):
    return _ri_error(value,Decimal(1))


def _concurrency(value):
    if value=="auto":return value
    try:
        result=int(value)
        if result<1:raise ValueError
        return result
    except ValueError:raise argparse.ArgumentTypeError("Expected auto or a positive integer") from None


def _memory_gib(value):
    try:
        result=float(value)
        if not math.isfinite(result) or result<=0:raise ValueError
        return result
    except ValueError:raise argparse.ArgumentTypeError("Expected a finite positive memory budget in GiB") from None


def main(argv=None):
    configure_console()
    p=argparse.ArgumentParser(prog="htpack",description="TCF 1.3.6/1.4.1/1.6.3 3D/time-lapse RI compression: Fourier by default; --fast uses BitRound + Pcodec")
    p.add_argument("--version",action="version",version=__version__)
    sub=p.add_subparsers(dest="command",required=True)
    for name in ("ri-encode","ri-decode","ri-verify"):
        q=sub.add_parser(name,help="v17 float RI/NPY contract; no uint16 grid conversion")
        q.add_argument("input",type=Path)
        if name in ("ri-encode","ri-decode"):q.add_argument("-o","--output",required=True,type=Path)
        if name in ("ri-encode","ri-verify"):q.add_argument("--reference",type=Path)
        if name=="ri-encode":
            q.add_argument("--max-ri-error",type=_absolute_ri_error,default=1e-4)
            q.add_argument("--fast",action="store_true")
    for name in ("inspect","encode","decode","verify"):
        q=sub.add_parser(name)
        q.add_argument("input",type=Path)
        q.add_argument("--json",action="store_true",help="JSON is the default output format")
        if name in ("encode","decode"):q.add_argument("-o","--output",required=True,type=Path)
        if name in ("encode","decode"):
            q.add_argument("--concurrent-volumes",type=_concurrency,default="auto",metavar="auto|N",
                help="Concurrent volume limit W; auto selects from RAM, CPU and timepoint count (default)")
            q.add_argument("--memory-gib",type=_memory_gib,metavar="GiB",
                help="Optional total-process RAM planning cap; not an OS-enforced memory limit")
        if name=="encode":
            budget=q.add_mutually_exclusive_group()
            budget.add_argument("--ri-error-scale",dest="max_ri_error",type=_scaled_ri_error,metavar="FACTOR",
                help="Maximum RI error in units of 1e-4: 1 (default) = 1e-4; 5 = 5e-4; 0.1 = 1e-5")
            budget.add_argument("--max-ri-error",dest="max_ri_error",type=_absolute_ri_error,metavar="RI",
                help="Absolute maximum RI error (legacy alternative to --ri-error-scale)")
            q.set_defaults(max_ri_error=1e-4)
            q.add_argument("--fast",action="store_true",help="Use BitRound + Pcodec without FFT; retain the same final RI bound and guard")
        if name=="verify":
            q.add_argument("--reference",type=Path)
            q.add_argument("--restored",type=Path,help="Also verify the final written TCF")
    args=p.parse_args(argv)
    try:
        if args.command.startswith("ri-"):result=_ri_command(args)
        elif args.command=="inspect":result=archive.inspect(args.input)
        elif args.command=="encode":result=archive.encode(args.input,args.output,args.max_ri_error,mode="fast" if args.fast else "fourier",
            concurrent_volumes=args.concurrent_volumes,memory_gib=args.memory_gib)
        elif args.command=="decode":result=archive.decode(args.input,args.output,concurrent_volumes=args.concurrent_volumes,memory_gib=args.memory_gib)
        else:result=archive.verify(args.input,args.reference,args.restored)
        print(json.dumps(result,indent=2,ensure_ascii=False,allow_nan=False))
        return 0
    except (ValueError,OSError,KeyError,TypeError,RuntimeError,OverflowError) as exc:
        print(json.dumps(dict(status="failed",error=str(exc)),ensure_ascii=False),file=sys.stderr)
        return 1


if __name__=="__main__":
    raise SystemExit(main())
