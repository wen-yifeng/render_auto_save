bl_info = {
    "name": "渲染自动保存",
    "author": "一枫",
    "version": (4, 0, 1),
    "blender": (5, 0, 0),
    "location": "View3D > N Panel > 渲染自动保存",
    "description": "智能自动编号保存 Render / Viewport / ID Pass，修复编号缓存导致的偶发覆盖，渲染前强制防覆盖检测",
    "category": "Render",
}

import bpy
import os
import re
import subprocess
import sys
import string
from bpy.app.handlers import persistent
from datetime import datetime

# ====================== 全局状态 ======================
_render_state = {
    "is_running": False,
    "original_filepath": "",
    "target_filepath": "",
    "target_dir": "",
    "render_type": "",
    "camera_name": "",
    "original_settings": {},
    "is_id_pass": False,
    "cached_next_num": 1,
    "cached_blend_name": "",
    "cached_camera_name": "",
    "cached_render_type": "",
    "needs_update": True,
}

addon_keymaps = []

# ====================== 偏好设置 ======================
class AutoRenderAddonPreferences(bpy.types.AddonPreferences):
    bl_idname = __name__

    save_path: bpy.props.StringProperty(
        name="全局保存路径",
        description="所有工程通用的渲染输出文件夹（支持 // 相对路径）",
        default="//RenderOutput/",
        subtype='DIR_PATH',
    )
    auto_open_after_render: bpy.props.BoolProperty(
        name="渲染完成后自动打开文件夹",
        description="正式渲染完成后自动打开输出目录",
        default=True,
    )
    filename_template: bpy.props.StringProperty(
        name="文件名模板",
        description="支持 {blend} {camera} {type} {date} {num:03d}",
        default="{blend}-{type}-{camera}-{num:03d}",
    )

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False

        box = layout.box()
        box.label(text="基础设置", icon='FILE_FOLDER')
        box.prop(self, "save_path")
        box.prop(self, "auto_open_after_render")
        box.prop(self, "filename_template")


# ====================== 辅助函数 ======================
_FILENAME_FORMATTER = string.Formatter()

# Blender 常见图片输出扩展名。用于在渲染前检测“实际会写出的文件”是否已存在。
_IMAGE_FORMAT_EXTENSIONS = {
    "BMP": ".bmp",
    "IRIS": ".rgb",
    "PNG": ".png",
    "JPEG": ".jpg",
    "JPEG2000": ".jp2",
    "TARGA": ".tga",
    "TARGA_RAW": ".tga",
    "CINEON": ".cin",
    "DPX": ".dpx",
    "OPEN_EXR": ".exr",
    "OPEN_EXR_MULTILAYER": ".exr",
    "HDR": ".hdr",
    "TIFF": ".tif",
    "WEBP": ".webp",
}


def get_blend_filename():
    blend_path = bpy.data.filepath
    if blend_path:
        return os.path.splitext(os.path.basename(blend_path))[0] or "未命名"
    return "未命名"


def sanitize_component(value):
    """清理文件名中的非法字符；保留空字符串，用于模板字面量。"""
    text = "" if value is None else str(value)
    return re.sub(r'[\\/:*?"<>|]+', "_", text).strip()


def sanitize_name(name):
    return sanitize_component(name) or "NoCamera"


def sanitize_filename(name, fallback="未命名"):
    return sanitize_component(name) or fallback


def get_active_camera_name(scene):
    cam = scene.camera
    return sanitize_name(cam.name if cam else None)


def template_has_num(template):
    return "{num" in (template or "")


def _safe_format_template(template, blend_name, render_type, camera_name, num):
    """按模板生成文件名。模板错误时回退到默认模板，避免操作中断。"""
    date_str = datetime.now().strftime("%Y%m%d")
    default_template = "{blend}-{type}-{camera}-{num:03d}"
    values = {
        "blend": sanitize_filename(blend_name, "未命名"),
        "camera": sanitize_name(camera_name),
        "type": sanitize_filename(render_type, "Render"),
        "date": date_str,
        "num": int(num),
    }

    if not template:
        template = default_template

    try:
        filename = template.format(**values)
    except Exception:
        template = default_template
        filename = template.format(**values)

    # 如果用户模板里没有 {num}，仍然自动追加编号，避免同名覆盖。
    if not template_has_num(template):
        filename = f"{filename}-{int(num):03d}"

    return sanitize_filename(filename)


