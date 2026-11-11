"""Disposable HTTP harness for actual plugin router. NOT a live gateway/core patch."""
import importlib.util
import json
import os
from pathlib import Path
import socket
import sqlite3

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

home = Path(os.environ['HERMES_HOME'])
home.mkdir(parents=True, exist_ok=True)
with sqlite3.connect(home / 'state.db') as conn:
    conn.executescript('''CREATE TABLE sessions(id TEXT PRIMARY KEY,profile_name TEXT,input_tokens INTEGER,
       output_tokens INTEGER,estimated_cost_usd REAL);
       CREATE TABLE messages(id INTEGER PRIMARY KEY,session_id TEXT,role TEXT,content TEXT,
       tool_call_id TEXT,tool_calls TEXT,tool_name TEXT,timestamp REAL);
       INSERT INTO sessions(id,profile_name) VALUES ('s1','default'),('s2','default'),('other','other-profile');
       INSERT INTO messages VALUES (1,'s1','user','Controlled task',NULL,NULL,NULL,1);
       INSERT INTO messages VALUES (2,'s1','assistant','Done.',NULL,NULL,NULL,2);
       INSERT INTO messages VALUES (3,'s2','user','Controlled task two',NULL,NULL,NULL,3);
       INSERT INTO messages VALUES (4,'s2','assistant','Done.',NULL,NULL,NULL,4);''')
spec = importlib.util.spec_from_file_location('browser_api', Path(__file__).resolve().parents[1] / 'dashboard/plugin_api.py')
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)
app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=['null'], allow_methods=['GET'], allow_headers=['Authorization'])
@app.middleware('http')
async def disposable_admission(request, call_next):
    # Test-only owner admission; tests the router's real fail-closed boundary.
    if request.headers.get('Authorization') == 'Bearer ' + os.environ['RECEIPT_TEST_TOKEN']:
        request.state.hosted_http_authenticated = True
    return await call_next(request)
app.include_router(api.router)
sock = socket.socket()
sock.bind(('127.0.0.1', 0))
print(json.dumps({'port': sock.getsockname()[1]}), flush=True)
uvicorn.Server(uvicorn.Config(app, log_level='error')).run(sockets=[sock])
