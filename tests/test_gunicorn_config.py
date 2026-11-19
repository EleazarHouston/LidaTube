"""The gunicorn settings LidaTube runs under."""

import os
import runpy

CONFIG = runpy.run_path(os.path.join(os.path.dirname(__file__), "..", "gunicorn_config.py"))


def test_gunicorn_control_socket_is_off():
    # Gunicorn 25 starts its control socket as a thread in the master. In the container it
    # cannot bind (Errno 13) and logs that error while the master forks the worker, which can
    # leave the worker holding a lock forever: no "Booting worker", the app never serves.
    assert CONFIG.get("control_socket_disable") is True


def test_one_gevent_worker_serves_the_app():
    assert CONFIG["workers"] == 1
    assert CONFIG["worker_class"] == "geventwebsocket.gunicorn.workers.GeventWebSocketWorker"
