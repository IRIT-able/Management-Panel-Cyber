# Gunicorn configuration file
import os
from dotenv import load_dotenv

load_dotenv()

# Server socket - use a Unix domain socket so Caddy can proxy on /run/cyberlab-admin/gunicorn.sock
bind = "unix:/run/cyberlab-admin/gunicorn.sock"
backlog = 2048

# Worker processes
workers = 24
threads = 20
worker_class = 'gthread'
worker_connections = 1000
max_requests = 0
max_requests_jitter = 50
preload_app = False
timeout = 30
keepalive = 2

# Restart workers after this many requests, with up to 50% jitter
max_requests = 0
max_requests_jitter = 0

# Logging
accesslog = '-'
errorlog = '-'
loglevel = 'info'
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s"'

# Process naming
proc_name = 'cyberlab-admin'

# Server mechanics
daemon = False
user = None
group = None
tmp_upload_dir = None

# SSL (if you want to enable HTTPS later)
# keyfile = None
# certfile = None

# Environment
raw_env = [
    'FLASK_ENV=production',
    'APP_CONFIG=config.ProductionConfig',
]

# Preload application for better performance
preload_app = False

def when_ready(server):
    server.log.info("Server is ready. Spawning workers")

def worker_int(worker):
    worker.log.info("worker received INT or QUIT signal")
    
    # get traceback info
    import threading
    import sys
    import traceback
    
    id2name = {th.ident: th.name for th in threading.enumerate()}
    code = []
    for thread_id, frame in sys._current_frames().items():
        code.append("\nThread %s(%s):\n" % (id2name.get(thread_id,""), thread_id))
        code.append(''.join(traceback.format_stack(frame)))
    
    worker.log.debug(''.join(code))
