"""Meaningful round-trip, error-bound and preservation/failure-path tests."""
import hashlib
import json
import shutil
import h5py
import numpy as np
import pytest
from htpack import archive,codec,tcf
from htpack.cli import main


def make_tcf(path,shape=(5,17,19)):
    rng=np.random.default_rng(20261021)
    with h5py.File(path,"w") as f:
        f.attrs.create("FormatVersion",np.array([b"1.6.3"],dtype="S7"))
        f.attrs.create("Title",np.array([b"TCF RI fixture"],dtype="S21"))
        f.attrs.create("TypedInt",np.array([3,9],dtype="<i2"))
        data=f.create_group("Data");g=data.create_group("3D")
        for key,value in dict(DataCount=1,SizeZ=shape[0],SizeY=shape[1],SizeX=shape[2]).items():
            g.attrs.create(key,np.array([value],dtype="<i8"))
        for axis in "XYZ":g.attrs.create("Resolution"+axis,np.array([.125 if axis!="Z" else .5],dtype="<f8"))
        ds=g.create_dataset("000000",data=rng.integers(13290,13700,size=shape,dtype="<u2"))
        ds.attrs.create("ScalarType",np.array([0],dtype="<i8"))
        ds.attrs.create("PositionX",np.array([1.23],dtype="<f8"))
        mip=data.create_group("2DMIP");mip.create_dataset("000000",data=ds[()].max(axis=0))
        data.create_group("BF").create_dataset("000000",data=rng.integers(0,255,(3,17,19),dtype="u1"))
        meta=f.create_group("Info").create_group("Annotation");meta.attrs["empty"]=np.array([b""],dtype="S2")
        meta.create_dataset("opaque",data=rng.integers(0,255,200,dtype="u1"),compression="gzip")


@pytest.mark.parametrize("shape",[(1,1,1),(5,7,9),(6,8,10),(32,30,26)])
@pytest.mark.parametrize("mode",["fourier","fast"])
def test_scalar_limits(shape,mode):
    x=np.resize(np.arange(13200,13600,dtype="<u2"),int(np.prod(shape))).reshape(shape)
    blob,h=codec.encode_block(x,1e-4,mode);y=codec.decode_block(blob)
    q=codec.quality(x,y,1e-4)
    assert q["violations"]==0
    assert q["max_RI_error"]<=1e-4 and q["max_reader_float32_RI_error"]<=1e-4
    assert y.dtype==np.dtype("<u2") and y.shape==shape and codec.digest(y)==h["decoded_sha256"]


@pytest.mark.parametrize("mode",["fourier","fast"])
def test_stricter_budget_and_constant(mode):
    x=np.arange(1000,1120,dtype="<u2").reshape(4,5,6)
    blob,_=codec.encode_block(x,1e-6,mode)
    np.testing.assert_array_equal(codec.decode_block(blob),x)
    x[:]=13337;blob,h=codec.encode_block(x,1e-4,mode)
    assert codec.quality(x,codec.decode_block(blob),1e-4)["violations"]==0
    if mode=="fourier":assert h["guard_count"]==0


@pytest.mark.parametrize("eps",[0,-1,float("nan"),float("inf")])
@pytest.mark.parametrize("mode",["fourier","fast"])
def test_bad_budget(eps,mode):
    with pytest.raises(ValueError):codec.encode_block(np.zeros((3,3,3),dtype="u2"),eps,mode)


def test_final_float32_guard():
    x=np.resize(np.arange(13200,13600,dtype="<u2"),4096).reshape(8,16,32)
    blob,h=codec.encode_block(x,1e-4)
    assert h["guard_count"]>0
    delta=np.abs(codec.reader_values(codec.decode_block(blob)).astype("f8")-codec.reader_values(x).astype("f8"))
    assert delta.max()<=1e-4


