"""3D import, repair, validation and export for rigid accessories and hair (APP_SPEC 10.7 to 10.9, 11).

Conventions everywhere: studs, Y up, the object's front faces +Z, UVs in glTF image space (v down).

Modules (nothing here is imported by the server process except ``worker.run_job`` and the pure helpers in ``pipeline``;
meshes are only parsed inside ``python -m duoskin.mesh.worker``):

* ``load``       magic-byte sniffing, glTF/GLB through trimesh, extension policy, FBX through Blender
* ``repair``     weld, degenerate/duplicate faces, islands, UV-preserving decimation, holes, winding, scale and placement
* ``decimate``   built-in texture-aware quadric edge collapse (the pymeshlab fallback)
* ``orient``     24-rotation silhouette search, mirrored models flagged and never flipped
* ``validate``   facts and CHK-M01..M21 on exported files (Roblox rules live in ``duoskin.roblox.mesh_validators``)
* ``export``     ``.gltf`` + ``.bin`` + PNG, GLB archive, FBX via Blender, the F-fixture round trip (CHK-M19)
* ``slab``       sticker and hair-clip slabs; ``primitives`` parametric accessories for the no-Tripo path
* ``hair``       ``hair.register`` (cut the grey head cube out of Tripo hair) and kit hair assembly
* ``worker``     the subprocess boundary: ``run_job(MeshJob) -> MeshResult`` with timeout and tree kill
* ``blender``    headless Blender bridge; ``proc`` process runner; ``fixtures`` code-built test meshes
"""
