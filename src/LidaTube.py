"""LidaTube entry point: gunicorn serves src.LidaTube:app; `python LidaTube.py` runs it directly."""

from flask import Flask
from flask_socketio import SocketIO

from data_handler import DataHandler
from web import register_routes

app = Flask(__name__)
app.secret_key = "secret_key"
socketio = SocketIO(app)
data_handler = DataHandler(socketio.emit, yield_to_event_loop=lambda: socketio.sleep(0))
register_routes(app, socketio, data_handler)


if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000)