def _compile_number_pattern(template, blend_name, render_type, camera_name):
    """把文件名模板转换成编号扫描正则，用于找出目录中已有的最大编号。"""
    date_str = datetime.now().strftime("%Y%m%d")
    values = {
        "blend": sanitize_filename(blend_name, "未命名"),
        "camera": sanitize_name(camera_name),
        "type": sanitize_filename(render_type, "Render"),
        "date": date_str,
    }

    if not template:
        template = "{blend}-{type}-{camera}-{num:03d}"

    parts = []
    has_num = False

    try:
        parsed = list(_FILENAME_FORMATTER.parse(template))
    except ValueError:
        parsed = list(_FILENAME_FORMATTER.parse("{blend}-{type}-{camera}-{num:03d}"))
        template = "{blend}-{type}-{camera}-{num:03d}"

    for literal_text, field_name, format_spec, conversion in parsed:
        if literal_text:
            parts.append(re.escape(sanitize_component(literal_text)))

        if field_name is None:
            continue

        # 只支持模板说明里列出的简单字段；未知字段按空字符串处理，避免崩溃。
        key = field_name.split(".", 1)[0].split("[", 1)[0]
        if key == "num":
            parts.append(r"(\d+)")
            has_num = True
            continue

        value = values.get(key, "")
        try:
            if conversion == "r":
                value = repr(value)
            elif conversion == "s":
                value = str(value)
            elif conversion == "a":
                value = ascii(value)

            if format_spec:
                rendered = ("{:" + format_spec + "}").format(value)
            else:
                rendered = str(value)
        except Exception:
            rendered = str(value)

        parts.append(re.escape(sanitize_component(rendered)))

    if not has_num:
        # 兼容“没有 {num} 的模板”：实际文件名会自动追加 -001。
        example = _safe_format_template(template, blend_name, render_type, camera_name, 0)
        base = re.sub(r"-0+$", "", example)
        parts = [re.escape(base), r"-(\d+)"]

    return re.compile(r"^" + "".join(parts) + r"(?:\.\w+)?$")


def update_next_number_cache(base_folder, blend_name, render_type, camera_name, prefs):
    """同步扫描目录，得到当前真正可用的下一编号。"""
    global _render_state

    if not os.path.exists(base_folder):
        next_num = 1
    else:
        try:
            files = os.listdir(base_folder)
        except Exception:
            files = []

        max_num = 0
        pattern = _compile_number_pattern(prefs.filename_template, blend_name, render_type, camera_name)

        for f in files:
            match = pattern.match(f)
            if match:
                try:
                    num = int(match.group(1))
                    max_num = max(max_num, num)
                except (ValueError, IndexError):
                    continue

        next_num = max_num + 1

    _render_state.update({
        "cached_next_num": next_num,
        "cached_blend_name": blend_name,
        "cached_camera_name": camera_name,
        "cached_render_type": render_type,
        "needs_update": False,
    })
    return next_num


def get_final_filepath(base_folder, render_type, next_num, camera_name, prefs):
    blend_name = get_blend_filename()
    filename = _safe_format_template(prefs.filename_template, blend_name, render_type, camera_name, next_num)
    return os.path.join(base_folder, filename)


def get_render_file_extension(scene):
    """尽量取得 Blender 实际保存图片时会追加的扩展名。"""
    try:
        ext = getattr(scene.render, "file_extension", "")
        if ext:
            return ext if ext.startswith(".") else f".{ext}"
    except Exception:
        pass

    try:
        file_format = scene.render.image_settings.file_format
        return _IMAGE_FORMAT_EXTENSIONS.get(file_format, "")
    except Exception:
        return ""


