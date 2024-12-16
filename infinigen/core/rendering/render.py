# Copyright (C) 2023, Princeton University.
# This source code is licensed under the BSD 3-Clause license found in the LICENSE file in the root directory of this source tree.

# Authors:
# - Lahav Lipson - Render, flat shading, etc
# - Alex Raistrick - Compositing
# - Hei Law - Initial version


import json
import logging
import os
import time
from pathlib import Path

import bpy
import gin
import numpy as np
from imageio import imwrite

from infinigen.core import init, surface
from infinigen.core.nodes.node_wrangler import Nodes, NodeWrangler
from infinigen.core.placement import camera as cam_util
from infinigen.core.rendering.post_render import (
    colorize_depth,
    colorize_flow,
    colorize_int_array,
    colorize_normals,
    load_depth,
    load_flow,
    load_normals,
    load_seg_mask,
    load_uniq_inst,
)
from infinigen.core.util import blender as butil
from infinigen.core.util.logging import Timer
from infinigen.tools.datarelease_toolkit import reorganize_old_framesfolder
from infinigen.tools.suffixes import get_suffix

TRANSPARENT_SHADERS = {Nodes.TranslucentBSDF, Nodes.TransparentBSDF}

logger = logging.getLogger(__name__)

def configure_360_camera(camera):
    camera.data.type = 'PANO'  # Set camera type to panoramic
    # Ensure the render engine is Cycles
    camera.data.cycles.panorama_type = 'EQUIRECTANGULAR'  # Set panoramic type to equirectangular

    # Optional settings depending on your needs
    camera.data.angle_x = np.radians(360)  # Horizontal FOV
    camera.data.angle_y = np.radians(180)  # Vertical FOV

    cam_util.adjust_camera_sensor(camera)

def remove_translucency():
    # The asserts were added since these edge cases haven't appeared yet -Lahav
    for material in bpy.data.materials:
        nw = NodeWrangler(material.node_tree)
        for node in nw.nodes:
            if node.bl_idname == Nodes.MixShader:
                fac_soc, shader_1_soc, shader_2_soc = node.inputs
                assert shader_1_soc.is_linked and len(shader_1_soc.links) == 1
                assert shader_2_soc.is_linked and len(shader_2_soc.links) == 1
                shader_1_type = shader_1_soc.links[0].from_node.bl_idname
                shader_2_type = shader_2_soc.links[0].from_node.bl_idname
                assert not (
                    shader_1_type in TRANSPARENT_SHADERS
                    and shader_2_type in TRANSPARENT_SHADERS
                )
                if shader_1_type in TRANSPARENT_SHADERS:
                    assert not fac_soc.is_linked
                    fac_soc.default_value = 1.0
                elif shader_2_type in TRANSPARENT_SHADERS:
                    assert not fac_soc.is_linked
                    fac_soc.default_value = 0.0


def set_pass_indices():
    tree_output = {}
    index = 1
    for obj in bpy.data.objects:
        if obj.hide_render:
            continue
        if obj.pass_index == 0:
            obj.pass_index = index
            index += 1
        object_dict = {"type": obj.type, "object_index": obj.pass_index, "children": []}
        if obj.type == "MESH":
            object_dict["num_verts"] = len(obj.data.vertices)
            object_dict["num_faces"] = len(obj.data.polygons)
            object_dict["materials"] = obj.material_slots.keys()
            object_dict["unapplied_modifiers"] = obj.modifiers.keys()
        tree_output[obj.name] = object_dict
        for child_obj in obj.children:
            if child_obj.pass_index == 0:
                child_obj.pass_index = index
                index += 1
            object_dict["children"].append(child_obj.pass_index)
        index += 1
    return tree_output


def set_material_pass_indices():
    output_material_properties = {}
    mat_index = 1
    for mat in bpy.data.materials:
        if mat.pass_index == 0:
            mat.pass_index = mat_index
            mat_index += 1
        output_material_properties[mat.name] = {"pass_index": mat.pass_index}
    return output_material_properties


# Can be pasted directly into the blender console
def make_clay():
    clay_material = bpy.data.materials.new(name="clay")
    clay_material.diffuse_color = (0.2, 0.05, 0.01, 1)
    for obj in bpy.data.objects:
        if "atmosphere" not in obj.name.lower() and not obj.hide_render:
            if len(obj.material_slots) == 0:
                obj.active_material = clay_material
            else:
                for mat_slot in obj.material_slots:
                    mat_slot.material = clay_material


