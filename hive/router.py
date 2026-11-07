"""Local decision-model router (routing step 1, plan section "Router").

The router picks a worker tier with the Open Decision Model 150M ONNX export
(`gparitosh007/Open-Decision-Model-150M-ONNX`, file `model_fp16.onnx`). The model
runs locally with ONNX Runtime. There is no HTTP endpoint and no network call at
run time. The model files download one time with `hive router setup`.

ONNX graph (inspected with onnxruntime, ort.InferenceSession.get_*_inputs):

    IN  input_ids       tensor(int64) [batch, seq]
    IN  attention_mask  tensor(int64) [batch, seq]
    IN  option_mask     tensor(float) [batch, seq, options]
    OUT logits          tensor(float) [batch, options]

The graph does the span mean-pooling itself: `option_mask[b, t, o] = 1` marks the
tokens of option `o`, and the graph returns one head logit per option from the
mean-pooled span vectors. Python code therefore needs only the masks and the
softmax over `logits / temperature`. The temperature comes from
`decision_config.json` (`temperature: 3.986253499984741`).

Input layout follows the reference quickstart of the PyTorch model card
(gparitosh007/Open-Decision-Model-150M, section "Quickstart"):

    [CLS] "k: v" (preferences) [SEP] state [SEP] instructions [SEP]
    "label: desc" (option) [SEP] "label: desc" [SEP] ...

CLS is token id 50281, SEP is token id 50282 (ModernBERT ids from the
transformers tokenizer). Options stay whole; the state is trimmed from the start
when the full sequence exceeds 1024 tokens. A span covers the option tokens only
(not the [SEP] after them).

Serving file: the repo ships `model_fp16.onnx`. Two backends pass both targets
in the real `hive router serve` process at intra-op threads 2 (p50 < 200 ms,
RSS < 1 GB): fp16 (README 285 MiB, VmRSS 620 MB, p50 138 ms, p95 152 ms) and
fp32 (VmRSS 942 MB, p50 162 ms). Dynamic int8 quantization produces an invalid
graph for this model. fp16 wins on latency and RSS, so `serve` loads
`model_fp16.onnx`; the fp32 copy is a local fallback (`model_fp32.onnx`).
`$HIVE_ROUTER_MODEL_FILE` overrides the pick.

Accuracy check: `VERIFY.json` is a summary file (`max_abs_prob_diff: 0.000277`,
`pass: true`) with no per-record probabilities, so the test uses the worked
example from the model card ("Quickstart"): state "Refactor the auth module and
add tests. The repo has 40 files.", preferences cost = "low cost before speed",
haiku/sonnet/opus options -> 0.16 / 0.50 / 0.34. The test skips when the model
files are missing.

Server: `hive router serve` loads the model one time and serves JSON on the Unix
socket `$HIVE_STATE_DIR/router.sock` (mode 0600). The request shape is the same
`/v1/decide` JSON as the model card (`state`, `preferences`, `questions`). The
server exits after 30 idle minutes (`IDLE_EXIT_S`, configurable with
`$HIVE_ROUTER_IDLE_EXIT_S`). `hive router ask --task-file F` is the client: it
starts the server in the background when the socket is missing.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

from . import state

HF_BASE = "https://huggingface.co/gparitosh007/Open-Decision-Model-150M-ONNX/resolve/main/"
MODEL_FILES = ("model_fp16.onnx", "tokenizer.json", "decision_config.json", "VERIFY.json")
CACHE_DIR = Path(os.environ.get("HIVE_DECISION_MODEL_DIR", "~/.cache/herdr-hive/decision-model")).expanduser()
IDLE_EXIT_S = float(os.environ.get("HIVE_ROUTER_IDLE_EXIT_S", "1800"))
MAX_LEN = 1024
CLS = 50281
SEP = 50282
REQ_TIMEOUT_S = 2.0

TIER_OPTIONS = [  # (label, description); text tuned in step 2b, same string as hive/config.py
    ("fast", "Small mechanical edit: one or two files, obvious and low risk, no tests needed. "
             "Examples: fix a typo, delete an unused import, rename in one file, change one message."),
    ("regular", "Normal feature or bug fix: a few files and tests, clear goal, regular patterns. "
                "Examples: add a CLI flag end to end, fix an off-by-one with a regression test."),
    ("think", "Hard or unclear work: design, unclear bugs, concurrency, performance, many-file refactor. "
              "Examples: fix a flaky test with no local repro, design a plugin API, find a memory leak."),
]
TIER_QUESTION = "Which worker tier must handle this task?"


class RouterError(Exception):
    """A router problem the caller must show or fall back on."""


# --- setup: one-time download ------------------------------------------------------------------

def download(dest: Path | None = None) -> list[str]:
    """Download the model files with stdlib urllib. Skip files that exist."""
    dest = dest or CACHE_DIR
    dest.mkdir(parents=True, exist_ok=True)
    done = []
    for name in MODEL_FILES:
        target = dest / name
        if target.exists() and target.stat().st_size > 0:
            print(f"router: skip {name} (exists)")
            continue
        req = urllib.request.Request(HF_BASE + name, headers={"User-Agent": "herdr-hive-router/1.0"})
        tmp = target.with_name(name + ".part")
        with urllib.request.urlopen(req) as r, open(tmp, "wb") as out:
            total = int(r.headers.get("Content-Length", 0))
            got = 0
            last = 0.0
            start = time.monotonic()
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
                got += len(chunk)
                now = time.monotonic()
                if now - last > 0.5:
                    last = now
                    speed = got / max(now - start, 1e-6) / 1e6
                    if total > 0:
                        print(f"\rrouter: {name} {got * 100 / max(total, 1):5.1f}% ({speed:.1f} MB/s)", end="",
                              flush=True)
                    else:
                        print(f"\rrouter: {name} {got >> 20} MiB ({speed:.1f} MB/s)", end="", flush=True)
        tmp.rename(target)
        print(f"\rrouter: {name} done ({target.stat().st_size >> 20} MiB)")
        done.append(name)
    return done


MODEL_CHOICES = ("model_fp16.onnx", "model_fp32.onnx", "model_int8.onnx")


def pick_model_file(cache_dir: Path | None = None) -> str:
    """Pick the model file to load.

    Measured on this machine (x86, ONNX Runtime CPU, intra_op threads 2, one
    server process): fp16 VmRSS 620 MB p50 138 ms; fp32 VmRSS 942 MB p50
    162 ms; int8 fails to load for this graph. fp16 wins, so it comes first.
    """
    cache_dir = Path(cache_dir or CACHE_DIR)
    for name in MODEL_CHOICES:
        if (cache_dir / name).exists():
            return name
    raise RouterError(f"no model file in {cache_dir} (run `hive router setup`)")


def deps_missing() -> bool:
    for mod in ("onnxruntime", "tokenizers", "numpy"):
        try:
            __import__(mod)
        except ImportError:
            return True
    return False


def install_deps() -> None:
    """Install the router extras into the running python (one-time setup)."""
    try:
        import onnxruntime  # noqa: F401
        import tokenizers  # noqa: F401
        import numpy  # noqa: F401
        return
    except ImportError:
        pass
    pkgs = ["onnxruntime", "tokenizers", "numpy"]
    if shutil.which("uv"):
        cmd = ["uv", "pip", "install", "--python", sys.executable, *pkgs]
    else:
        cmd = [sys.executable, "-m", "pip", "install", *pkgs]
    print(f"router: installing deps: {' '.join(pkgs)}")
    raise SystemExit(subprocess.run(cmd).returncode or _reexec_setup())


def _reexec_setup() -> int:  # the freshly installed deps appear only in a new process
    code = subprocess.run([sys.executable, "-m", "hive.cli", "router", "setup"], cwd=_pkg_root()).returncode
    print("router: setup done" if code == 0 else "router: setup failed")
    return code


def _pkg_root() -> Path:
    return Path(__file__).resolve().parent.parent


# --- model --------------------------------------------------------------------------------------

def _softmax(z):
    import numpy as np
    z = z - z.max(-1, keepdims=True)
    e = np.exp(z)
    return e / e.sum(-1, keepdims=True)


def _confidence(probs):
    """1 minus the normalized entropy of the distribution (model card)."""
    import math
    n = len(probs)
    if n <= 1:
        return 0.0
    h = -sum(p * math.log2(max(p, 1e-12)) for p in probs) / math.log2(n)
    return 1.0 - h


class DecisionModel:
    """Local ONNX decision model. Loads one time, then answers in ms."""

    def __init__(self, cache_dir: Path | None = None, intra_op_num_threads: int = 2,
                 model_file: str | None = None):
        import onnxruntime as ort
        from tokenizers import Tokenizer
        import numpy as np
        self._numpy = np
        self.cache_dir = Path(cache_dir or CACHE_DIR)
        self.model_file = model_file or os.environ.get("HIVE_ROUTER_MODEL_FILE") or pick_model_file(self.cache_dir)
        model_path = self.cache_dir / self.model_file
        tok_path = self.cache_dir / "tokenizer.json"
        cfg_path = self.cache_dir / "decision_config.json"
        for p in (model_path, tok_path, cfg_path):
            if not p.exists():
                raise RouterError(f"current model file missing: {p} (run `hive router setup`)")
        cfg = json.loads(cfg_path.read_text())
        self.temperature = float(cfg["temperature"])
        self.version = cfg.get("model_version", "decision-model onnx")
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = intra_op_num_threads
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        opts.log_severity_level = 3
        self.session = ort.InferenceSession(str(model_path), opts, providers=["CPUExecutionProvider"])
        self.tokenizer = Tokenizer.from_file(str(tok_path))
        self.max_len = MAX_LEN

    # -- encoding (reference quickstart layout) ---------------------------------------------

    def encode(self, state_text, instructions, options, preferences=None):
        """Build the token sequence and the option spans.

        `options` is a list of (label, description). Returns (ids, spans, n_opts).
        The state is trimmed from the start; instructions and options stay.
        """
        def bare(text):
            if not text:
                return []
            return self.tokenizer.encode(text, add_special_tokens=False).ids

        pref_ids = bare("\n".join(f"{k}: {v}" for k, v in preferences.items())) if preferences else []
        ids = [CLS]
        if pref_ids:
            ids += pref_ids + [SEP]
        state_ids = bare(state_text)
        instr_ids = bare(instructions)
        opt_ids = [bare(f"{label}: {desc}" if desc else label) for label, desc in options]

        fixed = 1 + (len(pref_ids) + 1 if pref_ids else 0) + 1 + len(instr_ids) + 1 \
            + sum(len(o) + 1 for o in opt_ids)
        room = MAX_LEN - fixed
        if room < 16:
            raise RouterError("instructions and options exceed the 1024-token limit")
        if len(state_ids) > room:
            state_ids = state_ids[len(state_ids) - room:]

        ids += state_ids + [SEP] if state_ids else []
        ids += instr_ids + [SEP]
        spans = []
        for o in opt_ids:
            spans.append((len(ids), len(ids) + len(o)))
            ids += o + [SEP]
        assert len(ids) <= MAX_LEN, "sequence too long"
        return ids, spans, len(options)

    # -- inference ---------------------------------------------------------------------------

    def _logits(self, ids, spans, n_opts):
        np = self._numpy
        n = len(ids)
        om = np.zeros((1, n, n_opts), dtype=np.float32)
        for i, (s, t) in enumerate(spans):
            if t > s:
                om[0, s:t, i] = 1.0
        feeds = {
            "input_ids": np.array([ids], dtype=np.int64),
            "attention_mask": np.ones((1, n), dtype=np.int64),
            "option_mask": om,
        }
        (logits,) = self.session.run(["logits"], feeds)
        return logits[0].astype(np.float64)

    def choice(self, state_text, instructions, options, preferences=None):
        """Answer a choice question.

        `options` is a list of (label, description). Returns (probs, logits)
        where `probs` is a dict label -> probability.
        """
        ids, spans, n_opts = self.encode(state_text, instructions, options, preferences)
        probs = _softmax(self._logits(ids, spans, n_opts) / self.temperature)
        return {label: float(p) for (label, _), p in zip(options, probs)}, probs

    def decide(self, request):
        """Serve the /v1/decide JSON shape. Returns the response dict."""
        opts = None
        answers = []
        for question in request.get("questions", []):
            q_type = question.get("type", "choice")
            preferences = request.get("preferences") or None
            if q_type == "choice":
                opts = [(str(label), str(desc)) for label, desc in question["criteria"].items()]
            elif q_type == "noul":
                opts = [("yes", ""), ("no", "")]
            elif q_type == "score":
                opts = [(str(level), "") for level in (question.get("levels") or question.get("criteria") or [])]
            else:
                raise RouterError(f"unknown question type: {q_type}")
            _, rows = self.choice(request.get("state", ""), question.get("instructions", ""),
                                  opts, preferences)
            probs_dict = {label: float(p) for (label, _), p in zip(opts, rows)}
            answer = {"id": question.get("id", "q"), "type": q_type, "probs": probs_dict,
                      "confidence": round(_confidence(list(probs_dict.values())), 4)}
            if q_type == "noul":
                answer["p_yes"] = probs_dict["yes"]
            if q_type == "score":
                answer["expected_level"] = round(sum(i * p for i, p in enumerate(probs_dict.values())), 4)
                answer["legend"] = {str(i): label for i, (label, _) in enumerate(opts)}
            answers.append(answer)
        return {"decision_id": str(uuid.uuid4()), "model_version": self.version, "answers": answers}


def get_model():
    """Load the model or raise RouterError with a fallback hint."""
    if deps_missing():
        raise RouterError("router deps missing (run `hive router setup`)")
    if not any((CACHE_DIR / name).exists() for name in MODEL_CHOICES):
        raise RouterError(f"model files missing in {CACHE_DIR} (run `hive router setup`)")
    try:
        return DecisionModel()
    except Exception as e:  # onnxruntime load failure means no predictions
        raise RouterError(f"router load failed: {e}") from e


# --- task -> decision input (plan section "Router", point 1) --------------------------------------

SIGNAL_WORDS = (  # state-feature keywords found in the tuning (step 2b)
    ("refactor", "refactor"), ("design", "design"), ("architecture", "design"),
    ("interface", "design"), ("protocol", "design"), ("migration", "design"),
    ("race", "concurrency"), ("deadlock", "concurrency"), ("lock", "concurrency"),
    ("flaky", "hard-debug"), ("memory", "hard-debug"), ("leak", "hard-debug"),
    ("perf", "perf"), ("latency", "perf"), ("slow", "perf"), ("spike", "perf"),
    ("unclear", "unclear"), ("repro", "unclear"), ("investigat", "unclear"),
    ("typo", "tiny"), ("docstring", "tiny"), ("comment", "tiny"),
    ("README", "tiny"), ("import", "tiny"), ("rename", "tiny"), ("one file", "tiny"),
)


def task_signals(task_text: str) -> list[str]:
    """Keyword hints from the task text for the state features."""
    hits, low = [], (task_text or "").lower()
    for word, tag in SIGNAL_WORDS:
        if word.lower() in low and tag not in hits:
            hits.append(tag)
    return hits


def task_question(task_text: str, title: str | None = None, file_count: int = 0,
                  criteria: list[tuple[str, str]] | None = None):
    """Build the /v1/decide request for tier routing.

    `criteria` is the option list (label, description) from config; TIER_OPTIONS is the fallback.

    State text (tuned in step 2b): task text, then the task length, then the
    keyword signals, then the repo file count."""
    trimmed = (task_text or "")[:2000].strip()
    title_text = (title or "").strip()
    state_text = title_text + ". " if title_text else ""
    state_text += trimmed
    if trimmed:
        state_text += f" The task text is about {len(trimmed)} characters long."
        sig = task_signals(task_text)
        if sig:
            state_text += " Signals in the text: " + ", ".join(sig) + "."
    if file_count:
        state_text += f" The repo has {file_count} files."
    return {
        "state": state_text.strip(),
        "preferences": {"cost": "low cost before speed"},
        "questions": [{"id": "tier", "type": "choice", "instructions": TIER_QUESTION,
                       "criteria": {label: desc for label, desc in (criteria or TIER_OPTIONS)}}],
    }


def tier_criteria(cfg) -> list[tuple[str, str]]:
    """Option list from the config tiers (label = tier name)."""
    return [(t.name, t.description) for t in cfg.tiers.values()]


# --- server ---------------------------------------------------------------------------------------

def sock_path() -> Path:
    p = state.STATE_DIR / "router.sock"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def serve():
    """Load the model one time and serve JSON on the Unix socket until idle."""
    try:
        model = get_model()
    except RouterError as e:
        print(f"router: not serving: {e}", file=sys.stderr)
        return 1
    import socket
    path = sock_path()
    if path.exists():
        path.unlink()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    os.chmod(path, 0o600)
    server.listen(4)
    idle_s = float(os.environ.get("HIVE_ROUTER_IDLE_EXIT_S", IDLE_EXIT_S))
    print(f"router: serving {model.model_file} on {path} (model {model.version}, "
          f"idle exit {idle_s / 60:.0f} min)")
    while True:
        server.settimeout(idle_s)
        try:
            conn, _ = server.accept()
        except socket.timeout:
            print("router: idle timeout, exiting")
            break
        except OSError:
            break
        with conn:
            try:
                conn.settimeout(30.0)
                data = b""
                while b"\n" not in data and len(data) < 10 << 20:
                    chunk = conn.recv(1 << 16)
                    if not chunk:
                        break
                    data += chunk
                if not data.strip():
                    continue
                request = json.loads(data)
                answer = model.decide(request)
                conn.sendall(json.dumps(answer).encode())
            except Exception as e:
                try:
                    conn.sendall(json.dumps({"error": str(e)}).encode())
                except OSError:
                    pass
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    return 0


# --- client -------------------------------------------------------------------------------------------

def ask_server(request, timeout: float = REQ_TIMEOUT_S, start_ok: bool = True):
    """Send one request to the local router server. Returns the response dict."""
    import socket
    path = sock_path()
    if not path.exists() and start_ok:
        _start_server()
    deadline = time.monotonic() + timeout
    last_err = None
    while time.monotonic() < deadline:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as c:
                c.settimeout(deadline - time.monotonic())
                c.connect(str(path))
                c.sendall((json.dumps(request) + "\n").encode())
                buf = b""
                while b"\n" not in buf:
                    chunk = c.recv(1 << 16)
                    if not chunk:
                        break
                    buf += chunk
                resp = json.loads(buf)
        except (FileNotFoundError, ConnectionRefusedError, socket.timeout, OSError) as e:
            last_err = e
            time.sleep(0.05)
            continue
        if "error" in resp:
            raise RouterError(str(resp["error"]))
        return resp
    raise RouterError(f"router server unreachable: {last_err}")


def _server_log() -> Path:
    return state.STATE_DIR / "router.log"


def _start_server():
    """Start `hive router serve` in the background and wait for the socket."""
    log = _server_log()
    log.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PYTHONPATH": f"{_pkg_root()}{os.environ.get('PYTHONPATH', '')}"}
    subprocess.Popen([sys.executable, "-m", "hive.cli", "router", "serve"],
                     stdout=open(log, "ab"), stderr=subprocess.STDOUT,
                     start_new_session=True, cwd=str(_pkg_root()))
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        if sock_path().exists():
            return
        if log.exists() and "not serving" in log.read_text(errors="replace"):
            raise RouterError("router server failed to start; see " + str(log))
        time.sleep(0.05)
    raise RouterError("router server did not start in 60 s; see " + str(log))


def repo_file_count() -> int:
    """Count files tracked by git in the cwd worktree (0 when git fails)."""
    root = Path(os.environ.get("HIVE_CWD", ".")) if os.environ.get("HIVE_CWD") else Path.cwd()
    try:
        out = subprocess.run(["git", "ls-files"], cwd=str(root), capture_output=True, text=True,
                             timeout=10).stdout
        return sum(1 for line in out.splitlines() if line.strip())
    except (OSError, subprocess.TimeoutExpired):
        return 0


def cmd_router_ask(args):
    """`hive router ask --task-file F`: print the tier probabilities.

    Tier descriptions come from config (global toml merged with .hive.toml in
    the cwd), so the human tunes routing only there (task item 2).
    """
    task_text = Path(args.task_file).read_text(errors="replace") if args.task_file else ""
    from . import config
    cfg = config.load(repo=Path(os.environ.get("HIVE_CWD") or os.getcwd()))
    request = task_question(task_text, title=args.title, file_count=repo_file_count(),
                            criteria=tier_criteria(cfg))
    resp = ask_server(request, timeout=float(args.timeout))
    answer = resp["answers"][0]
    probs = answer["probs"]
    pick = max(probs, key=probs.get)
    for label, p in sorted(probs.items(), key=lambda kv: -kv[1]):
        print(f"  {label:<8} {p:.3f}")
    print(f"tier: {pick}  confidence: {answer['confidence']:.3f}  (decision {resp['decision_id']})")
    if args.json:
        print(json.dumps(resp, indent=2))
    return 0


def cmd_router_setup(args):
    install_deps()
    download(getattr(args, "dest", None))
    return 0


def cmd_router_serve(args):
    return serve()


def cmd_router_ping(args):
    """`hive router ping`: check that the server answers."""
    try:
        resp = ask_server(task_question("ping test", file_count=0), timeout=5.0)
        print(f"router: up, decision {resp['decision_id']}")
        return 0
    except RouterError as e:
        print(f"router: down: {e}")
        return 1


def cmd_router_export(args):
    """`hive router export`: write retraining rows from the route log."""
    from . import routelog
    out = Path(args.out) if args.out else None
    path, rows = routelog.export(since=args.since, out=out)
    print(f"router: wrote {rows} rows to {path}")
    return 0
