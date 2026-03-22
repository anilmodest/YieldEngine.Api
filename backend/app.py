import os
from flask import Flask, jsonify

app = Flask(__name__)


@app.route("/api/status", methods=["GET"])
def status():
    return jsonify({
        "status": "healthy",
        "service": "YieldEngine.Api",
        "version": "0.1.0",
        "environment": os.getenv("ENVIRONMENT", "Development"),
    })


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port, debug=os.getenv("ENVIRONMENT") == "Development")