@gin.configurable
def compositor_postprocessing(
    nw,
    source,
    show=True,
    color_correct=True,
    distort=0,
    glare=False,
):
    if distort > 0:
        source = nw.new_node(
            Nodes.LensDistortion, input_kwargs={"Image": source, "Dispersion": distort}
        )

    if color_correct:
        source = nw.new_node(
            Nodes.BrightContrast,
            input_kwargs={"Image": source, "Bright": 1.0, "Contrast": 4.0},
        )

    if glare:
        source = nw.new_node(
            Nodes.Glare,
            input_kwargs={"Image": source},
            attrs={"glare_type": "GHOSTS", "threshold": 0.5, "mix": -0.99},
        )

    if show:
        nw.new_node(Nodes.Composite, input_kwargs={"Image": source})

    return source.outputs[0] if hasattr(source, "outputs") else source


@gin.configurable
def configure_compositor_output(
    nw,
    frames_folder,
    image_denoised,
    image_noisy,
    passes_to_save,
    saving_ground_truth,
):
    file_output_node_png = nw.new_node(
        Nodes.OutputFile,
        attrs={
            "base_path": str(frames_folder),
            "format.file_format": "PNG",
            "format.color_mode": "RGB",
        },
    )
    file_output_node_exr = nw.new_node(
        Nodes.OutputFile,
        attrs={
            "base_path": str(frames_folder),
            "format.file_format": "OPEN_EXR",
            "format.color_mode": "RGB",
        },
    )
    default_file_output_node = (
        file_output_node_exr if saving_ground_truth else file_output_node_png
    )
    file_slot_list = []
    viewlayer = bpy.context.scene.view_layers["ViewLayer"]
    render_layers = nw.new_node(Nodes.RenderLayers)
    for viewlayer_pass, socket_name in passes_to_save:
        if hasattr(viewlayer, f"use_pass_{viewlayer_pass}"):
            setattr(viewlayer, f"use_pass_{viewlayer_pass}", True)
        else:
            setattr(viewlayer.cycles, f"use_pass_{viewlayer_pass}", True)
        # must save the material pass index as EXR
        file_output_node = (
            default_file_output_node
            if viewlayer_pass != "material_index"
            else file_output_node_exr
        )

        slot_input = file_output_node.file_slots.new(socket_name)
        render_socket = render_layers.outputs[socket_name]
        match viewlayer_pass:
            case "vector":
                separate_color = nw.new_node(Nodes.CompSeparateColor, [render_socket])
                comnbine_color = nw.new_node(
                    Nodes.CompCombineColor,
                    [0, (separate_color, 3), (separate_color, 2), 0],
                )
                nw.links.new(comnbine_color.outputs[0], slot_input)
            case "normal":
                color = nw.new_node(
                    Nodes.CompositorMixRGB,
                    [None, render_socket, (0, 0, 0, 0)],
                    attrs={"blend_type": "ADD"},
                ).outputs[0]
                nw.links.new(color, slot_input)
            case _:
                nw.links.new(render_socket, slot_input)
        file_slot_list.append(file_output_node.file_slots[slot_input.name])

    slot_input = default_file_output_node.file_slots["Image"]
    image = image_denoised if image_denoised is not None else image_noisy
    nw.links.new(image, default_file_output_node.inputs["Image"])
    if saving_ground_truth:
        slot_input.path = "UniqueInstances"
    else:
        nw.links.new(image, file_output_node_exr.inputs["Image"])
        file_slot_list.append(file_output_node_exr.file_slots[slot_input.path])
    file_slot_list.append(default_file_output_node.file_slots[slot_input.path])

    return file_slot_list


def shader_random(nw: NodeWrangler):
    # Code generated using version 2.4.3 of the node_transpiler

    object_info = nw.new_node(Nodes.ObjectInfo_Shader)

    white_noise_texture = nw.new_node(
        Nodes.WhiteNoiseTexture, input_kwargs={"Vector": object_info.outputs["Random"]}
    )

    nw.new_node(
        Nodes.MaterialOutput,
        input_kwargs={"Surface": white_noise_texture.outputs["Color"]},
    )


