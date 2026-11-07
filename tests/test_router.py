"""Router tests (routing step 1). Model tests skip when the files are missing."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from hive import router


@pytest.fixture(scope="module")
def model():
    pytest.importorskip("onnxruntime")
    pytest.importorskip("tokenizers")
    pytest.importorskip("numpy")
    if not (router.CACHE_DIR / "model_fp16.onnx").exists():
        pytest.skip("model files not downloaded (run `hive router setup`)")
    return router.DecisionModel()


def test_get_model_missing_files(tmp_path, monkeypatch):
    monkeypatch.setattr(router, "CACHE_DIR", tmp_path)
    with pytest.raises(router.RouterError, match="model files missing"):
        router.get_model()


def test_operator_order_reference_example(model):
    """The quickstart example of the model card: expect 0.16 / 0.50 / 0.34."""
    probs, rows = model.choice(
        "Refactor the auth module and add tests. The repo has 40 files.",
        "Which model must handle this request?",
        [("haiku", "Short and simple tasks"), ("sonnet", "Normal coding tasks"),
         ("opus", "Long or difficult tasks")],
        {"cost": "low cost before speed"},
    )
    assert probs == {"haiku": pytest.approx(0.16, abs=0.006),
                     "sonnet": pytest.approx(0.50, abs=0.006),
                     "opus": pytest.approx(0.34, abs=0.006)}
    assert sum(probs.values()) == pytest.approx(1.0, abs=0.001)


def test_choice_without_preferences(model):
    probs, _ = model.choice("Fix a one-line typo in README.md.", "Which worker tier must handle this task?",
                            router.TIER_OPTIONS)
    assert len(probs) == 3
    assert sum(probs.values()) == pytest.approx(1.0, abs=0.001)
    top = max(probs, key=probs.get)
    assert top in {"fast", "regular", "think"}


def test_task_question_shape():
    req = router.task_question("Cut a release note.", title="Ship 0.2", file_count=12)
    assert req["state"].startswith("Ship 0.2. Cut a release note.")
    assert req["state"].endswith("The repo has 12 files.")
    assert req["questions"][0]["criteria"] == {label: desc for label, desc in router.TIER_OPTIONS}


def test_long_state_is_trimmed(model):
    state = "word " * 2000
    probs, _ = model.choice(state, "Which worker tier?", router.TIER_OPTIONS)
    assert len(probs) == 3


def test_decide_request_shapes(model):
    req = {"state": "Drop one unused column in a migration.", "preferences": None, "questions": [
        {"id": "risky", "type": "noul", "instructions": "Can this task delete user data?"},
        {"id": "hard", "type": "score", "instructions": "How hard is this task?",
         "levels": ["trivial", "easy", "hard"]},
    ]}
    resp = model.decide(req)
    assert resp["model_version"] == "decision-model onnx opset17"
    assert [a["id"] for a in resp["answers"]] == ["risky", "hard"]
    a_noul = resp["answers"][0]
    assert a_noul["probs"]["yes"] + a_noul["probs"]["no"] == pytest.approx(1.0, abs=1e-6)
    assert a_noul["p_yes"] == a_noul["probs"]["yes"]
    a_score = resp["answers"][1]
    assert a_score["expected_level"] == pytest.approx(sum(i * p for i, p in enumerate(a_score["probs"].values())), abs=1e-3)


def test_serve_idle_exit(monkeypatch, tmp_path):
    """The server exits when no request arrives inside the idle window."""
    pytest.importorskip("onnxruntime")
    if not (router.CACHE_DIR / "model_fp16.onnx").exists():
        pytest.skip("model files not downloaded")
    monkeypatch.setenv("HIVE_ROUTER_IDLE_EXIT_S", "1.5")
    monkeypatch.setattr(router.state, "STATE_DIR", tmp_path)
    result = {}

    def run():
        result["code"] = router.serve()

    th = threading.Thread(target=run, daemon=True)
    th.start()
    for _ in range(300):
        if router.sock_path().exists():
            break
        time.sleep(0.05)
    assert router.sock_path().exists(), "server socket never appeared"
    mode = router.sock_path().stat().st_mode & 0o777
    assert mode == 0o600, f"socket mode is {oct(mode)}, not 0600"
    th.join(timeout=30)
    assert not th.is_alive(), "server did not exit inside the idle window"
    assert result["code"] == 0
    assert not router.sock_path().exists(), "server left the socket file behind"


def test_ask_server_end_to_end(monkeypatch, tmp_path):
    """A real client round trip over the Unix socket."""
    pytest.importorskip("onnxruntime")
    if not (router.CACHE_DIR / "model_fp16.onnx").exists():
        pytest.skip("model files not downloaded")
    monkeypatch.setattr(router.state, "STATE_DIR", tmp_path)
    root = Path(__file__).resolve().parent.parent
    env = {**os.environ, "PYTHONPATH": str(root), "HIVE_STATE_DIR": str(tmp_path)}
    proc = subprocess.Popen([sys.executable, "-m", "hive.cli", "router", "serve"],
                            cwd=str(root), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(600):
            if router.sock_path().exists():
                break
            time.sleep(0.1)
        assert router.sock_path().exists()
        resp = router.ask_server(router.task_question("Add one test for the picker.", file_count=3),
                                 timeout=10.0)
        probs = resp["answers"][0]["probs"]
        assert set(probs) == {"fast", "regular", "think"}
        assert sum(probs.values()) == pytest.approx(1.0, abs=1e-3)
    finally:
        proc.terminate()
        proc.wait(timeout=15)
        if router.sock_path().exists():
            router.sock_path().unlink()
