"""Time-series scheduling, every-timepoint preservation and failure-path tests."""
import hashlib
import json
from threading import Barrier, Lock
import h5py
import numpy as np
import pytest
from htpack import archive, codec, parallel, series, tcf
from htpack.cli import main
from test_htpack import make_tcf


def make_series(path,count=4,shape=(5,37,39)):
    make_tcf(path,shape)
    with h5py.File(path,"r+") as f:
        g=f["Data/3D"];base=g["000000"];x=base[()]
        g.attrs.modify("DataCount",np.array([count],dtype="<i8"))
        g.attrs.create("TimeInterval",np.array([7.5],dtype="<f8"))
        base.attrs.create("TimeStamp",np.array([123.125],dtype="<f8"))
        for i in range(1,count):
            ds=g.create_dataset(f"{i:06d}",data=(x+i*3).astype("<u2"))
            tcf.copy_attrs(base,ds)
            ds.attrs.modify("TimeStamp",np.array([123.125+7.5*i],dtype="<f8"))
            ds.attrs.modify("PositionX",np.array([1.23+.01*i],dtype="<f8"))


@pytest.fixture
def generous_ram(monkeypatch):
    state=dict(total_memory_bytes=64*2**30,available_memory_bytes=32*2**30,
        current_process_RSS_bytes=100*2**20,logical_cpu_count=8)
    monkeypatch.setattr(parallel,"resources",lambda:dict(state))
    return state


@pytest.mark.parametrize("mode",["fourier","fast"])
@pytest.mark.parametrize("scale,epsilon",[("1",1e-4),("0.1",1e-5)])
def test_series_cli_all_timepoints(tmp_path,capsys,generous_ram,mode,scale,epsilon):
    src=tmp_path/"source.TCF";packed=tmp_path/"series.htfc";out=tmp_path/"restored.TCF"
    make_series(src);before=hashlib.sha256(src.read_bytes()).hexdigest()
    assert main(["inspect",str(src)])==0
    info=json.loads(capsys.readouterr().out)
    assert info["mvp_encode_supported"] and info["scalar_RI"]["volume_count"]==4
    flags=["--fast"] if mode=="fast" else []
    assert main(["encode",str(src),"-o",str(packed),"--ri-error-scale",scale,"--concurrent-volumes","2"]+flags)==0
    encoded=json.loads(capsys.readouterr().out)
    assert encoded["time_lapse"] and encoded["volume_count"]==4 and encoded["epsilon"]==epsilon
    assert encoded["concurrency_plan"]["concurrent_volume_limit_W"]==2
    assert main(["inspect",str(packed)])==0
    assert json.loads(capsys.readouterr().out)["volume_count"]==4
    assert main(["decode",str(packed),"-o",str(out),"--concurrent-volumes","3"])==0
    decoded=json.loads(capsys.readouterr().out)
    assert decoded["concurrency_plan"]["concurrent_volume_limit_W"]==3
    assert main(["verify",str(packed),"--reference",str(src),"--restored",str(out)])==0
    report=json.loads(capsys.readouterr().out)
    assert report["violations"]==0 and len(report["volume_errors"])==4
    assert report["max_RI_error"]<=epsilon and report["max_reader_float32_RI_error"]<=epsilon
    with h5py.File(src,"r") as a,h5py.File(out,"r") as b:
        assert tcf.metadata(a["/"])==tcf.metadata(b["/"])
        assert tcf.other_hashes(a["/"])==tcf.other_hashes(b["/"])
        for path in tcf.validate_source(a)["ri_paths"]:
            assert codec.quality(a[path][()],b[path][()],epsilon)["violations"]==0
            if epsilon<1e-4:np.testing.assert_array_equal(a[path][()],b[path][()])
    assert hashlib.sha256(src.read_bytes()).hexdigest()==before