def global_flat_shading():
    for obj in bpy.context.scene.view_layers["ViewLayer"].objects:
        if "fire_system_type" in obj and obj["fire_system_type"] == "volume":
            continue
        if obj.name.lower() in {"atmosphere", "atmosphere_fine"}:
            bpy.data.objects.remove(obj)
        elif obj.active_material is not None:
            nw = obj.active_material.node_tree
            for node in nw.nodes:
                if node.bl_idname == Nodes.MaterialOutput:
                    vol_socket = node.inputs["Volume"]
                    if len(vol_socket.links) > 0:
                        nw.links.remove(vol_socket.links[0])

    for obj in bpy.context.scene.view_layers["ViewLayer"].objects:
        if obj.type != "MESH":
            continue
        obj.hide_viewport = False
        if "fire_system_type" in obj and obj["fire_system_type"] == "gt_mesh":
            obj.hide_viewport = False
            obj.hide_render = False
        if not hasattr(obj, "material_slots"):
            print(obj.name, "NONE")
            continue
        with butil.SelectObjects(obj):
            for i in range(len(obj.material_slots)):
                bpy.ops.object.material_slot_remove()

    for obj in bpy.context.scene.view_layers["ViewLayer"].objects:
        surface.add_material(obj, shader_random)
    for mat in bpy.data.materials:
        nw = NodeWrangler(mat.node_tree)
        shader_random(nw)

    nw = NodeWrangler(bpy.data.worlds["World"].node_tree)
    for link in nw.links:
        nw.links.remove(link)


def postprocess_blendergt_outputs(frames_folder, output_stem):
    # Save flow visualization
    flow_dst_path = frames_folder / f"Vector{output_stem}.exr"
    flow_array = load_flow(flow_dst_path)
    np.save(flow_dst_path.with_name(f"Flow{output_stem}.npy"), flow_array)

    flow_color = colorize_flow(flow_array)
    if flow_color is not None:
        imwrite(
            flow_dst_path.with_name(f"Flow{output_stem}.png"),
            flow_color,
        )
        flow_dst_path.unlink()

    # Save surface normal visualization
    normal_dst_path = frames_folder / f"Normal{output_stem}.exr"
    normal_array = load_normals(normal_dst_path)
    np.save(flow_dst_path.with_name(f"SurfaceNormal{output_stem}.npy"), normal_array)
    imwrite(
        flow_dst_path.with_name(f"SurfaceNormal{output_stem}.png"),
        colorize_normals(normal_array),
    )
    normal_dst_path.unlink()

    # Save depth visualization
    depth_dst_path = frames_folder / f"Depth{output_stem}.exr"
    depth_array = load_depth(depth_dst_path)
    np.save(flow_dst_path.with_name(f"Depth{output_stem}.npy"), depth_array)
    imwrite(
        depth_dst_path.with_name(f"Depth{output_stem}.png"), colorize_depth(depth_array)
    )
    depth_dst_path.unlink()

    # Save segmentation visualization
    seg_dst_path = frames_folder / f"IndexOB{output_stem}.exr"
    seg_mask_array = load_seg_mask(seg_dst_path)
    np.save(
        flow_dst_path.with_name(f"ObjectSegmentation{output_stem}.npy"), seg_mask_array
    )
    imwrite(
        seg_dst_path.with_name(f"ObjectSegmentation{output_stem}.png"),
        colorize_int_array(seg_mask_array),
    )
    seg_dst_path.unlink()

    # Save unique instances visualization
    uniq_inst_path = frames_folder / f"UniqueInstances{output_stem}.exr"
    uniq_inst_array = load_uniq_inst(uniq_inst_path)
    np.save(
        flow_dst_path.with_name(f"InstanceSegmentation{output_stem}.npy"),
        uniq_inst_array,
    )
    imwrite(
        uniq_inst_path.with_name(f"InstanceSegmentation{output_stem}.png"),
        colorize_int_array(uniq_inst_array),
    )
    uniq_inst_path.unlink()


def postprocess_materialgt_output(frames_folder, output_stem):
    # Save material segmentation visualization if present
    ma_seg_dst_path = frames_folder / f"IndexMA{output_stem}.exr"
    if ma_seg_dst_path.is_file():
        ma_seg_mask_array = load_seg_mask(ma_seg_dst_path)
        np.save(
            ma_seg_dst_path.with_name(f"MaterialSegmentation{output_stem}.npy"),
            ma_seg_mask_array,
        )
        imwrite(
            ma_seg_dst_path.with_name(f"MaterialSegmentation{output_stem}.png"),
            colorize_int_array(ma_seg_mask_array),
        )
        ma_seg_dst_path.unlink()