def possible_render_filepaths(scene, filepath):
    """返回 Blender 可能实际写入的路径，用于渲染前防覆盖检测。"""
    paths = {filepath}
    try:
        use_file_extension = bool(scene.render.use_file_extension)
    except Exception:
        use_file_extension = True

    ext = get_render_file_extension(scene)
    if use_file_extension and ext:
        if not filepath.lower().endswith(ext.lower()):
            paths.add(filepath + ext)

    return paths


def render_output_exists(scene, filepath):
    return any(os.path.exists(path) for path in possible_render_filepaths(scene, filepath))


def get_collision_free_filepath(base_folder, render_type, camera_name, prefs, scene, start_num=None):
    """最终防线：即使缓存失效，也逐个检查实际输出文件，保证不覆盖已有文件。"""
    if start_num is None:
        start_num = 1

    try:
        next_num = max(1, int(start_num))
    except Exception:
        next_num = 1

    # 足够大的上限，避免极端情况下死循环。
    for num in range(next_num, next_num + 100000):
        candidate = get_final_filepath(base_folder, render_type, num, camera_name, prefs)
        if not render_output_exists(scene, candidate):
            return candidate, num

    raise RuntimeError("无法找到可用编号，请检查输出目录文件数量或文件名模板。")


def ensure_output_dir(base_path):
    os.makedirs(base_path, exist_ok=True)


def get_first_view3d_space(context):
    screen = getattr(context, "screen", None)
    if not screen:
        return None
    for area in screen.areas:
        if area.type == 'VIEW_3D':
            for space in area.spaces:
                if space.type == 'VIEW_3D':
                    return space
    return None


# ====================== ID Pass 设置 ======================
def setup_id_pass_viewport(context):
    global _render_state
    space = get_first_view3d_space(context)
    if not space:
        return

    _render_state["original_settings"] = {
        "shading_type": space.shading.type,
        "shading_light": space.shading.light,
        "show_overlays": space.overlay.show_overlays,
        "show_object_outline": getattr(space.shading, "show_object_outline", False),
        "show_cavity": space.shading.show_cavity,
        "show_shadows": space.shading.show_shadows,
        "use_dof": space.shading.use_dof,
    }

    space.shading.type = 'SOLID'
    space.shading.light = 'FLAT'
    space.overlay.show_overlays = False
    if hasattr(space.shading, "show_object_outline"):
        space.shading.show_object_outline = False
    space.shading.show_cavity = False
    space.shading.show_shadows = False
    space.shading.use_dof = False


def restore_viewport_settings(context):
    global _render_state
    settings = _render_state.get("original_settings", {})
    if not settings:
        return
    space = get_first_view3d_space(context)
    if not space:
        return
    try:
        space.shading.type = settings["shading_type"]
        space.shading.light = settings["shading_light"]
        space.overlay.show_overlays = settings["show_overlays"]
        if hasattr(space.shading, "show_object_outline"):
            space.shading.show_object_outline = settings["show_object_outline"]
        space.shading.show_cavity = settings["show_cavity"]
        space.shading.show_shadows = settings["show_shadows"]
        space.shading.use_dof = settings["use_dof"]
    except Exception:
        pass
    _render_state["original_settings"] = {}


# ====================== 清理与状态 ======================
def mark_ui_dirty():
    _render_state["needs_update"] = True


def cleanup_and_restore(scene=None, context=None):
    global _render_state
    if scene is None:
        scene = bpy.context.scene
    if context is None:
        context = bpy.context

    try:
        if _render_state["original_filepath"]:
            scene.render.filepath = _render_state["original_filepath"]

        if _render_state["is_id_pass"]:
            restore_viewport_settings(context)
    except Exception:
        pass

    _render_state["is_running"] = False
    _render_state["original_filepath"] = ""
    _render_state["is_id_pass"] = False
    mark_ui_dirty()


