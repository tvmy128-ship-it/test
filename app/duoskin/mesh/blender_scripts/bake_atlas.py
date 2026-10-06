"""Blender script: bake several materials or tiled UVs into ONE 0-1 atlas (APP_SPEC 10.9 step 2; FM MESH-02).

Used only when the built-in grid atlas cannot represent the file (tiled UVs or overlapping islands). Joins the meshes,
unwraps with Smart UV Project, bakes the diffuse colour of the old materials into a new image and exports a GLB with one
material. Needs Cycles (bundled with every Blender build). [Not exercised by the test-suite: it needs Blender.]
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
        size = int(args.get("size", 1024))
        for obj in list(bpy.data.objects):
            bpy.data.objects.remove(obj, do_unlink=True)
        src = args["src"]
        if src.lower().endswith(".fbx"):
            bpy.ops.import_scene.fbx(filepath=src)
        else:
            bpy.ops.import_scene.gltf(filepath=src)
        meshes = [o for o in bpy.context.scene.objects if o.type == "MESH"]
        if not meshes:
            raise RuntimeError("no mesh in the file")
        bpy.ops.object.select_all(action="DESELECT")
        for o in meshes:
            o.select_set(True)
        bpy.context.view_layer.objects.active = meshes[0]
        if len(meshes) > 1:
            bpy.ops.object.join()
        obj = bpy.context.view_layer.objects.active
        # new UV layer for the atlas
        uv = obj.data.uv_layers.new(name="DuoSkinAtlas")
        obj.data.uv_layers.active = uv
        bpy.ops.object.mode_set(mode="EDIT")
        bpy.ops.mesh.select_all(action="SELECT")
        bpy.ops.uv.smart_project(angle_limit=1.15, island_margin=0.01)
        bpy.ops.object.mode_set(mode="OBJECT")
        image = bpy.data.images.new("DuoSkinAtlas", size, size, alpha=False)
        for mat in obj.data.materials:
            if mat is None or not mat.use_nodes:
                continue
            node = mat.node_tree.nodes.new("ShaderNodeTexImage")
            node.image = image
            mat.node_tree.nodes.active = node
        scene = bpy.context.scene
        scene.render.engine = "CYCLES"
        scene.cycles.samples = 8
        scene.cycles.bake_type = "DIFFUSE"
        scene.render.bake.use_pass_direct = False
        scene.render.bake.use_pass_indirect = False
        scene.render.bake.use_pass_color = True
        scene.render.bake.margin = 8
        bpy.ops.object.bake(type="DIFFUSE")
        # one material that shows the baked image
        new_mat = bpy.data.materials.new("DuoSkinAtlas")
        new_mat.use_nodes = True
        tree = new_mat.node_tree
        tex = tree.nodes.new("ShaderNodeTexImage")
        tex.image = image
        bsdf = tree.nodes.get("Principled BSDF")
        tree.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
        obj.data.materials.clear()
        obj.data.materials.append(new_mat)
        for layer in list(obj.data.uv_layers):
            if layer.name != "DuoSkinAtlas":
                obj.data.uv_layers.remove(layer)
        os.makedirs(os.path.dirname(os.path.abspath(args["dst"])), exist_ok=True)
        bpy.ops.export_scene.gltf(filepath=args["dst"], export_format="GLB", export_yup=True, use_selection=False)
        _write(result_path, {"ok": True, "files": {"glb": args["dst"]}, "blender_version": bpy.app.version_string})
    except Exception as exc:  # noqa: BLE001
        _write(result_path, {"ok": False, "error": f"{type(exc).__name__}: {exc}", "trace": traceback.format_exc()[-1500:]})
        sys.exit(3)


main()
