"""Optional geometry probe, executed only in an explicitly configured tool environment."""
import sys,json,time,contextlib,importlib.metadata
from pathlib import Path
import numpy as np,trimesh
sys.path.insert(0,sys.argv[3])
from MeshTweaker import Tweak
from FileHandler import FileHandler
from shapely.geometry import MultiPoint,box
cases=json.loads(Path(sys.argv[1]).read_text());out={}
envelopes=Path(sys.argv[2]).parent/'envelopes';envelopes.mkdir(exist_ok=True)
np.random.seed(42)
for name,boxes in cases.items():
 parts=[]
    for x,y,z,w,d,h,_ in boxes:
  m=trimesh.creation.box([w,d,h]);m.apply_translation([x+w/2,y+d/2,z+h/2]);parts.append(m)
 m=trimesh.util.concatenate(parts);m.export(str(envelopes/(name+'.stl')))
 t=time.perf_counter()
 with contextlib.redirect_stdout(sys.stderr):a=Tweak(next(iter(FileHandler().load_mesh(str(envelopes/(name+'.stl'))).values()))["mesh"],extended_mode=True,verbose=False,min_volume=True)
 orientations=[]
 for r in a.all_orientations:
  mat=np.asarray(a.euler(r[:3])[2]).T
  if not any(np.allclose(mat,p['rotation_matrix'],atol=1e-6) for p in orientations):orientations.append({'rotation_matrix':mat.tolist(),'heuristic_score':float(r[6])})
 tweaker_seconds=time.perf_counter()-t
 t=time.perf_counter();poses,prob=m.compute_stable_poses(sigma=0,n_samples=1,threshold=0);trimesh_seconds=time.perf_counter()-t
 stable=[{'rotation_matrix':p[:3,:3].tolist(),'heuristic_score':float(-q)} for p,q in sorted(zip(poses,prob),key=lambda x:-x[1])]
 # Bed fit accounts for silhouette and inflated brim, not a bounding-box claim.
 footprint=MultiPoint(m.vertices[:,:2]).convex_hull
 footprint_ok=box(0,0,180,180).covers(footprint.buffer(5))
 out[name]={'tweaker':orientations[:4],'trimesh':stable[:4],'tweaker_seconds':tweaker_seconds,'trimesh_seconds':trimesh_seconds,'watertight':bool(m.is_watertight),'volume':float(m.volume),'triangles':len(m.faces),'brim_fits_at_current_origin':footprint_ok}
json.dump({'versions':{p:importlib.metadata.version(p) for p in ('numpy','trimesh','shapely','scipy','optuna')},'cases':out},open(sys.argv[2],'w'),indent=2)