def configure_compositor(
    frames_folder: Path,
    passes_to_save: list,
    flat_shading: bool,
):
    compositor_node_tree = bpy.context.scene.node_tree
    nw = NodeWrangler(compositor_node_tree)

    render_layers = nw.new_node(Nodes.RenderLayers)
    final_image_denoised = compositor_postprocessing(
        nw, source=render_layers.outputs["Image"]
    )

    final_image_noisy = (
        compositor_postprocessing(
            nw, source=render_layers.outputs["Noisy Image"], show=False
        )
        if bpy.context.scene.cycles.use_denoising
        else None
    )

    return configure_compositor_output(
        nw,
        frames_folder,
        image_denoised=final_image_denoised,
        image_noisy=final_image_noisy,
        passes_to_save=passes_to_save,
        saving_ground_truth=flat_shading,
    )


@gin.configurable
def render_image(
    camera: bpy.types.Object,
    frames_folder,
    passes_to_save,
    flat_shading=False,
    render_resolution_override=None,
    excludes=[],
    use_dof=False,
    dof_aperture_fstop=2.8,
):
    camera = bpy.data.objects.new("Camera", camera.data.copy())
    tic = time.time()

    for exclude in excludes:
        bpy.data.objects[exclude].hide_render = True

    init.configure_cycles_devices()

    tmp_dir = frames_folder.parent.resolve() / "tmp"
    tmp_dir.mkdir(exist_ok=True)
    bpy.context.scene.render.filepath = f"{tmp_dir}{os.sep}"

    camrig_id, subcam_id = cam_util.get_id(camera)

    if flat_shading:
        with Timer("Set object indices"):
            object_data = set_pass_indices()
            json_object = json.dumps(object_data, indent=4)
            first_frame = bpy.context.scene.frame_start
            suffix = get_suffix(
                dict(
                    cam_rig=camrig_id,
                    resample=0,
                    frame=first_frame,
                    subcam=subcam_id,
                )
            )
            (frames_folder / f"Objects{suffix}.json").write_text(json_object)

        with Timer("Flat Shading"):
            global_flat_shading()
    else:
        segment_materials = "material_index" in (x[0] for x in passes_to_save)
        if segment_materials:
            with Timer("Set material indices"):
                material_data = set_material_pass_indices()
                json_object = json.dumps(material_data, indent=4)
                first_frame = bpy.context.scene.frame_start
                suffix = get_suffix(
                    dict(
                        cam_rig=camrig_id,
                        resample=0,
                        frame=first_frame,
                        subcam=subcam_id,
                    )
                )
                (frames_folder / f"Materials{suffix}.json").write_text(json_object)

    if not bpy.context.scene.use_nodes:
        bpy.context.scene.use_nodes = True
    file_slot_nodes = configure_compositor(frames_folder, passes_to_save, flat_shading)

    indices = dict(cam_rig=camrig_id, resample=0, subcam=subcam_id)

    ## Update output names
    fileslot_suffix = get_suffix({"frame": "####", **indices})
    for file_slot in file_slot_nodes:
        file_slot.path = f"{file_slot.path}{fileslot_suffix}"

    configure_360_camera(camera)

    if use_dof == "IF_TARGET_SET":
        use_dof = camera.data.dof.focus_object is not None

    elif use_dof is not None:
        camera.data.dof.use_dof = use_dof
        camera.data.dof.aperture_fstop = dof_aperture_fstop

    if render_resolution_override is not None:
        bpy.context.scene.render.resolution_x = render_resolution_override[0]
        bpy.context.scene.render.resolution_y = render_resolution_override[1]

    # Render the scene
    bpy.context.scene.camera = camera
    with Timer("Actual rendering"):
        bpy.ops.render.render(animation=True)

    with Timer("Post Processing"):
        for frame in range(
            bpy.context.scene.frame_start, bpy.context.scene.frame_end + 1
        ):
            if flat_shading:
                bpy.context.scene.frame_set(frame)
                suffix = get_suffix(dict(frame=frame, **indices))
                postprocess_blendergt_outputs(frames_folder, suffix)
            else:
                cam_util.save_camera_parameters(
                    camera,
                    output_folder=frames_folder,
                    frame=frame,
                )
                bpy.context.scene.frame_set(frame)
                suffix = get_suffix(dict(frame=frame, **indices))
                postprocess_materialgt_output(frames_folder, suffix)

    for file in tmp_dir.glob("*.png"):
        file.unlink()

    reorganize_old_framesfolder(frames_folder)

    logger.info(f"rendering time: {time.time() - tic}")