@pytest.mark.parametrize("mode",["fourier","fast"])
def test_full_tcf_roundtrip_and_commands(tmp_path,capsys,mode):
    src=tmp_path/"original.TCF";packed=tmp_path/"sample.htfc";out=tmp_path/"restored.TCF"
    make_tcf(src,(33,260,258))
    original=hashlib.sha256(src.read_bytes()).hexdigest()
    assert main(["inspect",str(src)])==0
    assert json.loads(capsys.readouterr().out)["mvp_encode_supported"]
    assert main(["encode",str(src),"-o",str(packed)]+(["--fast"] if mode=="fast" else []))==0
    report=json.loads(capsys.readouterr().out);assert report["block_count"]==8 and report["violations"]==0
    assert report["mode"]==mode and report["codec"]==codec.CODECS[mode]
    assert main(["decode",str(packed),"-o",str(out)])==0
    assert json.loads(capsys.readouterr().out)["metadata_preserved"]
    assert main(["verify",str(packed),"--reference",str(src),"--restored",str(out)])==0
    q=json.loads(capsys.readouterr().out)
    assert q["violations"]==0 and q["restored_file_checked"] and q["max_reader_float32_RI_error"]<=1e-4
    with h5py.File(src,"r") as a,h5py.File(out,"r") as b:
        assert tcf.metadata(a["/"])==tcf.metadata(b["/"])
        assert tcf.other_hashes(a["/"])==tcf.other_hashes(b["/"])
    assert hashlib.sha256(src.read_bytes()).hexdigest()==original
    assert archive.inspect(packed)["mode"]==mode
    q=archive.verify(packed)
    assert q["max_RI_error"] is None and not q["reference_supplied"]
    with pytest.raises(ValueError,match="overwrite"):archive.decode(packed,out)


@pytest.mark.parametrize("mode",["fourier","fast"])
def test_tamper_fails_without_output(tmp_path,mode):
    src=tmp_path/"a.tcf";packed=tmp_path/"a.htfc";out=tmp_path/"failed.tcf"
    make_tcf(src);archive.encode(src,packed,mode=mode)
    with h5py.File(packed,"r+") as f:f["blocks/000000"][20]^=1
    with pytest.raises(ValueError,match="checksum"):archive.decode(packed,out)
    assert not out.exists() and not list(tmp_path.glob("*.partial"))


@pytest.mark.parametrize("mode",["fourier","fast"])
def test_metadata_and_wrong_reference(tmp_path,mode):
    src=tmp_path/"a.tcf";packed=tmp_path/"a.htfc";wrong=tmp_path/"wrong.tcf"
    make_tcf(src);archive.encode(src,packed,mode=mode);shutil.copyfile(src,wrong)
    with h5py.File(wrong,"r+") as f:f[tcf.RI_PATH][0,0,0]+=10
    with pytest.raises(ValueError,match="native source"):archive.verify(packed,wrong)
    with h5py.File(packed,"r+") as f:f["tcf"].attrs.modify("Title",np.array([b"changed"],dtype="S21"))
    with pytest.raises(ValueError,match="metadata checksum"):archive.verify(packed)


@pytest.mark.parametrize("mode",["fourier","fast"])
def test_decode_without_original_file(tmp_path,mode):
    src=tmp_path/"throwaway_fixture.tcf";packed=tmp_path/"standalone.htfc";out=tmp_path/"restored.tcf"
    make_tcf(src)
    archive.encode(src,packed,mode=mode)
    src.unlink()  # Synthetic pytest-owned fixture only; never a user source.
    result=archive.decode(packed,out)
    assert out.exists() and not result["original_required"]
    q=archive.verify(packed,restored=out)
    assert q["restored_file_checked"] and not q["reference_supplied"]
    assert q["max_RI_error"] is None


def test_unsupported_reference_and_version(tmp_path):
    src=tmp_path/"a.tcf";out=tmp_path/"a.htfc";make_tcf(src)
    with h5py.File(src,"r+") as f:f["Info"].attrs["object_ref"]=f[tcf.RI_PATH].ref
    with pytest.raises(ValueError,match="attribute"):archive.encode(src,out)
    assert not out.exists()
    with h5py.File(src,"r+") as f:
        del f["Info"].attrs["object_ref"]
        f.attrs.modify("FormatVersion",np.array([b"1.6.2"],dtype="S7"))
    with pytest.raises(ValueError,match="1.6.3"):archive.encode(src,out)


def test_fast_does_not_call_fft_or_mutate_source(monkeypatch):
    def forbidden(*args,**kwargs):raise AssertionError("FFT/Fourier path reached in --fast")
    for name in ("rfftn","irfftn"):monkeypatch.setattr(codec.fft,name,forbidden)
    monkeypatch.setattr(codec,"bands",forbidden)
    monkeypatch.setattr(codec,"_predict",forbidden)
    x=np.resize(np.arange(13268,13596,dtype="<u2"),4096).reshape(8,16,32)
    before=x.tobytes();blob,h=codec.encode_block(x,1e-4,"fast")
    y=codec.decode_block(blob)
    assert x.tobytes()==before and h["keepbits"]==13
    assert {r["name"] for r in h["streams"]}=={"base","guard_index","guard_values"}
    assert codec.quality(x,y,1e-4)["violations"]==0


