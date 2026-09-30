import time
from flask import Flask, current_app
from flask_apscheduler import APScheduler
import logging

logging.basicConfig(level=logging.INFO)

app = Flask(__name__)
app.config['SCHEDULER_API_ENABLED'] = False
scheduler = APScheduler()
scheduler.init_app(app)
scheduler.start()

def my_task(msg):
    with app.app_context(): # or can we just use current_app? Let's check both
        print(f"Task running! msg={msg}, current_app.name={current_app.name}")

with app.app_context():
    scheduler.add_job(id='test_job', func=my_task, args=['hello'])

time.sleep(1)
