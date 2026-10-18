import threading, os, webbrowser
from newtonite import app

if __name__ == "__main__":
    app.seed()
    threading.Thread(target=app.worker, daemon=True).start()
    port = int(os.environ.get("PORT", 8000))
    url = f"http://localhost:{port}"
    print(f"Newtonite Ops running at {url}")
    threading.Timer(1, lambda: webbrowser.open(url)).start()
    app.ThreadingHTTPServer(("", port), app.H).serve_forever()