"""Verify the returned native-Windows archives on Mac; rejects rehearsal as Windows evidence."""
import argparse
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import zipfile


def sha(path):
    h=hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda:f.read(8*2**20),b""):h.update(chunk)
    return h.hexdigest()


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--return-zip",type=Path,required=True)
    parser.add_argument("--data",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--allow-rehearsal",action="store_true")
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=False)
    fixtures=json.loads((args.data/"data-manifest.json").read_text(encoding="utf-8"))
    results=[]
    with tempfile.TemporaryDirectory(prefix="htpack-return-") as scratch:
        unpack=Path(scratch)
        with zipfile.ZipFile(args.return_zip) as z:
            for entry in z.infolist():
                if not (unpack/entry.filename).resolve().is_relative_to(unpack.resolve()):raise ValueError("Unsafe return path")
            z.extractall(unpack)
        report=json.loads((unpack/"report.json").read_text(encoding="utf-8"))
        if not report["native_windows_execution"] and not args.allow_rehearsal:raise ValueError("Return is a non-Windows rehearsal")
        if report["version"]!=fixtures["version"]:raise ValueError("Version mismatch")
        if report["data_manifest_sha256"]!=sha(args.data/"data-manifest.json"):raise ValueError("Fixture manifest differs")
        expected={(case["id"],mode) for case in fixtures["cases"] for mode in ("fourier","fast")}
        actual=[(item["case"],item["mode"]) for item in report["produced_archives"]]
        if not expected or set(actual)!=expected or len(actual)!=len(expected):raise ValueError("Incomplete/duplicate returned archive matrix")
        cli=[sys.executable,"-B","-I","-X","utf8","-m","htpack"]
        for item in report["produced_archives"]:
            record=dict(case=item["case"],mode=item["mode"],status="running")
            results.append(record)
            try:
                case=next(v for v in fixtures["cases"] if v["id"]==item["case"])
                source=args.data/case["source"];packed=unpack/item["path"]
                if not packed.resolve().is_relative_to(unpack.resolve()):raise ValueError("Unsafe archive path")
                if item["source_sha256"]!=case["source_sha256"]:raise ValueError("Returned source differs")
                if report["native_windows_execution"] and item["producer_system"]!="Windows":raise ValueError("Archive not produced on Windows")
                if sha(source)!=case["source_sha256"] or sha(packed)!=item["sha256"]:raise ValueError("Source/archive checksum mismatch")
                restored=args.output/(item["case"]+"-"+item["mode"]+".TCF")
                for name,command in (
                    ("decode",cli+["decode",str(packed),"-o",str(restored)]),
                    ("verify",cli+["verify",str(packed),"--reference",str(source),"--restored",str(restored)])):
                    value=subprocess.run(command,capture_output=True,encoding="utf-8",errors="replace")
                    (args.output/(item["case"]+"-"+item["mode"]+"-"+name+".stderr.txt")).write_text(value.stderr,encoding="utf-8")
                    if value.returncode:raise ValueError("Returned "+name+" failed: "+value.stderr[-600:])
                    record[name]=json.loads(value.stdout)
                if record["verify"]["violations"]!=0:raise ValueError("RI bound failed")
                audit=args.output/(item["case"]+"-"+item["mode"]+"-independent.json")
                checked=subprocess.run([sys.executable,"-B","-I","-X","utf8",str(Path(__file__).with_name("audit_tcf.py")),
                    str(source),str(restored),str(audit)],capture_output=True,encoding="utf-8",errors="replace")
                if checked.returncode:raise ValueError("Independent audit failed: "+checked.stderr[-600:])
                record["independent_audit"]=json.loads(audit.read_text(encoding="utf-8"))
                if record["independent_audit"]["status"]!="passed":raise ValueError("Independent audit did not pass")
                record["status"]="passed"
            except Exception as error:record["status"]="failed";record["error"]=str(error)
    genuine=report["native_windows_execution"] and platform.system()=="Darwin"
    passed=bool(results) and all(v["status"]=="passed" for v in results) and report["status"] in ("passed","passed_rehearsal_only")
    status="passed" if genuine and passed else "rehearsal_only" if not genuine and passed else "failed"
    result=dict(status=status,actual_Windows_to_Mac=genuine,producer_host=report["host"],consumer_system=platform.system(),
        results=results,native_windows_report_status=report["status"])
    (args.output/"cross-report.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(dict(status=status,report=str(args.output/"cross-report.json")),ensure_ascii=False))
    return 1 if status=="failed" else 0


if __name__=="__main__":sys.exit(main())

