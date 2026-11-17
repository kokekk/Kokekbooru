import importlib.machinery
import os
import sys
import types
import webbrowser
import numpy as np


def _install_android_onnxruntime_shim():
    existing = sys.modules.get("onnxruntime")
    if existing is not None and getattr(existing, "_is_kokek_android_shim", False):
        return existing

    ort_mod = types.ModuleType("onnxruntime")
    ort_mod.__spec__ = importlib.machinery.ModuleSpec("onnxruntime", loader=None)
    ort_mod.__version__ = "1.22.0-android"
    ort_mod._is_kokek_android_shim = True

    class _NodeArg:
        def __init__(self, name: str = "input_1:0"):
            self.name = name

    class SessionOptions:
        def __init__(self):
            self.graph_optimization_level = 99
            self.intra_op_num_threads = 4
            self.inter_op_num_threads = 1
            self.enable_mem_pattern = True
            self.execution_mode = 0

    class GraphOptimizationLevel:
        ORT_DISABLE_ALL = 0
        ORT_ENABLE_BASIC = 1
        ORT_ENABLE_EXTENDED = 2
        ORT_ENABLE_ALL = 99

    class ExecutionMode:
        ORT_SEQUENTIAL = 0
        ORT_PARALLEL = 1

    class InferenceSession:
        def __init__(self, path_or_bytes, sess_options=None, providers=None, **kwargs):
            from java import jclass
            self._bridge = jclass("com.kokekk.kokekbooru.OnnxBridge")
            use_nnapi = True
            if providers and "CPUExecutionProvider" in providers and len(providers) == 1:
                use_nnapi = False
            self.provider = str(self._bridge.loadModel(str(path_or_bytes), bool(use_nnapi)))

        def get_inputs(self):
            return [_NodeArg("input_1:0")]

        def get_providers(self):
            return [self.provider]

        def run(self, output_names, input_feed, run_options=None):
            inp_tensor = next(iter(input_feed.values()))
            arr = np.ascontiguousarray(inp_tensor, dtype=np.float32)
            batch_size = int(arr.shape[0]) if arr.ndim == 4 else 1
            flat_arr = np.ascontiguousarray(arr.reshape(-1), dtype=np.float32)
            try:
                from java import jarray, jfloat
                j_input = jarray(jfloat)(flat_arr)
            except Exception:
                j_input = flat_arr
            raw_out = self._bridge.runBatch(j_input, batch_size)
            out_np = np.array(raw_out, dtype=np.float32)
            if batch_size > 0 and out_np.size > 0:
                out_np = out_np.reshape(batch_size, -1)
            else:
                out_np = np.zeros((batch_size, 0), dtype=np.float32)
            return [out_np]

    ort_mod.SessionOptions = SessionOptions
    ort_mod.GraphOptimizationLevel = GraphOptimizationLevel
    ort_mod.ExecutionMode = ExecutionMode
    ort_mod.InferenceSession = InferenceSession
    ort_mod.get_available_providers = lambda: ["NnapiExecutionProvider", "CPUExecutionProvider"]
    ort_mod.get_device = lambda: "NPU/CPU"

    sys.modules["onnxruntime"] = ort_mod
    return ort_mod


def start_server(app_files_dir: str):
    os.environ["HOME"] = app_files_dir
    os.environ["KOKEK_ANDROID"] = "1"
    webbrowser.open = lambda *a, **kw: True
    _install_android_onnxruntime_shim()
    import uvicorn
    import kokekbooru
    uvicorn.run(kokekbooru.app, host="127.0.0.1", port=8765, log_level="warning")