@persistent
def render_complete_handler(scene):
    global _render_state
    if not _render_state["is_running"]:
        return

    mark_ui_dirty()
    prefs = bpy.context.preferences.addons[__name__].preferences

    if _render_state["render_type"] == "Render" and prefs.auto_open_after_render:
        try:
            subprocess.Popen(['explorer', _render_state["target_dir"]] if sys.platform.startswith("win") else
                             ['open', _render_state["target_dir"]] if sys.platform == "darwin" else
                             ['xdg-open', _render_state["target_dir"]])
        except Exception:
            pass

    bpy.app.timers.register(lambda: cleanup_and_restore(scene, bpy.context), first_interval=0.2)


@persistent
def render_cancel_handler(scene):
    cleanup_and_restore(scene)


@persistent
def load_handler(dummy):
    mark_ui_dirty()


# ====================== 准备函数 ======================
def prepare_render_output(context, render_type, allow_running=False):
    global _render_state
    scene = context.scene

    if _render_state.get("is_running") and not allow_running:
        return None, "已有渲染任务正在进行，请等待完成后再开始。"

    if not bpy.data.filepath:
        return None, "请先保存 .blend 文件！"

    addon = context.preferences.addons.get(__name__)
    if not addon:
        return None, "插件偏好设置读取失败，请重新启用插件。"

    prefs = addon.preferences
    base_path = bpy.path.abspath(prefs.save_path)

    ensure_output_dir(base_path)
    blend_name = get_blend_filename()
    camera_name = get_active_camera_name(scene)

    # 关键修复：渲染前同步扫描目录，不再使用可能过期的后台缓存。
    next_num = update_next_number_cache(base_path, blend_name, render_type, camera_name, prefs)

    # 第二道防线：检查 Blender 实际会写出的文件路径，存在就继续递增。
    try:
        final_path, next_num = get_collision_free_filepath(
            base_path, render_type, camera_name, prefs, scene, start_num=next_num
        )
    except RuntimeError as e:
        return None, str(e)

    _render_state.update({
        "is_running": True,
        "original_filepath": scene.render.filepath,
        "target_filepath": final_path,
        "target_dir": base_path,
        "render_type": render_type,
        "camera_name": camera_name,
        "is_id_pass": (render_type == "IDPass"),
        "cached_next_num": next_num + 1,
        "cached_blend_name": blend_name,
        "cached_camera_name": camera_name,
        "cached_render_type": render_type,
        "needs_update": False,
    })

    scene.render.filepath = final_path
    return {
        "scene": scene,
        "base_path": base_path,
        "final_path": final_path,
        "next_num": next_num,
    }, None


# ====================== 操作符 ======================
class RENDER_OT_auto_save_smart(bpy.types.Operator):
    bl_idname = "render.auto_save_smart"
    bl_label = "开始正式渲染 (F12)"
    bl_description = "使用 F12 快捷键正式渲染并自动保存（带编号）"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        result, error = prepare_render_output(context, "Render")
        if error:
            self.report({'WARNING'}, error)
            return {'CANCELLED'}

        try:
            bpy.ops.render.render('INVOKE_DEFAULT', write_still=True)
            self.report({'INFO'}, f"正在渲染: {os.path.basename(result['final_path'])}")
            return {'FINISHED'}
        except Exception as e:
            cleanup_and_restore(result["scene"], context)
            self.report({'ERROR'}, f"渲染启动失败: {e}")
            return {'CANCELLED'}


class RENDER_OT_auto_save_viewport(bpy.types.Operator):
    bl_idname = "render.auto_save_viewport"
    bl_label = "渲染视图预览"
    bl_description = "快速渲染当前视口并自动保存"

    def execute(self, context):
        result, error = prepare_render_output(context, "Viewport")
        if error:
            self.report({'WARNING'}, error)
            return {'CANCELLED'}

        try:
            bpy.ops.render.opengl(write_still=True, view_context=True)
            self.report({'INFO'}, f"Viewport 已保存: {os.path.basename(result['final_path'])}")
            return {'FINISHED'}
        except Exception as e:
            self.report({'ERROR'}, f"视口渲染失败: {e}")
            return {'CANCELLED'}
        finally:
            cleanup_and_restore(result["scene"], context)


