import uvicorn
import api

host = "127.0.0.1"
port = 8000

if __name__ == "__main__":
    uvicorn.run(api.app, host=host, port=port, log_level="info")