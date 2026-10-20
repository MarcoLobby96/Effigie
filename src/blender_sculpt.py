"""
Eseguito DENTRO Blender (blender --python blender_sculpt.py -- modello.stl scolpito.stl file.blend).
Apre il modello in modalità Scultura con il pennello Smooth; ogni Ctrl+S salva il .blend
ed esporta anche scolpito.stl, che l'interfaccia usa per Bambu Studio.
"""
import sys

import bpy

args = sys.argv[sys.argv.index("--") + 1:]
src, out_stl, blend = args[0], args[1], args[2]


def view3d():
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                region = next(r for r in area.regions if r.type == "WINDOW")
                return {"window": window, "area": area, "region": region}
    return {}


def export(*_):
    obj = bpy.data.objects.get("Modello")
    if obj is None:
        return
    ctx = view3d()
    with bpy.context.temp_override(**ctx):
        mode = obj.mode
        if mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        bpy.ops.wm.stl_export(filepath=out_stl, ascii_format=False)
        if mode != "OBJECT":
            bpy.ops.object.mode_set(mode=mode)
    print("Esportato", out_stl)


def setup():
    ctx = view3d()
    if not ctx:                                    # interfaccia non ancora pronta
        return 0.5
    with bpy.context.temp_override(**ctx):
        for o in list(bpy.data.objects):          # via cubo, luce e camera di default
            bpy.data.objects.remove(o)
        bpy.ops.wm.stl_import(filepath=src)
        obj = bpy.context.selected_objects[0]
        obj.name = "Modello"
        bpy.context.view_layer.objects.active = obj
        bpy.ops.object.shade_smooth()
        bpy.ops.view3d.view_all()
        ctx["area"].spaces[0].shading.type = "SOLID"
        bpy.ops.object.mode_set(mode="SCULPT")
        try:
            bpy.ops.wm.tool_set_by_id(name="builtin_brush.Smooth")
        except Exception as e:
            print("Pennello Smooth non selezionato:", e)
        bpy.ops.wm.save_as_mainfile(filepath=blend)
    return None


if bpy.app.background:                             # uso da riga di comando/test
    bpy.ops.wm.read_homefile(use_empty=True)
    bpy.ops.wm.stl_import(filepath=src)
    bpy.context.selected_objects[0].name = "Modello"
    bpy.ops.wm.stl_export(filepath=out_stl, ascii_format=False)
else:
    bpy.app.handlers.save_post.append(export)
    bpy.app.timers.register(setup, first_interval=0.5)