class RENDER_OT_auto_save_id_pass(bpy.types.Operator):
    bl_idname = "render.auto_save_id_pass"
    bl_label = "渲染 ID Pass"
    bl_description = "以纯色 ID 模式渲染并保存"

    def execute(self, context):
        result, error = prepare_render_output(context, "IDPass")
        if error:
            self.report({'WARNING'}, error)
            return {'CANCELLED'}

        try:
            setup_id_pass_viewport(context)
            bpy.ops.render.opengl(write_still=True, view_context=True)
            self.report({'INFO'}, f"ID Pass 已保存: {os.path.basename(result['final_path'])}")
            return {'FINISHED'}
        except Exception as e:
            self.report({'ERROR'}, f"ID Pass 渲染失败: {e}")
            return {'CANCELLED'}
        finally:
            cleanup_and_restore(result["scene"], context)


class RENDER_OT_batch_render_cameras(bpy.types.Operator):
    bl_idname = "render.auto_save_batch_cameras"
    bl_label = "批量渲染选中相机"
    bl_description = "为当前选中的所有相机分别渲染一次"

    def execute(self, context):
        original_cam = context.scene.camera
        count = 0
        errors = []

        try:
            for obj in context.selected_objects:
                if obj.type != 'CAMERA':
                    continue

                context.scene.camera = obj
                result, error = prepare_render_output(context, "Render", allow_running=True)
                if error:
                    errors.append(f"{obj.name}: {error}")
                    continue

                try:
                    bpy.ops.render.render(write_still=True)
                    count += 1
                except Exception as e:
                    errors.append(f"{obj.name}: {e}")
                finally:
                    # 批量渲染是连续同步操作，必须每个相机后立即恢复状态，避免下一轮继承旧 filepath。
                    cleanup_and_restore(result["scene"], context)
        finally:
            context.scene.camera = original_cam
            cleanup_and_restore(context.scene, context)

        if errors:
            self.report({'WARNING'}, f"已渲染 {count} 个相机，{len(errors)} 个失败")
        else:
            self.report({'INFO'}, f"已批量渲染 {count} 个相机")
        return {'FINISHED'}


class RENDER_OT_refresh_cache(bpy.types.Operator):
    bl_idname = "render.auto_save_refresh"
    bl_label = "刷新编号缓存"
    bl_description = "立即重新扫描目录并更新下一编号"

    def execute(self, context):
        mark_ui_dirty()
        self.report({'INFO'}, "编号缓存已刷新")
        return {'FINISHED'}


class RENDER_OT_open_output_dir(bpy.types.Operator):
    bl_idname = "render.auto_save_open_output_dir"
    bl_label = "打开输出目录"
    bl_description = "在文件管理器中打开当前保存文件夹"

    def execute(self, context):
        prefs = context.preferences.addons[__name__].preferences
        base_path = bpy.path.abspath(prefs.save_path)
        ensure_output_dir(base_path)

        try:
            if sys.platform.startswith("win"):
                os.startfile(base_path)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", base_path])
            else:
                subprocess.Popen(["xdg-open", base_path])
            return {'FINISHED'}
        except Exception as e:
            self.report({'ERROR'}, f"打开目录失败: {e}")
            return {'CANCELLED'}


