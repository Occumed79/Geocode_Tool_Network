from __future__ import annotations

import os

from flask import Flask

app = Flask(__name__)


@app.get('/')
def index():
    return '<!doctype html><title>Occu-Med Geocoder</title><h1>Occu-Med Geocoder</h1>'


@app.get('/healthz')
def healthz():
    return {'ok': True}


if __name__ == '__main__':
    port = int(os.getenv('PORT', '8080'))
    app.run(host='0.0.0.0', port=port)
