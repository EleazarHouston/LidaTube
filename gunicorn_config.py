bind = "0.0.0.0:5000"
workers = 1
threads = 4
timeout = 300
worker_class = "geventwebsocket.gunicorn.workers.GeventWebSocketWorker"
# Gunicorn 25 runs its control socket in a master thread; in the container it cannot bind
# (Errno 13) and logs that while the master forks, which can hang the new worker forever.
control_socket_disable = True
