import bmesh
import bpy
import queue
import sys
import traceback
from threading import Thread

from ..generator.generator import Generator


class MESHGEN_OT_GenerateMesh(bpy.types.Operator):
    bl_idname = "meshgen.generate_mesh"
    bl_label = "Generate Mesh"
    bl_options = {"REGISTER", "INTERNAL"}

    def execute(self, context):
        from transformers import StoppingCriteria, TextIteratorStreamer

        props = context.scene.meshgen_props
        props.cancelled = False
        props.vertices_generated = 0
        props.faces_generated = 0

        self.mesh_data = bpy.data.meshes.new("GeneratedMesh")
        self.mesh_obj = bpy.data.objects.new("GeneratedMesh", self.mesh_data)
        context.collection.objects.link(self.mesh_obj)

        self.bmesh = bmesh.new()

        generator = Generator.instance()
        conversation = [
            {"role": "user", "content": props.prompt}
        ]
        input_ids = generator.tokenizer.apply_chat_template(conversation, return_tensors="pt").to(
            generator.device
        )
        self.generated_text = ""
        self.line_buffer = ""

        class CancelStoppingCriteria(StoppingCriteria):
            def __call__(self, input_ids, scores, **kwargs):
                return props.cancelled
            
        stopping_criteria = CancelStoppingCriteria()
        self._streamer = TextIteratorStreamer(generator.tokenizer, timeout=10.0, skip_prompt=True, skip_special_tokens=True)

        kwargs = dict(
            input_ids=input_ids,
            streamer=self._streamer,
            do_sample=False,
            temperature=props.temperature,
            max_new_tokens=props.max_new_tokens,
            eos_token_id=generator.terminators,
            stopping_criteria=[stopping_criteria],
        )

        def generation_thread():
            generator.pipeline.generate(**kwargs)

        self._thread = Thread(target=generation_thread)
        props.is_running = True
        self._thread.start()

        self._timer = context.window_manager.event_timer_add(0.1, window=context.window)
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def redraw(self, context):
        for area in context.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()
    
    def modal(self, context, event):
        props = context.scene.meshgen_props
        
        if event.type == "TIMER":
            new_tokens = False
            try:
                while True:
                    next_token = self._streamer.text_queue.get_nowait()
                    if next_token is None:
                        break
                    self.generated_text += next_token
                    self.line_buffer += next_token
                    props.generated_text = self.generated_text
                    new_tokens = True

                    if "\n" in self.line_buffer:
                        lines = self.line_buffer.split("\n")
                        for line in lines[:-1]:
                            self.process_line(line.strip(), context)
                        self.line_buffer = lines[-1]
            except queue.Empty:
                pass
            except Exception as e:
                print(e, file=sys.stderr)
                traceback.print_exc()
                props.is_running = False
                context.window_manager.event_timer_remove(self._timer)
                self.bmesh.free()
                return {"CANCELLED"}

            if new_tokens:
                self.redraw(context)
        
            if not self._thread.is_alive() and self._streamer.text_queue.empty():
                if self.line_buffer:
                    self.process_line(self.line_buffer.strip(), context)
                self.update_mesh(context)
                props.is_running = False
                context.window_manager.event_timer_remove(self._timer)
                self.bmesh.free()
                self.redraw(context)
                return {"FINISHED"}
    
        if props.cancelled:
            props.is_running = False
            context.window_manager.event_timer_remove(self._timer)
            self.bmesh.free()
            self.redraw(context)
            return {"CANCELLED"}
        
        return {"PASS_THROUGH"}

    def process_line(self, line, context):
        print(line)
        if line.startswith("v "):
            parts = line.split()
            if len(parts) == 4:
                try:
                    x, y, z = map(float, parts[1:])
                    self.add_vertex(x, y, z, context)
                except ValueError:
                    print(f"Invalid vertex line: {line}", file=sys.stderr)
                    traceback.print_exc()
        elif line.startswith("f "):
            parts = line.split()
            if len(parts) == 4:
                try:
                    a, b, c = map(int, parts[1:])
                    self.add_face(a, b, c, context)
                except ValueError:
                    print(f"Invalid face line: {line}", file=sys.stderr)
                    traceback.print_exc()

    def add_vertex(self, x, y, z, context):
        self.bmesh.verts.new((x / 64.0, y / 64.0, z / 64.0))
        self.bmesh.verts.ensure_lookup_table()
        context.scene.meshgen_props.vertices_generated += 1
        self.update_mesh(context)

    def add_face(self, a, b, c, context):
        try:
            self.bmesh.faces.new((self.bmesh.verts[a - 1], self.bmesh.verts[b - 1], self.bmesh.verts[c - 1]))
            self.bmesh.faces.ensure_lookup_table()
            context.scene.meshgen_props.faces_generated += 1
            self.update_mesh(context)
        except IndexError:
            print(f"Indices out of range: {a}, {b}, {c}", file=sys.stderr)
        except ValueError:
            print(f"Face already exists: {a}, {b}, {c}", file=sys.stderr)

    def update_mesh(self, context):
        self.bmesh.to_mesh(self.mesh_data)
        self.mesh_data.update()


class MESHGEN_OT_CancelGeneration(bpy.types.Operator):
    bl_idname = "meshgen.cancel_generation"
    bl_label = "Cancel Generation"
    bl_options = {"REGISTER", "INTERNAL"}

    def execute(self, context):
        props = context.scene.meshgen_props
        props.cancelled = True
        return {"FINISHED"}
