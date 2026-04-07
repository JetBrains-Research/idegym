RUN cat > /home/appuser/openenv_ws_server.py <<'PY'
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
import uvicorn

app = FastAPI()


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    while True:
        try:
            message = await websocket.receive_text()
        except WebSocketDisconnect:
            break

        if message == "status":
            await websocket.send_text("ready")
        elif message == "ping":
            await websocket.send_text("pong")
        elif message.startswith("echo "):
            await websocket.send_text(message[5:])
        elif message.startswith("add "):
            parts = message.split()
            if len(parts) != 3:
                await websocket.send_text("error: bad add syntax")
                continue
            try:
                left = int(parts[1])
                right = int(parts[2])
            except ValueError:
                await websocket.send_text("error: add requires two integers")
                continue
            await websocket.send_text(str(left + right))
        elif message == "close":
            await websocket.send_text("bye")
            await websocket.close(code=1000)
            break
        elif message == "crash":
            raise RuntimeError("forced websocket crash")
        else:
            await websocket.send_text("error: unknown command")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")
PY

CMD [".venv/bin/python", "/home/appuser/openenv_ws_server.py"]
