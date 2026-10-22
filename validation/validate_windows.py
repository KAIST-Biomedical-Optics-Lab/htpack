"""Native Windows lab test; produces logs and a return ZIP without decoded TCFs."""
import argparse
from datetime import datetime,timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import traceback
import uuid
import zipfile
import h5py
import psutil


def digest(path):
    h=hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda:f.read(8*2**20),b""):h.update(chunk)
    return h.hexdigest()


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--data",type=Path,required=True,help="Folder containing data-manifest.json")
    parser.add_argument("--output",type=Path,default=Path.home()/"HTPack-validation")
    parser.add_argument("--memory-gib",type=float)
    parser.add_argument("--allow-non-windows",action="store_true",help="Development rehearsal only; never Windows validation")
    args=parser.parse_args()
    native_windows=platform.system()=="Windows"
    if not native_windows and not args.allow_non_windows:
        parser.error("Run this on Windows; --allow-non-windows is a clearly labelled development rehearsal")
    data=args.data.resolve();fixtures=json.loads((data/"data-manifest.json").read_text(encoding="utf-8"))
    if fixtures["version"]!=importlib.metadata.version("htpack"):raise ValueError("Fixture/package version mismatch")
    run=args.output.resolve()/(datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")+"-"+uuid.uuid4().hex[:8])
    run.mkdir(parents=True);(run/"logs").mkdir();(run/"native_archives").mkdir();(run/"restored").mkdir()
    report=dict(format="htpack-windows-validation-v1",version=fixtures["version"],status="running",
        native_windows_execution=native_windows,execution_kind="native Windows" if native_windows else "NON-WINDOWS REHEARSAL",
        as_of_utc=datetime.now(timezone.utc).isoformat(),
        host=dict(system=platform.system(),release=platform.release(),machine=platform.machine(),node=platform.node(),
            Python=sys.version,total_RAM_bytes=psutil.virtual_memory().total,logical_CPUs=psutil.cpu_count(),
            hdf5_version=h5py.version.hdf5_version),
        data_manifest_sha256=digest(data/"data-manifest.json"),epsilon=1e-4,results=[],produced_archives=[],
        Windows_to_Mac_validation="PENDING_RETURN_TO_MAC",TomoAnalysis="NOT_CHECKED_AUTOMATICALLY")
    cli=[sys.executable,"-B","-I","-X","utf8","-m","htpack"]
    flags=["--memory-gib",str(args.memory_gib)] if args.memory_gib is not None else []
    def save():
        (run/"report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    def invoke(label,command):
        print(label,flush=True)
        result=subprocess.run(command,capture_output=True,encoding="utf-8",errors="replace")
        (run/"logs"/(label+".stdout.txt")).write_text(result.stdout,encoding="utf-8")
        (run/"logs"/(label+".stderr.txt")).write_text(result.stderr,encoding="utf-8")
        if result.returncode:raise RuntimeError(label+" failed; see stderr log")
        return json.loads(result.stdout)
    def validate_quality(value):
        if value.get("violations")!=0 or value["max_RI_error"]>1e-4 or value["max_reader_float32_RI_error"]>1e-4:
            raise ValueError("Final RI budget check failed")
        if not value["metadata_preserved"] or not value["non_RI_data_preserved"]:raise ValueError("Metadata/non-RI check failed")
    save()
    try:
        for case in fixtures["cases"]:
            source=data/case["source"];valid_source=digest(source)==case["source_sha256"]
            for mode in ("fourier","fast"):
                label=case["id"]+"-"+mode
                for phase in ("mac_archive_decode","native_encode_decode"):
                    item=dict(case=case["id"],mode=mode,phase=phase,status="running")
                    report["results"].append(item);save()
                    try:
                        if not valid_source:raise ValueError("Native reference checksum mismatch")
                        original=case["mac_archives"][mode]
                        if phase=="mac_archive_decode":
                            packed=data/original["path"]
                            if digest(packed)!=original["sha256"]:raise ValueError("Mac archive checksum mismatch")
                        else:
                            packed=run/"native_archives"/(label+".htfc")
                            item["encode"]=invoke(label+"-encode",cli+["encode",str(source),"-o",str(packed)]+flags+(["--fast"] if mode=="fast" else []))
                            report["produced_archives"].append(dict(case=case["id"],mode=mode,path=packed.relative_to(run).as_posix(),
                                sha256=digest(packed),source_sha256=case["source_sha256"],producer_system=platform.system()))
                        recovered=run/"restored"/(label+"-"+phase+".TCF")
                        item["decode"]=invoke(label+"-"+phase+"-decode",cli+["decode",str(packed),"-o",str(recovered)]+flags)
                        quality=invoke(label+"-"+phase+"-verify",cli+["verify",str(packed),"--reference",str(source),"--restored",str(recovered)])
                        validate_quality(quality);item["verify"]=quality
                        audit=run/"logs"/(label+"-"+phase+"-independent.json")
                        invoke(label+"-"+phase+"-audit",[sys.executable,"-B","-I","-X","utf8",str(Path(__file__).with_name("audit_tcf.py")),
                            str(source),str(recovered),str(audit)])
                        item["independent_audit"]=json.loads(audit.read_text(encoding="utf-8"))
                        if item["independent_audit"]["status"]!="passed" or item["independent_audit"]["violations"]!=0:
                            raise ValueError("Independent RI/metadata check failed")
                        item["status"]="passed"
                    except Exception as error:
                        item["status"]="failed";item["error"]=str(error);item["traceback"]=traceback.format_exc()
                    save()
        failed=[v for v in report["results"] if v["status"]!="passed"]
        report["status"]="failed" if failed else "passed" if native_windows else "passed_rehearsal_only"
        report["Mac_to_Windows_validation"]="passed" if native_windows and not any(v["status"]!="passed" for v in report["results"] if v["phase"]=="mac_archive_decode") else "not_passed_or_not_Windows"
    finally:
        save()
        returned=run.with_suffix(".return.zip")
        with zipfile.ZipFile(returned,"w",compression=zipfile.ZIP_DEFLATED,compresslevel=1) as z:
            z.write(run/"report.json","report.json")
            for folder in ("logs","native_archives"):
                for path in (run/folder).rglob("*"):
                    if path.is_file():z.write(path,path.relative_to(run).as_posix())
        print(json.dumps(dict(report=str(run/"report.json"),return_zip=str(returned),status=report["status"],
            decoded_TCFs_included_in_return=False,native_windows_execution=native_windows),ensure_ascii=False),flush=True)
    return 0 if report["status"] in ("passed","passed_rehearsal_only") else 1


if __name__=="__main__":sys.exit(main())