@pytest.mark.parametrize("mode",["fourier","fast"])
def test_packet_identity_across_W_and_actual_overlap(tmp_path,generous_ram,monkeypatch,mode):
    src=tmp_path/"source.tcf";make_series(src,3,(33,260,258))
    serial=tmp_path/"W1.htfc";concurrent=tmp_path/"W2.htfc"
    archive.encode(src,serial,mode=mode,concurrent_volumes=1)
    barrier=Barrier(2);guard=Lock();active=maximum=calls=0
    original=codec.encode_block
    def observed(*args,**kwargs):
        nonlocal active,maximum,calls
        with guard:
            calls+=1;number=calls;active+=1;maximum=max(maximum,active)
        try:
            if number<=2:barrier.wait(timeout=10)
            return original(*args,**kwargs)
        finally:
            with guard:active-=1
    monkeypatch.setattr(codec,"encode_block",observed)
    archive.encode(src,concurrent,mode=mode,concurrent_volumes=2)
    assert maximum==2
    with h5py.File(serial,"r") as a,h5py.File(concurrent,"r") as b:
        for i in range(3):
            for j in range(8):
                path=f"volumes/{i:06d}/blocks/{j:06d}"
                np.testing.assert_array_equal(a[path][()],b[path][()])


@pytest.mark.parametrize("mode",["fourier","fast"])
def test_series_source_free_and_damage(tmp_path,generous_ram,mode):
    src=tmp_path/"source.tcf";make_series(src,3)
    packed=tmp_path/"series.htfc";out=tmp_path/"restored.tcf"
    archive.encode(src,packed,mode=mode)
    src.unlink()  # pytest-owned synthetic fixture only
    archive.decode(packed,out)
    report=archive.verify(packed,restored=out)
    assert report["restored_file_checked"] and report["max_RI_error"] is None
    assert all(v["max_RI_error"] is None for v in report["volume_errors"])
    with h5py.File(packed,"r+") as f:f["volumes/000002/blocks/000000"][20]^=1
    failed=tmp_path/"failed.tcf"
    with pytest.raises(ValueError,match="checksum"):archive.decode(packed,failed)
    assert not failed.exists() and not list(tmp_path.glob("*.partial"))


def test_series_wrong_last_reference_and_missing_volume(tmp_path,generous_ram):
    src=tmp_path/"source.tcf";make_series(src)
    packed=tmp_path/"series.htfc";archive.encode(src,packed)
    with h5py.File(src,"r+") as f:f["Data/3D/000003"][0,0,0]+=10
    with pytest.raises(ValueError,match="native source"):archive.verify(packed,reference=src)
    with h5py.File(packed,"r+") as f:del f["volumes/000003"]
    with pytest.raises(ValueError,match="Missing time-series"):archive.inspect(packed)


@pytest.mark.parametrize("damage",["count","gap","shape"])
def test_invalid_series_metadata_rejected(tmp_path,damage):
    src=tmp_path/"source.tcf";make_series(src)
    with h5py.File(src,"r+") as f:
        g=f["Data/3D"]
        if damage=="count":g.attrs.modify("DataCount",np.array([3],dtype="<i8"))
        elif damage=="gap":g.move("000003","000004")
        else:
            del g["000003"]
            ds=g.create_dataset("000003",data=np.ones((2,3,4),dtype="<u2"))
            ds.attrs["ScalarType"]=np.array([0],dtype="<i8")
    with pytest.raises(ValueError):archive.encode(src,tmp_path/"failed.htfc")


def test_worker_failure_never_publishes(tmp_path,generous_ram,monkeypatch):
    src=tmp_path/"source.tcf";make_series(src)
    original=hashlib.sha256(src.read_bytes()).hexdigest()
    def fail(*args,**kwargs):raise ValueError("worker fault")
    monkeypatch.setattr(codec,"encode_block",fail)
    out=tmp_path/"failed.htfc"
    with pytest.raises(ValueError,match="worker fault"):archive.encode(src,out,concurrent_volumes=2)
    assert not out.exists() and not list(tmp_path.glob("*.partial"))
    assert hashlib.sha256(src.read_bytes()).hexdigest()==original


