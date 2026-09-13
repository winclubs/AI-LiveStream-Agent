"""可由桌面端托管、支持应用内优雅退出的 Uvicorn 入口。"""
import uvicorn

from server.app import app
from server.config import SERVER_HOST, SERVER_PORT


def main() -> None:
    config = uvicorn.Config(app, host=SERVER_HOST, port=SERVER_PORT, reload=False)
    server = uvicorn.Server(config)
    app.state.uvicorn_server = server
    server.run()


if __name__ == "__main__":
    main()
