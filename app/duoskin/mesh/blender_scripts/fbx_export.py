"""Blender script: GLB -> FBX with the texture embedded (APP_SPEC 10.9 step 10; FM MESH-14, MESH-19).

Settings: Path Mode Copy + Embed Textures, Apply Scalings = FBX Unit Scale, Y up, front stays +Z (Blender's default FBX axes
-Z forward / Y up map a glTF +Z front to +Z in the FBX). Only meshes are written; no leaf bones, no animation.
"""
import json
import os
import sys
import traceback

import bpy


def _args():
    argv = sys.argv[sys.argv.index("--") + 1:]
    with open(argv[0], encoding="utf-8") as fh:
        return json.load(fh), argv[1]


def _write(path, data):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)


def main():
    args, result_path = _args()
    try:
        for obj in list(bpy.data.objects):
            bpy.data.objects.remove(obj, do_unlink=True)
        bpy.ops.import_scene.gltf(filepath=args["src"])
        meshes = [o for o in bpy.context.scene.objects if o.type == "MESH"]
        if not meshes:
            raise RuntimeError("no mesh in the GLB")
        bpy.ops.object.select_all(action="DESELECT")
        for o in meshes:
            o.select_set(True)
        os.makedirs(os.path.dirname(os.path.abspath(args["dst"])), exist_ok=True)
        bpy.ops.export_scene.fbx(
            filepath=args["dst"], use_selection=True, object_types={"MESH"}, apply_scale_options="FBX_SCALE_UNITS",
            global_scale=1.0, axis_forward="-Z", axis_up="Y", path_mode="COPY", embed_textures=True,
            bake_space_transform=False, use_mesh_modifiers=True, add_leaf_bones=False, bake_anim=False,
            mesh_smooth_type="OFF", use_triangles=True)
        _write(result_path, {"ok": True, "files": {"fbx": args["dst"]}, "blender_version": bpy.app.version_string})
    except Exception as exc:  # noqa: BLE001
        _write(result_path, {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc), "trace": traceback.format_exc()[-1500:]})
        sys.exit(3)


main()
