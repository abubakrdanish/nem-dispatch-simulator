"""python -m nemsim.web [--port 8765]: serve the frontend on localhost only."""
import argparse

import uvicorn

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    uvicorn.run("nemsim.web.app:app", host="127.0.0.1", port=a.port)
