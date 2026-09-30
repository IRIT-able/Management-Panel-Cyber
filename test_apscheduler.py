import time
from app import create_app
from app.extensions import scheduler
from flask import current_app

app = create_app('config.DevelopmentConfig')

def dummy_task(student_ids, template_id, app_context):
    pass

with app.app_context():
    try:
        scheduler.add_job(
            id=f"deploy_bulk_vms_1_{time.time()}",
            func=dummy_task,
            args=[[1,2,3], 4, current_app._get_current_object().app_context()]
        )
        print("Success")
    except Exception as e:
        print("Error:", type(e), str(e))
