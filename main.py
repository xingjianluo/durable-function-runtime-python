"""本地启动入口：uv run main 后访问 /docs。"""

import uvicorn

from runtime.api import create_app

app = create_app()


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