# ====================== UI 面板 ======================
class VIEW3D_PT_auto_render_smart_ui(bpy.types.Panel):
    bl_label = "渲染自动保存"
    bl_idname = "VIEW3D_PT_auto_render_smart_ui"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "渲染自动保存"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        global _render_state
        prefs = context.preferences.addons[__name__].preferences
        scene = context.scene
        blend_name = get_blend_filename()
        camera_name = get_active_camera_name(scene)
        base_path = bpy.path.abspath(prefs.save_path)

        # 输出设置
        box = layout.box()
        box.label(text="输出设置", icon='FILE_FOLDER')
        box.prop(prefs, "save_path")
        row = box.row(align=True)
        row.operator("render.auto_save_open_output_dir", icon='FILE_FOLDER')
        row.operator("render.auto_save_refresh", icon='FILE_REFRESH')

        # 当前信息
        box = layout.box()
        box.label(text="当前信息", icon='INFO')
        box.label(text=f"项目: {blend_name}", icon='FILE_BLEND')
        box.label(text=f"相机: {camera_name}", icon='CAMERA_DATA')

        if not bpy.data.filepath:
            warn = layout.box()
            warn.alert = True
            warn.label(text="请先保存 .blend 文件", icon='ERROR')
            return

        # 命名预览
        preview_box = layout.box()
        preview_box.label(text="命名预览", icon='IMAGE_DATA')
        for rtype, icon in [("Render", 'RENDER_STILL'), ("Viewport", 'SHADING_RENDERED'), ("IDPass", 'RENDERLAYERS')]:
            num = update_next_number_cache(base_path, blend_name, rtype, camera_name, prefs)
            try:
                preview_path, preview_num = get_collision_free_filepath(
                    base_path, rtype, camera_name, prefs, scene, start_num=num
                )
                preview_name = os.path.basename(preview_path)
            except Exception:
                preview_name = os.path.basename(get_final_filepath(base_path, rtype, num, camera_name, prefs))
            preview_box.label(text=preview_name, icon=icon)

        # 状态
        status_box = layout.box()
        status_box.label(text="状态", icon='INFO')
        if _render_state["is_running"]:
            status_box.alert = True
            status_box.label(text="渲染进行中...", icon='RENDER_STILL')
        else:
            status_box.label(text="准备就绪 ✓", icon='CHECKMARK')

        # 操作按钮
        box = layout.box()
        box.label(text="正式输出", icon='RENDER_STILL')
        col = box.column(align=True)
        col.scale_y = 1.4
        col.operator("render.auto_save_smart", icon='RENDER_STILL')

        box = layout.box()
        box.label(text="快速预览 / 通道", icon='SHADING_RENDERED')
        col = box.column(align=True)
        col.scale_y = 1.25
        col.operator("render.auto_save_viewport", icon='SHADING_RENDERED')
        col.operator("render.auto_save_id_pass", icon='RENDERLAYERS')

        # 新增批量
        row = layout.row()
        row.operator("render.auto_save_batch_cameras", icon='CAMERA_DATA')


# ====================== 注册 ======================
classes = (
    AutoRenderAddonPreferences,
    RENDER_OT_auto_save_smart,
    RENDER_OT_auto_save_viewport,
    RENDER_OT_auto_save_id_pass,
    RENDER_OT_batch_render_cameras,
    RENDER_OT_refresh_cache,
    RENDER_OT_open_output_dir,
    VIEW3D_PT_auto_render_smart_ui,
)


def register_keymaps():
    global addon_keymaps
    kc = bpy.context.window_manager.keyconfigs.addon
    if not kc:
        return
    km = kc.keymaps.new(name='3D View', space_type='VIEW_3D')
    kmi = km.keymap_items.new("render.auto_save_smart", type='F12', value='PRESS')
    addon_keymaps.append((km, kmi))


def unregister_keymaps():
    global addon_keymaps
    for km, kmi in addon_keymaps[:]:
        try:
            km.keymap_items.remove(kmi)
        except Exception:
            pass
    addon_keymaps.clear()


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    register_keymaps()

    handlers = bpy.app.handlers
    if render_complete_handler not in handlers.render_complete:
        handlers.render_complete.append(render_complete_handler)
    if render_cancel_handler not in handlers.render_cancel:
        handlers.render_cancel.append(render_cancel_handler)
    if load_handler not in handlers.load_post:
        handlers.load_post.append(load_handler)


def unregister():
    unregister_keymaps()

    handlers = bpy.app.handlers
    for h in (render_complete_handler, render_cancel_handler, load_handler):
        if h in handlers.render_complete:
            handlers.render_complete.remove(h)
        if h in handlers.render_cancel:
            handlers.render_cancel.remove(h)
        if h in handlers.load_post:
            handlers.load_post.remove(h)

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
