"""Blender script: hair polish pack files (APP_SPEC 10.7): hair_fitted.fbx, hair_fitted.blend and head_guide.fbx.

Studs, Y up, +Z front. The artist polishes ``hair_fitted`` (one mesh, one material, triangle target <= 3600, origin kept) and
saves ``return\\hair.fbx`` or ``.glb`` for the inbox watcher.
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


def _fresh_import(glb):
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    bpy.ops.import_scene.gltf(filepath=glb)
    bpy.ops.object.select_all(action="DESELECT")
    for o in bpy.context.scene.objects:
        if o.type == "MESH":
            o.select_set(True)


def _fbx(path):
    bpy.ops.export_scene.fbx(filepath=path, use_selection=True, object_types={"MESH"}, apply_scale_options="FBX_SCALE_UNITS",
                             axis_forward="-Z", axis_up="Y", path_mode="COPY", embed_textures=True, add_leaf_bones=False,
                             bake_anim=False, use_triangles=True)


def main():
    args, result_path = _args()
    try:
        out = args["out_dir"]
        os.makedirs(out, exist_ok=True)
        files = {}
        _fresh_import(args["fitted"])
        files["fbx"] = os.path.join(out, "hair_fitted.fbx")
        _fbx(files["fbx"])
        files["blend"] = os.path.join(out, "hair_fitted.blend")
        bpy.ops.wm.save_as_mainfile(filepath=files["blend"], copy=True)
        if args.get("head"):
            _fresh_import(args["head"])
            files["head_fbx"] = os.path.join(out, "head_guide.fbx")
            _fbx(files["head_fbx"])
        _write(result_path, {"ok": True, "files": files, "blender_version": bpy.app.version_string})
    except Exception as exc:  # noqa: BLE001
        _write(result_path, {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc), "trace": traceback.format_exc()[-1500:]})
        sys.exit(3)


main()
