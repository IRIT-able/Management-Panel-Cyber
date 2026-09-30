from apscheduler.schedulers.background import BackgroundScheduler

scheduler = BackgroundScheduler()
try:
    scheduler.add_job(func=lambda: print("hello"), id="test")
    print("Success")
except Exception as e:
    print("Error:", type(e), str(e))