def test_planner_changes_with_RAM_CPU_and_volume_size(generous_ram):
    schema=dict(volume_count=1000,shape=[64,2832,2832])
    roomy=parallel.plan(schema,"fourier",snapshot=generous_ram)
    assert roomy["concurrent_volume_limit_W"]==6
    limited=parallel.plan(schema,"fourier",memory_gib=2,snapshot=generous_ram)
    assert limited["concurrent_volume_limit_W"]<roomy["concurrent_volume_limit_W"]
    assert limited["estimated_peak_RSS_bytes"]<=limited["memory_budget_bytes"]
    tiled=parallel.plan(schema,"fourier",memory_gib=.9,snapshot=generous_ram)
    assert tiled["input_buffer"]=="tile" and tiled["concurrent_volume_limit_W"]==1
    with pytest.raises(ValueError,match="Insufficient"):parallel.plan(schema,"fourier",memory_gib=.2,snapshot=generous_ram)
    one=parallel.plan(dict(schema,volume_count=1),"fourier",snapshot=generous_ram)
    assert one["concurrent_volume_limit_W"]==1
    restricted=parallel.plan(schema,"fourier",concurrent_volumes=2,snapshot=generous_ram)
    assert restricted["concurrent_volume_limit_W"]==2


def test_bounded_submission_does_not_queue_whole_series():
    supplied=0
    def inputs():
        nonlocal supplied
        for i in range(1000):supplied+=1;yield i
    seen=[]
    for result in parallel.bounded_map(lambda x:x,inputs(),3):
        seen.append(result)
        assert supplied-len(seen)<=3
    assert sorted(seen)==list(range(1000))


@pytest.mark.parametrize("version",["1.3.6","1.4.1"])
def test_older_profile_without_ScalarType_and_tile_fallback(tmp_path,generous_ram,version):
    src=tmp_path/"v136.tcf";packed=tmp_path/"tiled.htfc";out=tmp_path/"restored.tcf"
    make_series(src,3,(33,260,258))
    with h5py.File(src,"r+") as f:
        f.attrs.modify("FormatVersion",np.array([version.encode()],dtype="S7"))
        for ds in f["Data/3D"].values():del ds.attrs["ScalarType"]
        schema=tcf.validate_source(f)
    regular=parallel.plan(schema,"fourier",snapshot=generous_ram)
    cap=(regular["current_process_RSS_bytes"]+regular["shared_headroom_bytes"]+regular["estimated_bytes_per_active_volume"]-1)/2**30
    result=archive.encode(src,packed,memory_gib=cap)
    assert result["concurrency_plan"]["input_buffer"]=="tile"
    archive.decode(packed,out)
    assert archive.verify(packed,reference=src,restored=out)["violations"]==0


@pytest.mark.parametrize("count",[1,3])
def test_variable_length_UTF8_metadata_preserved(tmp_path,generous_ram,count):
    src=tmp_path/"source.tcf";packed=tmp_path/"strings.htfc";out=tmp_path/"out.tcf"
    make_series(src,count)
    with h5py.File(src,"r+") as f:
        f.attrs.create("UTF8Notes",np.array(["촬영 시각", "", "αβ"],dtype=h5py.string_dtype("utf-8")))
        f["Data/3D/000000"].attrs["RecordingTime"]="2026-10-21 15:00:00"
    archive.encode(src,packed);archive.decode(packed,out)
    assert archive.verify(packed,reference=src,restored=out)["violations"]==0
    with h5py.File(src,"r") as a,h5py.File(out,"r") as b:
        assert tcf.metadata(a["/"])==tcf.metadata(b["/"])
        assert list(a.attrs["UTF8Notes"])==list(b.attrs["UTF8Notes"])


@pytest.mark.parametrize("args",[["--concurrent-volumes","0"],["--concurrent-volumes","x"],["--memory-gib","nan"],["--memory-gib","-1"]])
def test_bad_schedule_flags_rejected_before_io(tmp_path,args):
    with pytest.raises(SystemExit) as error:main(["encode",str(tmp_path/"absent"),"-o",str(tmp_path/"out")]+args)
    assert error.value.code==2

