"""Demo server: python3 app.py, then open http://localhost:8000"""
import json
import random
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from evaluate import ERRORS, mutate
from runner import Model, load, quantities, split_steps

PROTOCOLS = load()
MODEL = Model(PROTOCOLS)
PAGE = Path(__file__).with_name("index.html")


def steps_of(text):
    s = split_steps(text)
    return s if len(s) > 1 else [l.strip() for l in text.splitlines() if l.strip()]


def example():
    """A real protocol from the dataset with one slip inserted. The model is rebuilt without
    that protocol so it can't just remember the original value."""
    rng = random.Random()
    while True:
        p = rng.choice(PROTOCOLS)
        steps = list(p["steps"])
        i = rng.randrange(len(steps))
        qs = quantities(steps[i])
        if not qs:
            continue
        err = rng.choice(ERRORS)
        mut = mutate(steps[i], rng.choice(qs), err)
        if mut:
            steps[i] = mut[0]
            return p, steps, i + 1, err


class Handler(BaseHTTPRequestHandler):
    def send(self, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/example":
            p, steps, step_no, err = example()
            model = Model([q for q in PROTOCOLS if q["id"] != p["id"]])
            self.send(json.dumps({"title": p["title"], "url": p["url"], "inserted": {"step": step_no, "error": err},
                                  "text": "\n".join(f"{n}. {s}" for n, s in enumerate(steps, 1)),
                                  "trace": model.run(steps)}))
        else:
            self.send(PAGE.read_bytes(), "text/html; charset=utf-8")

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        text = json.loads(self.rfile.read(min(n, 200_000)) or b"{}").get("text", "")
        self.send(json.dumps({"trace": MODEL.run(steps_of(text))}))


if __name__ == "__main__":
    print("http://localhost:8000")
    ThreadingHTTPServer(("127.0.0.1", 8000), Handler).serve_forever()
