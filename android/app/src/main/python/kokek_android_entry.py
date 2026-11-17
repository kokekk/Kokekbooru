import os
import webbrowser

def start_server(app_files_dir: str):
    os.environ["HOME"] = app_files_dir
    os.environ["KOKEK_ANDROID"] = "1"
    webbrowser.open = lambda *a, **kw: True
    import uvicorn
    import kokekbooru
    uvicorn.run(kokekbooru.app, host="127.0.0.1", port=8765, log_level="warning")
