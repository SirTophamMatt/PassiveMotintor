"""On-demand CPU profiler (/admin/cpu)."""
import threading

from app import cpu_profile


def _spin(stop):
    x = 0
    while not stop.is_set():
        x += 1


def test_profile_names_the_busy_thread():
    stop = threading.Event()
    t = threading.Thread(target=_spin, args=(stop,), name="spinner-test",
                         daemon=True)
    t.start()
    try:
        report = cpu_profile.profile(1)
    finally:
        stop.set()
        t.join()
    assert "spinner-test" in report
    assert "=== spinner-test" in report        # detailed as a busy thread
    assert "_spin" in report                    # with the code it was running


def test_idle_thread_is_not_detailed():
    stop = threading.Event()
    t = threading.Thread(target=stop.wait, name="idle-test", daemon=True)
    t.start()
    try:
        report = cpu_profile.profile(1)
    finally:
        stop.set()
        t.join()
    assert "idle-test" in report
    assert "=== idle-test" not in report


def test_seconds_are_capped():
    assert cpu_profile.MAX_SECONDS <= 30


def test_route_requires_admin(monkeypatch):
    from app import auth, factory
    app = factory.create_app()
    monkeypatch.setattr(auth, "is_admin", lambda: False)
    client = app.server.test_client()
    assert client.get("/admin/cpu?seconds=1").status_code == 403
    monkeypatch.setattr(auth, "is_admin", lambda: True)
    resp = client.get("/admin/cpu?seconds=1")
    assert resp.status_code == 200
    assert "CPU profile over" in resp.get_data(as_text=True)
