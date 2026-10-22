# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = ["h5py>=3.14,<4", "numpy==2.4.4"]
# ///
"""Independent original/final TCF audit; deliberately imports no htpack code."""
import hashlib
import json
from pathlib import Path
import sys
import time
import h5py
import numpy as np


def objects(root):
    result={}
    def add(name,obj):result["/"+name]=obj
    add("",root);root.visititems(add)
    return result


def attrs(obj):
    values={}
    for name in obj.attrs:
        aid=obj.attrs.get_id(name);value=np.empty(aid.shape,dtype=aid.dtype);aid.read(value)
        strings=h5py.check_string_dtype(aid.dtype)
        if strings is not None and strings.length is None:
            content=tuple(v.hex() if isinstance(v,bytes) else v.encode(strings.encoding).hex() for v in value.ravel())
        else:content=value.tobytes().hex()
        values[name]=(aid.get_type().encode().hex(),aid.get_space().encode().hex(),content)
    return values


def dataset_hash(ds):
    h=hashlib.sha256()
    if ds.ndim==0:h.update(np.asarray(ds[()]).tobytes())
    else:
        row=max(1,int(np.prod(ds.shape[1:]))*ds.dtype.itemsize)
        for start in range(0,ds.shape[0],max(1,8*2**20//row)):
            h.update(np.ascontiguousarray(ds[start:start+max(1,8*2**20//row)]).tobytes())
    return h.hexdigest()


def main():
    start=time.perf_counter();source=Path(sys.argv[1]);restored=Path(sys.argv[2]);out=Path(sys.argv[3])
    before=(source.stat().st_size,source.stat().st_mtime_ns)
    records=[];non_RI=attrs_count=0
    with h5py.File(source,"r") as a,h5py.File(restored,"r") as b:
        one=objects(a["/"]);two=objects(b["/"])
        assert set(one)==set(two)
        paths={"/Data/3D/"+key for key in a["Data/3D"]}
        for path in one:
            x=one[path];y=two[path]
            assert type(x) is type(y) and attrs(x)==attrs(y),path
            attrs_count+=len(x.attrs)
            if isinstance(x,h5py.Dataset):
                assert x.shape==y.shape and x.id.get_type().encode()==y.id.get_type().encode(),path
                assert x.maxshape==y.maxshape and x.chunks==y.chunks and x.compression==y.compression and x.compression_opts==y.compression_opts,path
                assert x.shuffle==y.shuffle and x.fletcher32==y.fletcher32 and x.scaleoffset==y.scaleoffset,path
                assert np.asarray(x.fillvalue,dtype=x.dtype).tobytes()==np.asarray(y.fillvalue,dtype=y.dtype).tobytes(),path
                if path not in paths:
                    assert dataset_hash(x)==dataset_hash(y),path
                    non_RI+=1
        for path in sorted(paths):
            x=a[path];y=b[path];maximum=0;reader_max=0.;changed=bad=0
            native=hashlib.sha256();decoded=hashlib.sha256()
            for z in range(0,x.shape[0],16):
                original=np.ascontiguousarray(x[z:z+16]);recovered=np.ascontiguousarray(y[z:z+16])
                native.update(original.tobytes());decoded.update(recovered.tobytes())
                delta=np.abs(recovered.astype("i8")-original.astype("i8"))
                reader=np.abs((recovered.astype("f4")/np.float32(10000)).astype("f8")-(original.astype("f4")/np.float32(10000)).astype("f8"))
                maximum=max(maximum,int(delta.max(initial=0)));reader_max=max(reader_max,float(reader.max(initial=0)))
                changed+=int(np.count_nonzero(delta));bad+=int(np.count_nonzero((delta>1)|(reader>1e-4)))
            assert maximum<=1 and reader_max<=1e-4 and bad==0
            records.append(dict(path=path,shape=list(x.shape),max_RI_error=maximum/10000,
                max_reader_float32_RI_error=reader_max,violations=bad,changed_voxels=changed,
                native_RI_sha256=native.hexdigest(),restored_RI_sha256=decoded.hexdigest()))
    assert before==(source.stat().st_size,source.stat().st_mtime_ns)
    result=dict(status="passed",source=str(source),restored=str(restored),volume_count=len(records),
        object_count=len(one),typed_attribute_count=attrs_count,non_RI_datasets=non_RI,
        metadata_values_types_and_spaces_identical=True,all_non_RI_values_identical=True,
        max_RI_error=max(v["max_RI_error"] for v in records),
        max_reader_float32_RI_error=max(v["max_reader_float32_RI_error"] for v in records),
        violations=0,volume_errors=records,source_stat_unchanged=True,elapsed_s=time.perf_counter()-start,
        independence="Reads actual source and final TCF; no htpack imports and no archive/manifest error values used.",
        script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    out.write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps({k:v for k,v in result.items() if k!="volume_errors"},ensure_ascii=False))


if __name__=="__main__":main()

