"""Start the app: python -m debtlab.api"""

import uvicorn

from debtlab.api.app import create_app
from debtlab.config import Settings

if __name__ == "__main__":
    settings = Settings.from_env()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)
