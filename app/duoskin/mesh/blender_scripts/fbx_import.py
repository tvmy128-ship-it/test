"""Blender script: import FBX / OBJ / .blend / glTF and write a clean GLB (APP_SPEC 10.9 step 1, FM ACC-12).

Cleans the scene for a rigid accessory: armatures and skin weights removed, parents flattened, transforms applied,
every mesh triangulated. Coordinates stay in the file's own frame (Y up in, Y up out), so the app's 24-rotation
orientation search decides which way is front. Textures are written by the glTF exporter (PNG or JPEG; the app
re-saves them as PNG).
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


def _clear():
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)


def _import(kind, src):
    if kind == "fbx":
        bpy.ops.import_scene.fbx(filepath=src, use_anim=False)
    elif kind == "obj":
        if hasattr(bpy.ops.wm, "obj_import"):
            bpy.ops.wm.obj_import(filepath=src)
        else:
            bpy.ops.import_scene.obj(filepath=src)
    elif kind == "blend":
        bpy.ops.wm.open_mainfile(filepath=src)
    elif kind == "glb":
        bpy.ops.import_scene.gltf(filepath=src)
    else:
        raise RuntimeError("unsupported kind " + str(kind))


def _clean_for_rigid():
    meshes = [o for o in bpy.context.scene.objects if o.type == "MESH"]
    if not meshes:
        raise RuntimeError("the file has no mesh objects")
    for obj in list(bpy.context.scene.objects):
        if obj.type not in ("MESH",):
            if obj.type == "ARMATURE":
                for child in obj.children:
                    child.parent = None
            bpy.data.objects.remove(obj, do_unlink=True)
    bpy.ops.object.select_all(action="DESELECT")
    for obj in bpy.context.scene.objects:
        if obj.type != "MESH":
            continue
        for mod in list(obj.modifiers):
            if mod.type == "ARMATURE":
                obj.modifiers.remove(mod)
        obj.vertex_groups.clear()
        bpy.context.view_layer.objects.active = obj
        obj.select_set(True)
        if obj.parent is not None:
            mw = obj.matrix_world.copy()
            obj.parent = None
            obj.matrix_world = mw
        tri = obj.modifiers.new("DuoSkinTriangulate", "TRIANGULATE")
        tri.quad_method = "BEAUTY"
        tri.ngon_method = "BEAUTY"
        bpy.ops.object.modifier_apply(modifier=tri.name)
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)


def _export_glb(dst):
    kwargs = dict(filepath=dst, export_format="GLB", export_yup=True, export_apply=True, use_selection=False,
                  export_image_format="AUTO", export_materials="EXPORT", export_normals=True, export_cameras=False,
                  export_lights=False, export_skins=False)
    while True:
        try:
            bpy.ops.export_scene.gltf(**kwargs)
            return
        except TypeError as exc:           # an option this Blender version does not have: drop it and retry
            msg = str(exc)
            dropped = False
            for key in list(kwargs):
                if key in msg and key != "filepath":
                    kwargs.pop(key)
                    dropped = True
            if not dropped:
                raise


def main():
    args, result_path = _args()
    try:
        _clear()
        _import(args["kind"], args["src"])
        _clean_for_rigid()
        os.makedirs(os.path.dirname(os.path.abspath(args["dst"])), exist_ok=True)
        _export_glb(args["dst"])
        _write(result_path, {"ok": True, "files": {"glb": args["dst"]}, "blender_version": bpy.app.version_string})
    except Exception as exc:  # noqa: BLE001
        _write(result_path, {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc), "trace": traceback.format_exc()[-1500:]})
        sys.exit(3)


main()
