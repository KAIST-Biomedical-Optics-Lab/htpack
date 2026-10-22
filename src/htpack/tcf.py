"""Strict supported TCF scalar-RI profiles and exact metadata preservation."""
import hashlib
import json
from itertools import product
import h5py
import numpy as np

RI_PATH="/Data/3D/000000"
BLOCK_SHAPE=(32,256,256)
SUPPORTED_VERSIONS=("1.3.6","1.4.1","1.6.3")


def scalar_attr(obj,key):
    value=np.asarray(obj.attrs[key]).reshape(-1)
    if value.size!=1:
        raise ValueError(f"Attribute {obj.name}:{key} is not scalar-valued")
    v=value[0]
    return v.decode("utf8").rstrip("\x00") if isinstance(v,bytes) else v.item() if isinstance(v,np.generic) else v


def _dtype_allowed(dtype,allow_vlen_string=False):
    string=h5py.check_string_dtype(dtype)
    if allow_vlen_string and string is not None and string.length is None:return True
    return dtype.kind in "biufcSV" and dtype.fields is None and h5py.check_dtype(ref=dtype) is None and h5py.check_dtype(vlen=dtype) is None


def preflight(root):
    """Reject links/references unsupported by the minimal self-contained writer."""
    visited=set()
    def visit(obj):
        address=int(h5py.h5o.get_info(obj.id).addr)
        if address in visited:
            raise ValueError(f"Multiple hard links or cycles are unsupported: {obj.name}")
        visited.add(address)
        for name in obj.attrs:
            if not _dtype_allowed(obj.attrs.get_id(name).dtype,allow_vlen_string=True):
                raise ValueError(f"Unsupported reference/variable-length/compound attribute: {obj.name}:{name}")
        if isinstance(obj,h5py.Dataset):
            if not _dtype_allowed(obj.dtype) or obj.is_virtual or obj.external:
                raise ValueError(f"Unsupported dataset type or external storage: {obj.name}")
            return
        for name in obj:
            link=obj.get(name,getlink=True)
            if not isinstance(link,h5py.HardLink):
                raise ValueError(f"Soft/external link is unsupported: {obj.name}/{name}")
            child=obj[name]
            if not isinstance(child,(h5py.Group,h5py.Dataset)):
                raise ValueError(f"Unsupported named object: {child.name}")
            visit(child)
    visit(root)


def validate_source(f):
    if "FormatVersion" not in f.attrs or scalar_attr(f,"FormatVersion") not in SUPPORTED_VERSIONS:
        raise ValueError("Supported TCF FormatVersion profiles: 1.3.6, 1.4.1 and 1.6.3")
    version=scalar_attr(f,"FormatVersion")
    if "Data/3D" not in f or not isinstance(f["Data/3D"],h5py.Group):
        raise ValueError("Missing /Data/3D RI group")
    g=f["Data/3D"]
    count=scalar_attr(g,"DataCount")
    if type(count) is not int or not 1<=count<=1000000 or sorted(g.keys())!=[f"{i:06d}" for i in range(count)]:
        raise ValueError("RI DataCount must match contiguous time indices 000000,000001,...")
    shape=tuple(scalar_attr(g,"Size"+a) for a in "ZYX")
    if any(type(n) is not int or not 0<n<=65536 for n in shape):
        raise ValueError("Invalid RI volume shape metadata")
    paths=["/Data/3D/"+name for name in sorted(g)]
    for path in paths:
        ds=f[path.lstrip("/")]
        if not isinstance(ds,h5py.Dataset) or ds.ndim!=3 or ds.dtype.str!="<u2":
            raise ValueError("MVP supports little-endian uint16 ScalarType=0 RI only")
        if "ScalarType" in ds.attrs:
            if scalar_attr(ds,"ScalarType")!=0:raise ValueError("Unsupported RI ScalarType")
        elif version not in ("1.3.6","1.4.1"):raise ValueError("Missing RI ScalarType")
        if ds.shape!=shape:raise ValueError("RI volume count/shape metadata mismatch")
    if any(not np.isfinite(scalar_attr(g,"Resolution"+a)) or scalar_attr(g,"Resolution"+a)<=0 for a in "ZYX"):
        raise ValueError("Missing/invalid spatial sampling metadata")
    preflight(f["."])
    return dict(format_version=version,ri_path=RI_PATH,ri_paths=paths,shape=list(shape),dtype="<u2",
                axes="ZYX",ri_scale_denominator=10000,ri_offset=0,
                spacing_zyx_um=[float(scalar_attr(g,"Resolution"+a)) for a in "ZYX"],
                single_RI_volume=count==1,volume_count=count,time_lapse=count>1,
                time_order="Numeric /Data/3D dataset index; all acquisition metadata preserved")


def copy_attrs(source,dest):
    """Preserve attribute values, datatype (including string padding) and dataspace."""
    for name in source.attrs:
        a=source.attrs.get_id(name)
        out=h5py.h5a.create(dest.id,name.encode("utf8"),a.get_type(),a.get_space())
        value=np.empty(a.shape,dtype=a.dtype)
        a.read(value);out.write(value)