def test_fast_uint16_limits():
    x=np.resize(np.array([0,1,65534,65535],dtype="<u2"),60).reshape(3,4,5)
    blob,_=codec.encode_block(x,1e-4,"fast")
    assert codec.quality(x,codec.decode_block(blob),1e-4)["violations"]==0
    with pytest.raises(ValueError,match="mode"):codec.encode_block(x,1e-4,"unknown")


def test_legacy_manifest_without_mode(tmp_path):
    src=tmp_path/"a.tcf";packed=tmp_path/"a.htfc";make_tcf(src);archive.encode(src,packed)
    with h5py.File(packed,"r+") as f:
        m=json.loads(f["manifest"][()].tobytes());m.pop("mode");m["tool_version"]="0.1.0"
        raw=json.dumps(m,sort_keys=True,separators=(",",":"),allow_nan=False).encode()
        del f["manifest"];f.create_dataset("manifest",data=np.frombuffer(raw,dtype="u1"))
    assert archive.inspect(packed)["mode"]=="fourier"
    assert archive.verify(packed,reference=src)["violations"]==0


@pytest.mark.parametrize("mode",["fourier","fast"])
@pytest.mark.parametrize("scale,epsilon",[("1",1e-4),("5",5e-4),("0.1",1e-5),("3",3e-4)])
def test_cli_scaled_error_end_to_end(tmp_path,capsys,mode,scale,epsilon):
    src=tmp_path/"source.tcf";packed=tmp_path/"scaled.htfc";out=tmp_path/"restored.tcf"
    make_tcf(src,(7,19,23))
    flags=["--fast"] if mode=="fast" else []
    assert main(["encode",str(src),"-o",str(packed),"--ri-error-scale",scale]+flags)==0
    encoded=json.loads(capsys.readouterr().out)
    assert encoded["epsilon"]==epsilon and encoded["ri_error_scale"]==float(scale)
    assert encoded["mode"]==mode
    assert main(["decode",str(packed),"-o",str(out)])==0
    decoded=json.loads(capsys.readouterr().out)
    assert decoded["epsilon"]==epsilon and decoded["ri_error_scale"]==float(scale)
    assert main(["inspect",str(packed)])==0
    inspected=json.loads(capsys.readouterr().out)
    assert inspected["epsilon"]==epsilon and inspected["ri_error_scale"]==float(scale)
    assert main(["verify",str(packed),"--reference",str(src),"--restored",str(out)])==0
    checked=json.loads(capsys.readouterr().out)
    assert checked["epsilon"]==epsilon and checked["ri_error_scale"]==float(scale)
    assert checked["violations"]==0 and checked["max_RI_error"]<=epsilon
    assert checked["max_reader_float32_RI_error"]<=epsilon
    with h5py.File(src,"r") as a,h5py.File(out,"r") as b:
        assert tcf.metadata(a["/"])==tcf.metadata(b["/"])
        assert tcf.other_hashes(a["/"])==tcf.other_hashes(b["/"])
        if epsilon<1e-4:np.testing.assert_array_equal(a[tcf.RI_PATH][()],b[tcf.RI_PATH][()])


@pytest.mark.parametrize("mode",["fourier","fast"])
def test_default_scaled_and_absolute_options_have_identical_packets(tmp_path,capsys,mode):
    src=tmp_path/"source.tcf";make_tcf(src)
    packets=[]
    for i,option in enumerate(([],["--ri-error-scale","1"],["--max-ri-error","0.0001"])):
        out=tmp_path/f"{i}.htfc"
        assert main(["encode",str(src),"-o",str(out)]+option+(["--fast"] if mode=="fast" else []))==0
        report=json.loads(capsys.readouterr().out)
        assert report["epsilon"]==1e-4 and report["ri_error_scale"]==1
        with h5py.File(out,"r") as f:packets.append(f["blocks/000000"][()].tobytes())
    assert packets[0]==packets[1]==packets[2]


@pytest.mark.parametrize("value",["0","-1","nan","inf","1000.1","1e-999999","1e999999999","abc"])
def test_invalid_error_scale_rejected_before_io(tmp_path,value):
    out=tmp_path/"never-created.htfc"
    with pytest.raises(SystemExit) as error:
        main(["encode",str(tmp_path/"missing.tcf"),"-o",str(out),"--ri-error-scale",value])
    assert error.value.code==2 and not out.exists()


def test_conflicting_error_options_rejected(tmp_path):
    out=tmp_path/"never-created.htfc"
    with pytest.raises(SystemExit) as error:
        main(["encode",str(tmp_path/"missing.tcf"),"-o",str(out),"--ri-error-scale","5","--max-ri-error","0.0001"])
    assert error.value.code==2 and not out.exists()
