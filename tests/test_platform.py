"""OS adapter contract tests; Windows branches are simulated on a Mac runner."""
import builtins
from pathlib import Path
from types import SimpleNamespace
import os
import sys
import pytest
from htpack import archive,parallel,platform_support as support
from test_htpack import make_tcf


def test_windows_peak_does_not_import_resource(monkeypatch):
    monkeypatch.setattr(support,"WINDOWS",True)
    monkeypatch.setattr(support.psutil,"Process",lambda:SimpleNamespace(memory_info=lambda:SimpleNamespace(peak_wset=123456789,rss=1)))
    importer=builtins.__import__
    def restricted(name,*args,**kwargs):
        if name=="resource":raise AssertionError("Unix resource module reached from Windows branch")
        return importer(name,*args,**kwargs)
    monkeypatch.setattr(builtins,"__import__",restricted)
    assert support.peak_rss()==123456789


def test_native_peak_is_available():
    assert support.peak_rss()>0


@pytest.mark.parametrize("raced",[False,True])
def test_windows_publication_uses_non_overwriting_rename(tmp_path,monkeypatch,raced):
    monkeypatch.setattr(support,"WINDOWS",True)
    real_rename=os.rename;calls=[]
    def windows_rename(source,target):
        calls.append((source,target))
        if target.exists():raise FileExistsError(str(target))
        real_rename(source,target)
    monkeypatch.setattr(support.os,"rename",windows_rename)
    target=tmp_path/"result.TCF"
    def write():
        with archive.atomic_output(target) as temporary:
            temporary.write_bytes(b"completed output")
            if raced:target.write_bytes(b"other writer owns this")
    if raced:
        with pytest.raises(FileExistsError):write()
        assert target.read_bytes()==b"other writer owns this"
    else:
        write();assert target.read_bytes()==b"completed output"
    assert len(calls)==1 and not list(tmp_path.glob("*.partial"))


def test_current_process_affinity_caps_CPUs(monkeypatch):
    monkeypatch.setattr(parallel.psutil,"virtual_memory",lambda:SimpleNamespace(total=16*2**30,available=8*2**30))
    monkeypatch.setattr(parallel.psutil,"cpu_count",lambda logical=True:16)
    monkeypatch.setattr(parallel.psutil,"Process",lambda:SimpleNamespace(memory_info=lambda:SimpleNamespace(rss=100*2**20),cpu_affinity=lambda:[2,3]))
    if hasattr(parallel.os,"sched_getaffinity"):monkeypatch.delattr(parallel.os,"sched_getaffinity")
    assert parallel.resources()["logical_cpu_count"]==2


def test_unavailable_affinity_falls_back(monkeypatch):
    def unavailable():raise parallel.psutil.AccessDenied()
    monkeypatch.setattr(parallel.psutil,"virtual_memory",lambda:SimpleNamespace(total=16*2**30,available=8*2**30))
    monkeypatch.setattr(parallel.psutil,"cpu_count",lambda logical=True:8)
    monkeypatch.setattr(parallel.psutil,"Process",lambda:SimpleNamespace(memory_info=lambda:SimpleNamespace(rss=1),cpu_affinity=unavailable))
    if hasattr(parallel.os,"sched_getaffinity"):monkeypatch.delattr(parallel.os,"sched_getaffinity")
    assert parallel.resources()["logical_cpu_count"]==8


def test_windows_console_is_UTF8(monkeypatch):
    calls=[]
    stream=SimpleNamespace(reconfigure=lambda **kwargs:calls.append(kwargs))
    monkeypatch.setattr(support,"WINDOWS",True)
    monkeypatch.setattr(sys,"stdout",stream);monkeypatch.setattr(sys,"stderr",stream)
    support.configure_console()
    assert calls==[dict(encoding="utf-8",errors="backslashreplace")]*2


@pytest.mark.parametrize("mode",["fourier","fast"])
def test_roundtrip_in_unicode_paths(tmp_path,mode):
    folder=tmp_path/"한글 space μ";folder.mkdir()
    source=folder/"원본 3D.TCF";packed=folder/"압축.htfc";restored=folder/"복원.TCF"
    make_tcf(source)
    archive.encode(source,packed,mode=mode)
    archive.decode(packed,restored)
    report=archive.verify(packed,reference=source,restored=restored)
    assert report["violations"]==0 and report["metadata_preserved"]
    assert Path(restored).exists()