def copy_tree(source,dest,ri_path=RI_PATH):
    if source.name in ("/","/tcf"):
        ri_path={"/Data/3D/"+name for name in source["Data/3D"]}
    elif isinstance(ri_path,str):ri_path={ri_path}
    copy_attrs(source,dest)
    for name in source:
        obj=source[name]
        logical=obj.name.removeprefix("/tcf") or "/"
        if isinstance(obj,h5py.Group):
            ident=h5py.h5g.create(dest.id,name.encode("utf8"),gcpl=obj.id.get_create_plist())
            group=h5py.Group(ident)
            copy_tree(obj,group,ri_path)
        elif logical in ri_path:
            ident=h5py.h5d.create(dest.id,name.encode("utf8"),obj.id.get_type(),obj.id.get_space(),dcpl=obj.id.get_create_plist())
            copy_attrs(obj,h5py.Dataset(ident))
        else:
            source.file.copy(obj,dest,name=name,expand_soft=False,expand_external=False,expand_refs=False)


def metadata(root):
    records=[]
    def visit(name,obj):
        attrs=[]
        for key in sorted(obj.attrs):
            aid=obj.attrs.get_id(key);value=np.empty(aid.shape,dtype=aid.dtype);aid.read(value)
            record=dict(name=key,shape=list(aid.shape),dtype=aid.dtype.str,hdf5_type=aid.get_type().encode().hex())
            string=h5py.check_string_dtype(aid.dtype)
            if string is not None and string.length is None:
                # Object-array bytes are process-local pointers, not the HDF5
                # string values. Preserve the datatype plus each string's bytes.
                record["vlen_string_bytes"]=[(v if isinstance(v,bytes) else v.encode(string.encoding)).hex() for v in value.ravel()]
            else:record["bytes"]=value.tobytes().hex()
            attrs.append(record)
        row=dict(path="/"+name,kind="dataset" if isinstance(obj,h5py.Dataset) else "group",attrs=attrs)
        if isinstance(obj,h5py.Dataset):
            filters=[];props=obj.id.get_create_plist()
            for i in range(props.get_nfilters()):
                ident,flags,values,label=props.get_filter(i)
                filters.append([ident,flags,list(values),label.decode("utf8",errors="backslashreplace")])
            row.update(shape=list(obj.shape),dtype=obj.dtype.str,hdf5_type=obj.id.get_type().encode().hex(),
                maxshape=list(obj.maxshape),chunks=list(obj.chunks) if obj.chunks else None,filters=filters,
                fill_bytes=np.asarray(obj.fillvalue,dtype=obj.dtype).tobytes().hex())
        records.append(row)
    visit("",root);root.visititems(visit)
    return records


def metadata_digest(records):
    return hashlib.sha256(json.dumps(records,sort_keys=True,separators=(",",":"),allow_nan=False).encode()).hexdigest()


def dataset_digest(ds):
    h=hashlib.sha256()
    if ds.ndim==0:
        h.update(np.asarray(ds[()]).tobytes())
    elif ds.shape[0]:
        row_bytes=max(1,int(np.prod(ds.shape[1:]))*ds.dtype.itemsize)
        step=max(1,16*2**20//row_bytes)
        for start in range(0,ds.shape[0],step):
            values=np.ascontiguousarray(ds[start:start+step])
            h.update(memoryview(values).cast("B"))
    return h.hexdigest()


def other_hashes(root):
    result={}
    ri_paths={"/Data/3D/"+name for name in root["Data/3D"]}
    def visit(name,obj):
        if isinstance(obj,h5py.Dataset) and "/"+name not in ri_paths:
            result["/"+name]=dataset_digest(obj)
    root.visititems(visit)
    return result


def block_layout(shape,block_shape=BLOCK_SHAPE):
    for begin in product(*(range(0,n,b) for n,b in zip(shape,block_shape))):
        size=tuple(min(b,n-s) for s,b,n in zip(begin,block_shape,shape))
        yield begin,size


def slices(begin,size):
    return tuple(slice(s,s+n) for s,n in zip(begin,size))


def readable_attrs(obj):
    def show(v):
        if isinstance(v,bytes):return v.decode("utf8",errors="backslashreplace").rstrip("\x00")
        if isinstance(v,np.ndarray):return [show(x) for x in v.ravel()] if v.size<200 else {"shape":list(v.shape),"dtype":str(v.dtype)}
        if isinstance(v,np.generic):return v.item()
        return str(v) if not isinstance(v,(str,int,float,bool,type(None))) else v
    return {k:show(v) for k,v in obj.attrs.items()}


def inspect_tcf(f):
    objects=[]
    def visit(name,obj):
        row=dict(path="/"+name,kind="dataset" if isinstance(obj,h5py.Dataset) else "group",attributes=readable_attrs(obj))
        if isinstance(obj,h5py.Dataset):
            row.update(shape=list(obj.shape),dtype=obj.dtype.str,logical_bytes=obj.size*obj.dtype.itemsize,
                       storage_bytes=obj.id.get_storage_size(),compression=obj.compression)
        objects.append(row)
    visit("",f["/"]);f.visititems(visit)
    try: supported=validate_source(f);reason=None
    except (ValueError,KeyError,TypeError) as exc:supported=None;reason=str(exc)
    return dict(format="TCF",mvp_encode_supported=supported is not None,unsupported_reason=reason,
                scalar_RI=supported,objects=objects)
